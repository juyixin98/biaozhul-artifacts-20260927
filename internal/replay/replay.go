// Package replay folds the append-only event stream back into a state
// snapshot. It is the read model behind the HTTP /replay endpoints and the
// basis of replay tests: an unknown event type is reported as an error rather
// than ignored, so a corrupt or newer-than-reader log can never look like a
// successful replay.
package replay

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"time"

	"workbroker/internal/protocol"
)

// Snapshot is the reconstructed world: every message keyed by id, receipts in
// emission order and the terminal dead reasons.
type Snapshot struct {
	Messages map[string]*protocol.Message
	Receipts map[string]*protocol.Receipt
	Dead     map[string]*protocol.DeadReason
	LastSeq  int64
}

// New returns an empty snapshot.
func New() *Snapshot {
	return &Snapshot{
		Messages: map[string]*protocol.Message{},
		Receipts: map[string]*protocol.Receipt{},
		Dead:     map[string]*protocol.DeadReason{},
	}
}

// EventLoader yields events in seq order (implemented by store.Store).
type EventLoader interface {
	Events(afterSeq int64, limit int) ([]protocol.Event, error)
}

// Build pages through the whole stream and folds it into a Snapshot.
func Build(load func(afterSeq int64, limit int) ([]protocol.Event, error)) (*Snapshot, error) {
	s := New()
	const page = 500
	var after int64
	for {
		evs, err := load(after, page)
		if err != nil {
			return nil, fmt.Errorf("replay: load events after %d: %w", after, err)
		}
		for i := range evs {
			if err := s.Apply(&evs[i]); err != nil {
				return nil, err
			}
			after = evs[i].Seq
		}
		if len(evs) < page {
			return s, nil
		}
	}
}

// Apply folds one event into the snapshot.
func (s *Snapshot) Apply(ev *protocol.Event) error {
	s.LastSeq = ev.Seq
	switch ev.Type {
	case protocol.EventEnqueued:
		var d enqueueData
		if err := json.Unmarshal(ev.Data, &d); err != nil {
			return bad(ev, err)
		}
		body, err := base64.StdEncoding.DecodeString(d.BodyBase64)
		if err != nil {
			return bad(ev, err)
		}
		availAt, err := time.Parse(time.RFC3339Nano, d.AvailableAt)
		if err != nil {
			return bad(ev, err)
		}
		s.Messages[ev.MessageID] = &protocol.Message{
			ID:          ev.MessageID,
			Partition:   ev.Partition,
			Body:        body,
			Status:      protocol.StatusAvailable,
			Attempts:    0,
			MaxAttempts: d.MaxAttempts,
			AvailableAt: availAt,
			CreatedAt:   ev.At,
			UpdatedAt:   ev.At,
		}

	case protocol.EventReceived:
		var d receiveData
		if err := json.Unmarshal(ev.Data, &d); err != nil {
			return bad(ev, err)
		}
		m, ok := s.Messages[ev.MessageID]
		if !ok {
			return bad(ev, fmt.Errorf("receive for unknown message"))
		}
		exp, err := time.Parse(time.RFC3339Nano, d.ExpiresAt)
		if err != nil {
			return bad(ev, err)
		}
		m.Status = protocol.StatusInFlight
		m.Attempts = d.Attempt
		m.InFlightAt = ev.At
		m.VisibilityDeadline = exp
		m.ReceiptID = ev.ReceiptID
		m.WorkerID = d.WorkerID
		m.UpdatedAt = ev.At
		s.Receipts[ev.ReceiptID] = &protocol.Receipt{
			ID:        ev.ReceiptID,
			MessageID: ev.MessageID,
			WorkerID:  d.WorkerID,
			IssuedAt:  ev.At,
			ExpiresAt: exp,
		}

	case protocol.EventVisibilityExt:
		var d extendData
		if err := json.Unmarshal(ev.Data, &d); err != nil {
			return bad(ev, err)
		}
		m, ok := s.Messages[ev.MessageID]
		if !ok {
			return bad(ev, fmt.Errorf("extend for unknown message"))
		}
		r, ok := s.Receipts[ev.ReceiptID]
		if !ok {
			return bad(ev, fmt.Errorf("extend for unknown receipt"))
		}
		exp, err := time.Parse(time.RFC3339Nano, d.NewExpiresAt)
		if err != nil {
			return bad(ev, err)
		}
		r.ExpiresAt = exp
		r.ExtendedAt = ev.At
		m.VisibilityDeadline = exp
		m.UpdatedAt = ev.At

	case protocol.EventAcked:
		m, ok := s.Messages[ev.MessageID]
		if !ok {
			return bad(ev, fmt.Errorf("ack for unknown message"))
		}
		if r, ok := s.Receipts[ev.ReceiptID]; ok {
			r.Consumed = true
		}
		m.Status = protocol.StatusAcked
		m.InFlightAt = time.Time{}
		m.VisibilityDeadline = time.Time{}
		m.ReceiptID = ""
		m.WorkerID = ""
		m.UpdatedAt = ev.At

	case protocol.EventNackRequeued, protocol.EventTimeoutRetry:
		var d requeueData
		if err := json.Unmarshal(ev.Data, &d); err != nil {
			return bad(ev, err)
		}
		m, ok := s.Messages[ev.MessageID]
		if !ok {
			return bad(ev, fmt.Errorf("requeue for unknown message"))
		}
		if r, ok := s.Receipts[ev.ReceiptID]; ok {
			r.Consumed = true
		}
		availAt, err := time.Parse(time.RFC3339Nano, d.AvailableAt)
		if err != nil {
			return bad(ev, err)
		}
		m.Status = protocol.StatusAvailable
		m.AvailableAt = availAt
		m.InFlightAt = time.Time{}
		m.VisibilityDeadline = time.Time{}
		m.ReceiptID = ""
		m.WorkerID = ""
		m.UpdatedAt = ev.At

	case protocol.EventDead:
		var d deadData
		if err := json.Unmarshal(ev.Data, &d); err != nil {
			return bad(ev, err)
		}
		m, ok := s.Messages[ev.MessageID]
		if !ok {
			return bad(ev, fmt.Errorf("dead for unknown message"))
		}
		if r, ok := s.Receipts[ev.ReceiptID]; ok {
			r.Consumed = true
		}
		m.Status = protocol.StatusDead
		m.InFlightAt = time.Time{}
		m.VisibilityDeadline = time.Time{}
		m.ReceiptID = ""
		m.WorkerID = ""
		m.UpdatedAt = ev.At
		history := make([]protocol.AttemptFailure, len(d.History))
		for i, h := range d.History {
			ht, err := time.Parse(time.RFC3339Nano, h.HappenedAt)
			if err != nil {
				return bad(ev, err)
			}
			history[i] = protocol.AttemptFailure{
				ID: h.FailureID, Attempt: h.Attempt, ReceiptID: h.ReceiptID,
				WorkerID: h.WorkerID, Class: protocol.AttemptCause(h.Cause),
				Reason: h.Reason, HappenedAt: ht, MessageID: ev.MessageID,
			}
		}
		reason := &protocol.DeadReason{
			Class:      protocol.AttemptCause(d.Cause),
			Reason:     d.Reason,
			HappenedAt: ev.At,
			Failures:   history,
		}
		s.Dead[ev.MessageID] = reason

	default:
		return bad(ev, fmt.Errorf("unknown event type %q", ev.Type))
	}
	return nil
}

