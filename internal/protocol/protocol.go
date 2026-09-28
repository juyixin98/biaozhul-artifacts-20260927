// Package protocol defines the data and error contracts between modules:
// tasks (token transfers), control messages (Chandy-Lamport markers), snapshot
// records and the replay event log. These types are the wire and persistence
// format; JSON tags are part of the contract.
package protocol

import (
	"regexp"

	"clsnap/internal/apperr"
)

// NodeID identifies one of the three local snapshot processes, e.g. "n1".
type NodeID string

// SnapshotID identifies one global snapshot round. Ids are caller supplied
// (fixtures generate them) so parallel rounds are easy to correlate in logs.
type SnapshotID string

var idPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

// Valid checks the id shape; it returns an input error otherwise.
func (s SnapshotID) Valid() error {
	if !idPattern.MatchString(string(s)) {
		return apperr.Inputf(apperr.CodeMalformed,
			"snapshot id %q must match %s and be at most 64 chars", s, idPattern.String())
	}
	return nil
}

func (n NodeID) Valid() error {
	if !idPattern.MatchString(string(n)) {
		return apperr.Inputf(apperr.CodeMalformed,
			"node id %q must match %s", n, idPattern.String())
	}
	return nil
}

// Amount is an integer token quantity. The toy ledger never creates or
// destroys fractional tokens; uint64 with explicit overflow checks is enough.
type Amount = uint64

// Account is the local ledger state for one account. A node owns a disjoint
// set of accounts; the kernel is the only component allowed to mutate these.
type Account struct {
	ID      string `json:"id"`
	Owner   NodeID `json:"owner"`
	Balance uint64 `json:"balance"`
	// Frozen is set while THIS node is recording a snapshot that has already
	// captured this account. Freezing only the recorded node is part of the
	// pedagogical demonstration: other nodes keep transferring, proving the
	// protocol does not pause the whole system.
	Frozen bool `json:"frozen,omitempty"`
}

// Transfer is a task submitted to a node: move Amount tokens from account From
// (must live on that node) to account To (may live on another node). Ref is a
// client-supplied idempotency key; a Ref seen twice is rejected as a conflict.
type Transfer struct {
	Ref    string `json:"ref"`
	From   string `json:"from"`
	To     string `json:"to"`
	Amount uint64 `json:"amount"`
}

// Validate applies input rules independent of ledger state.
func (t Transfer) Validate() error {
	if !idPattern.MatchString(t.Ref) {
		return apperr.Inputf(apperr.CodeMalformed, "transfer ref %q is malformed", t.Ref)
	}
	if t.From == "" || t.To == "" {
		return apperr.Inputf(apperr.CodeMalformed, "transfer %q: from/to required", t.Ref)
	}
	if t.From == t.To {
		return apperr.Inputf(apperr.CodeMalformed, "transfer %q: from and to must differ", t.Ref)
	}
	if t.Amount == 0 {
		return apperr.Inputf(apperr.CodeBadAmount, "transfer %q: amount must be > 0", t.Ref)
	}
	return nil
}

// MsgType discriminates envelopes on the wire.
type MsgType string

const (
	MsgTransfer MsgType = "transfer"
	MsgMarker   MsgType = "marker"
)

// Envelope is the single wire format exchanged over FIFO channels.
type Envelope struct {
	Type     MsgType   `json:"type"`
	Src      NodeID    `json:"src"`
	Dst      NodeID    `json:"dst"`
	Lamport  uint64    `json:"lamport"`
	Transfer *Transfer `json:"transfer,omitempty"`
	Marker   *Marker   `json:"marker,omitempty"`
	// Seq is the channel-local FIFO sequence assigned by the source outbox.
	// It is transported so an ack can identify the exact durable entry; the
	// receiver does not rely on it for ordering (per-channel FIFO does that).
	Seq uint64 `json:"seq,omitempty"`
}

// Marker is the Chandy-Lamport control message: one per directed channel per
// snapshot round.
type Marker struct {
	Snapshot SnapshotID `json:"snapshot"`
	// Epoch is bumped whenever a node boots. A marker carrying a different
	// epoch than the current one proves it is stale after a restart; the
	// receiver rejects the round rather than stitching states from two eras.
	Epoch uint64 `json:"epoch"`
}

// ChannelState records messages captured on one directed inbound channel
// (From -> recorder node) between the recorder's local-state save and the
// marker arrival on that channel.
type ChannelState struct {
	From      NodeID     `json:"from"`
	To        NodeID     `json:"to"`
	Snapshot  SnapshotID `json:"snapshot"`
	Closed    bool       `json:"closed"`
	Messages  []Transfer `json:"messages"`
	// Sum is the running total of captured message amounts, maintained by the
	// coordinator so replay can verify conservation without re-walking.
	Sum       uint64     `json:"sum"`
}

// Phase of one node's participation in one snapshot round.
type Phase string

const (
	PhaseNone     Phase = ""          // never seen this round
	PhaseRecording Phase = "recording" // saved local state, collecting channels
	PhaseComplete  Phase = "complete"  // all inbound channels closed
	PhaseAborted   Phase = "aborted"   // round failed / invalidated by restart
)

// LocalState is the snapshot of one node at marker-initiation time.
type LocalState struct {
	Node       NodeID             `json:"node"`
	Snapshot   SnapshotID         `json:"snapshot"`
	Accounts   map[string]Account `json:"accounts"`
	Total      uint64             `json:"total"`
	Lamport    uint64             `json:"lamport"`
	RecordedAt string             `json:"recorded_at"` // RFC3339 UTC
	Epoch      uint64             `json:"epoch"`
}

// NodeRecord is one node's full record for one snapshot round.
type NodeRecord struct {
	Node      NodeID                  `json:"node"`
	Snapshot  SnapshotID              `json:"snapshot"`
	Phase     Phase                   `json:"phase"`
	Local     *LocalState             `json:"local,omitempty"`
	Channels  map[NodeID]*ChannelState `json:"channels"`
	// Reason is populated when Phase == aborted.
	Reason    string                  `json:"reason,omitempty"`
	UpdatedAt string                  `json:"updated_at"`
}

// EventKind enumerates replay log entries.
type EventKind string

const (
	EvTransferAccepted EventKind = "transfer_accepted"
	EvTransferDelivered EventKind = "transfer_delivered"
	EvMarkerSent        EventKind = "marker_sent"
	EvMarkerReceived    EventKind = "marker_received"
	EvStateRecorded     EventKind = "state_recorded"
	EvChannelClosed     EventKind = "channel_closed"
	EvRoundComplete     EventKind = "round_complete"
	EvRoundAborted      EventKind = "round_aborted"
	EvTransferRejected  EventKind = "transfer_rejected"
)

// Event is one append-only log line used for replay and for the test run logs.
type Event struct {
	Seq      uint64                 `json:"seq"`
	RunID    string                 `json:"run_id"`
	Kind     EventKind              `json:"kind"`
	Node     NodeID                 `json:"node,omitempty"`
	Snapshot SnapshotID             `json:"snapshot,omitempty"`
	Peer     NodeID                 `json:"peer,omitempty"`
	Lamport  uint64                 `json:"lamport,omitempty"`
	Ref      string                 `json:"ref,omitempty"`
	Detail   map[string]any         `json:"detail,omitempty"`
	At       string                 `json:"at"`
}
