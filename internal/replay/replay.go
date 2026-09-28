// Package replay reconstructs what happened from the append-only event journals
// and independently re-derives each snapshot slice, then compares it with the
// state the process recorded. It is the "replay interface" of the system:
// given journals + initial balances, a failure can be re-investigated without
// trusting the snapshot rows.
package replay

import (
	"context"
	"fmt"
	"sort"
	"time"

	"clsnap/internal/protocol"
	"clsnap/internal/store"
)

var zeroTime = time.Time{}

// JournalSource provides a node's journal and id.
type JournalSource interface {
	ID() string
	Journal(ctx context.Context, sessionID string) ([]store.Event, error)
}

// Reconstructed is one node's slice of a session, re-derived purely from
// events in journal order.
type Reconstructed struct {
	NodeID         string
	Status         store.Status
	LocalBalance   int64
	LocalLamport   int64
	Recorded       map[string][]protocol.Transfer // incoming peer -> msgs
	Closed         map[string]bool
	AbortReason    string
	Reasons        []string // reconstruction notes (e.g. events after abort)
}

// Reconstruct re-derives the node slice for one session from its journal.
//
// Rules applied (same as the kernel, read from events only):
//   - the balance at EvStateRecorded is the frozen local state;
//   - every EvChannelRecord between EvStateRecorded and EvChannelClosed for a
//     peer belongs to that incoming channel;
//   - EvSessionAborted terminates a slice as aborted.
func Reconstruct(ctx context.Context, js JournalSource, sessionID string) (*Reconstructed, error) {
	events, err := js.Journal(ctx, sessionID)
	if err != nil {
		return nil, err
	}
	r := &Reconstructed{
		NodeID:   js.ID(),
		Recorded: map[string][]protocol.Transfer{},
		Closed:   map[string]bool{},
		Status:   store.StatusRecording,
		LocalLamport: -1,
	}
	seenState := false
	completed := false
	for _, ev := range events {
		switch ev.Kind {
		case store.EvStateRecorded:
			if seenState {
				r.Reasons = append(r.Reasons, "duplicate local_state_recorded event")
				continue
			}
			seenState = true
			r.LocalLamport = ev.Lamport
			if bal, ok := detailInt64(ev.Detail, "total"); ok {
				r.LocalBalance = bal
			}
		case store.EvChannelRecord:
			from, _ := ev.Detail["from"].(string)
			tx, _ := ev.Detail["tx_id"].(string)
			amount, _ := detailInt64(ev.Detail, "amount")
			if r.Closed[from] {
				r.Reasons = append(r.Reasons, fmt.Sprintf(
					"channel record on already-closed channel from %s (tx %s)", from, tx))
				continue
			}
			r.Recorded[from] = append(r.Recorded[from], protocol.Transfer{TxID: tx, Amount: amount})
		case store.EvChannelClosed:
			from, _ := ev.Detail["from"].(string)
			if r.Closed[from] {
				r.Reasons = append(r.Reasons, fmt.Sprintf("duplicate channel_closed from %s", from))
			}
			r.Closed[from] = true
		case store.EvSessionAborted:
			reason, _ := ev.Detail["reason"].(string)
			r.Status = store.StatusAborted
			r.AbortReason = reason
		case store.EvSessionDone:
			completed = true
		}
	}
	if r.Status == store.StatusAborted {
		return r, nil
	}
	if !seenState {
		r.Status = store.StatusAborted
		r.Reasons = append(r.Reasons, "no local_state_recorded event found")
		return r, nil
	}
	if completed {
		r.Status = store.StatusComplete
	}
	return r, nil
}

// Verify compares a reconstructed slice with the stored session record.
type VerifyResult struct {
	NodeID  string   `json:"node_id"`
	Match   bool     `json:"match"`
	Reasons []string `json:"reasons,omitempty"`
}

