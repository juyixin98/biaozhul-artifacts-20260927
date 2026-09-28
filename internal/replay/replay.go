// Package replay reconstructs message state exclusively from the append-only
// event log and compares it against live table state. It is the independent
// oracle used by tests: because replay never reads broker_messages / live
// structs, agreement between replayed and live state is a real consistency
// check rather than the implementation testifying about itself.
package replay

import (
	"context"
	"fmt"
	"sort"
	"time"

	"localbroker/internal/kernel"
	"localbroker/internal/protocol"
	"localbroker/internal/store"
)

// State is one replayed message.
type State struct {
	ID          string
	State       protocol.State
	Attempts    int
	Receipt     string
	ReceiptGen  int64
	LastReceipt string
	Deadline    time.Time
	Failures    []protocol.Failure
	LastSeq     int64
}

// Result is the full replay of one queue plus the raw event count.
type Result struct {
	Queue      string
	EventCount int
	Messages   map[string]*State
}

// FromStore reads ONLY the event log and folds it.
func FromStore(ctx context.Context, st store.Store, queue string) (*Result, error) {
	events, err := st.Events(ctx, queue)
	if err != nil {
		return nil, err
	}
	rm, err := kernel.Replay(events)
	if err != nil {
		return nil, err
	}
	res := &Result{Queue: queue, EventCount: len(events), Messages: map[string]*State{}}
	for id, m := range rm {
		res.Messages[id] = &State{
			ID:          m.ID,
			State:       m.State,
			Attempts:    m.Attempts,
			Receipt:     m.Receipt,
			ReceiptGen:  m.ReceiptGen,
			LastReceipt: m.LastReceipt,
			Deadline:    m.Deadline,
			Failures:    append([]protocol.Failure(nil), m.Failures...),
			LastSeq:     m.LastSeq,
		}
	}
	return res, nil
}

// Diff is one disagreement between live storage and the replayed log.
type Diff struct {
	MessageID string
	Field     string
	Live      string
	Replayed  string
}

// CompareLive crosses replayed state with live state and reports every
// disagreement (state, attempt count, failure count, terminal receipt). An
// empty diff means the two independent views agree.
func (r *Result) CompareLive(ctx context.Context, st store.Store) ([]Diff, error) {
	live, err := st.ListMessages(ctx, r.Queue)
	if err != nil {
		return nil, err
	}
	var diffs []Diff
	byID := map[string]protocol.Message{}
	for _, m := range live {
		byID[m.ID] = m
	}
	for id, rp := range r.Messages {
		lm, ok := byID[id]
		if !ok {
			diffs = append(diffs, Diff{id, "existence", "missing in live store", "present in log"})
			continue
		}
		check := func(field, got, want string) {
			if got != want {
				diffs = append(diffs, Diff{id, field, got, want})
			}
		}
		check("state", string(lm.State), string(rp.State))
		check("attempts", fmt.Sprint(lm.Attempts), fmt.Sprint(rp.Attempts))
		check("receipt_gen", fmt.Sprint(lm.ReceiptGen), fmt.Sprint(rp.ReceiptGen))
		check("failure_count", fmt.Sprint(len(lm.Failures)), fmt.Sprint(len(rp.Failures)))
		check("receipt", lm.Receipt, rp.Receipt)
	}
	for id := range byID {
		if _, ok := r.Messages[id]; !ok {
			diffs = append(diffs, Diff{id, "existence", "present in live store", "missing in log"})
		}
	}
	sort.Slice(diffs, func(i, j int) bool {
		if diffs[i].MessageID == diffs[j].MessageID {
			return diffs[i].Field < diffs[j].Field
		}
		return diffs[i].MessageID < diffs[j].MessageID
	})
	return diffs, nil
}