func bad(ev *protocol.Event, err error) error {
	return fmt.Errorf("replay: event seq=%d type=%s message=%s: %w",
		ev.Seq, ev.Type, ev.MessageID, err)
}

// JSON-tagged payload mirrors (independent from kernel's, so replay parsing
// is not coupled to the producer struct layout beyond the wire format).
type enqueueData struct {
	BodyBase64  string `json:"body_base64"`
	MaxAttempts int64  `json:"max_attempts"`
	AvailableAt string `json:"available_at"`
}
type receiveData struct {
	Attempt    int64  `json:"attempt"`
	WorkerID   string `json:"worker_id"`
	ExpiresAt  string `json:"expires_at"`
	FromStatus string `json:"from_status"`
}
type extendData struct {
	WorkerID      string `json:"worker_id"`
	OldExpiresAt  string `json:"old_expires_at"`
	NewExpiresAt  string `json:"new_expires_at"`
	ExtendSeconds int64  `json:"extend_seconds"`
}
type requeueData struct {
	Attempt     int64  `json:"attempt"`
	WorkerID    string `json:"worker_id"`
	Cause       string `json:"cause"`
	FailureID   string `json:"failure_id"`
	AvailableAt string `json:"available_at"`
}
type deadData struct {
	Cause   string         `json:"cause"`
	Reason  string         `json:"reason"`
	History []failureEntry `json:"history"`
}
type failureEntry struct {
	FailureID  string `json:"failure_id"`
	Attempt    int64  `json:"attempt"`
	ReceiptID  string `json:"receipt_id"`
	WorkerID   string `json:"worker_id"`
	Cause      string `json:"cause"`
	Reason     string `json:"reason"`
	HappenedAt string `json:"happened_at"`
}