func Verify(rec *store.SessionRecord, rc *Reconstructed) VerifyResult {
	v := VerifyResult{NodeID: rc.NodeID, Match: true}
	if rec.Status != rc.Status {
		v.Match = false
		v.Reasons = append(v.Reasons, fmt.Sprintf(
			"status: stored=%s reconstructed=%s", rec.Status, rc.Status))
	}
	if rec.Local != nil {
		if rec.Local.TotalBalance != rc.LocalBalance {
			v.Match = false
			v.Reasons = append(v.Reasons, fmt.Sprintf(
				"local total: stored=%d reconstructed=%d",
				rec.Local.TotalBalance, rc.LocalBalance))
		}
	}
	for from, ch := range rec.Channels {
		stored := ch.Recorded
		got := rc.Recorded[from]
		if len(stored) != len(got) {
			v.Match = false
			v.Reasons = append(v.Reasons, fmt.Sprintf(
				"channel from %s: stored %d msgs, journal reconstructs %d",
				from, len(stored), len(got)))
			continue
		}
		for i := range stored {
			if stored[i].TxID != got[i].TxID || stored[i].Amount != got[i].Amount {
				v.Match = false
				v.Reasons = append(v.Reasons, fmt.Sprintf(
					"channel from %s msg %d: stored %s/%d journal %s/%d",
					from, i, stored[i].TxID, stored[i].Amount, got[i].TxID, got[i].Amount))
			}
		}
		closed := ch.MarkerSeenAt != zeroTime
		if closed != rc.Closed[from] {
			v.Match = false
			v.Reasons = append(v.Reasons, fmt.Sprintf(
				"channel from %s closed flag stored=%v journal=%v", from, closed, rc.Closed[from]))
		}
	}
	v.Reasons = append(v.Reasons, rc.Reasons...)
	if len(rc.Reasons) > 0 {
		v.Match = false
	}
	return v
}

// VerifyAll reconstructs and verifies one session on all nodes and returns a
// global total derived from journals for the conservation cross-check.
type GlobalReplay struct {
	SessionID   string                   `json:"session_id"`
	PerNode     map[string]VerifyResult  `json:"per_node"`
	LocalSum    int64                    `json:"local_sum"`
	InFlightSum int64                    `json:"in_flight_sum"`
	AllMatch    bool                     `json:"all_match"`
}

func VerifyAll(ctx context.Context, sessionID string, nodes []JournalSource) (*GlobalReplay, error) {
	g := &GlobalReplay{SessionID: sessionID, PerNode: map[string]VerifyResult{}, AllMatch: true}
	for _, n := range nodes {
		rec, err := getSession(ctx, n, sessionID)
		if err != nil {
			return nil, err
		}
		rc, err := Reconstruct(ctx, n, sessionID)
		if err != nil {
			return nil, err
		}
		v := Verify(rec, rc)
		g.PerNode[n.ID()] = v
		if !v.Match {
			g.AllMatch = false
		}
		if rc.Status == store.StatusComplete {
			g.LocalSum += rc.LocalBalance
			peers := make([]string, 0, len(rc.Recorded))
			for p := range rc.Recorded {
				peers = append(peers, p)
			}
			sort.Strings(peers)
			for _, p := range peers {
				for _, t := range rc.Recorded[p] {
					g.InFlightSum += t.Amount
				}
			}
		}
	}
	return g, nil
}

type sessionGetter interface {
	GetSessionRecord(ctx context.Context, sessionID string) (*store.SessionRecord, error)
}

func getSession(ctx context.Context, js JournalSource, id string) (*store.SessionRecord, error) {
	switch t := js.(type) {
	case interface {
		GetSession(context.Context, string) (*store.SessionRecord, error)
	}:
		return t.GetSession(ctx, id)
	}
	return nil, fmt.Errorf("journal source %T cannot provide stored session", js)
}

func detailInt64(d map[string]interface{}, key string) (int64, bool) {
	if d == nil {
		return 0, false
	}
	switch v := d[key].(type) {
	case float64:
		return int64(v), true
	case int64:
		return v, true
	case int:
		return int64(v), true
	}
	return 0, false
}
