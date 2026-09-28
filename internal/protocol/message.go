// Package protocol defines the wire contract between Chandy-Lamport snapshot
// processes. Every inter-process message is an Envelope carried over HTTP and
// persisted by the sender before delivery (reliable-FIFO assumption).
package protocol

import (
	"fmt"
	"time"
)

const (
	KindTransfer = "transfer"
	KindMarker   = "marker"
)

// Transfer is the only business payload in the teaching system: a token move.
type Transfer struct {
	TxID   string `json:"tx_id"`
	Amount int64  `json:"amount"`
	Memo   string `json:"memo,omitempty"`
}

// Envelope is sent on exactly one directed channel (From -> To).
//
// Seq is a per-channel monotonically increasing sequence number assigned at the
// sender when the message is persisted. It lets a receiver defend the FIFO
// assumption: a gap or reordering is a protocol violation, not something the
// receiver silently repairs.
type Envelope struct {
	Kind      string    `json:"kind"`
	MsgID     string    `json:"msg_id"`
	Seq       int64     `json:"seq"`
	From      string    `json:"from"`
	To        string    `json:"to"`
	Lamport   int64     `json:"lamport"`
	SessionID string    `json:"session_id,omitempty"`
	Transfer  *Transfer `json:"transfer,omitempty"`
	SentAt    time.Time `json:"sent_at"`
}

// Validate is the wire-level contract shared by sender and receiver.
func (e *Envelope) Validate(selfID string) error {
	if e.MsgID == "" {
		return fmt.Errorf("msg_id is required")
	}
	if e.Seq <= 0 {
		return fmt.Errorf("seq must be positive, got %d", e.Seq)
	}
	if e.From == "" || e.To == "" {
		return fmt.Errorf("from and to are required")
	}
	if selfID != "" && e.To != selfID {
		return fmt.Errorf("message addressed to %q, receiver is %q", e.To, selfID)
	}
	switch e.Kind {
	case KindTransfer:
		if e.Transfer == nil {
			return fmt.Errorf("transfer envelope missing payload")
		}
		if e.Transfer.TxID == "" {
			return fmt.Errorf("transfer.tx_id is required")
		}
		if e.Transfer.Amount <= 0 {
			return fmt.Errorf("transfer.amount must be positive, got %d", e.Transfer.Amount)
		}
		if e.SessionID != "" {
			return fmt.Errorf("transfer envelopes must not carry a session id")
		}
	case KindMarker:
		if e.SessionID == "" {
			return fmt.Errorf("marker envelope missing session_id")
		}
		if e.Transfer != nil {
			return fmt.Errorf("marker envelope must not carry a transfer payload")
		}
	default:
		return fmt.Errorf("unknown envelope kind %q", e.Kind)
	}
	return nil
}
