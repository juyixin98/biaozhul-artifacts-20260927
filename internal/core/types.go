// Package core implements the causal broadcast engine for a fixed-membership group.
//
// The layer consumes two inputs — local events (Submit) and network messages
// (Receive) — and produces one output: events handed to the application via the
// Deliver callback in a causally admissible order. There is no total order:
// concurrent events may be delivered in any order that respects causal
// dependencies, and this package deliberately does not choose one.
package core

import (
	"fmt"
	"strings"
)

// Version is the engine protocol/implementation version. It is emitted into
// every structured log record and into test run metadata so that a test log
// can always be tied back to the exact code that produced it.
const Version = "core-1.0.0"

// NodeID identifies a group member. Membership is fixed for the lifetime of
// an Engine (see Membership).
type NodeID string

// VC is a vector clock: one component per member of the group.
//
// Invariant used throughout the package: clock[origin] is the count of events
// originating at "origin" that have been delivered locally. Components for
// unknown senders are treated as missing prerequisites rather than ignored, so
// a malformed message referencing a stranger node can never slip through.
type VC map[NodeID]int

// Clone returns an independent copy.
func (v VC) Clone() VC {
	out := make(VC, len(v))
	for k, x := range v {
		out[k] = x
	}
	return out
}

// Equal reports whether two clocks agree on every component.
func (v VC) Equal(o VC) bool {
	if len(v) != len(o) {
		return false
	}
	for k, x := range v {
		if o[k] != x {
			return false
		}
	}
	return true
}

// HappensBefore reports v < o in the causal partial order: every component of
// v is <= the corresponding component of o, and at least one is strictly
// smaller. Equal and concurrent clocks both return false.
func (v VC) HappensBefore(o VC) bool {
	strict := false
	for k, x := range o {
		if v[k] > x {
			return false
		}
		if v[k] < x {
			strict = true
		}
	}
	// Any component present in v but absent in o makes v[k] > 0 > o[k],
	// which was already caught above for o's keys; check v's own keys too.
	for k, x := range v {
		if _, ok := o[k]; !ok && x > 0 {
			return false
		}
	}
	return strict
}

// Concurrent reports v || o: neither clock precedes the other.
func (v VC) Concurrent(o VC) bool {
	return !v.HappensBefore(o) && !o.HappensBefore(v) && !v.Equal(o)
}

// String renders the clock in a stable node-sorted form for logs.
func (v VC) String() string {
	ids := make([]string, 0, len(v))
	for k := range v {
		ids = append(ids, string(k))
	}
	// simple insertion sort to avoid pulling sort into hot formatting
	for i := 1; i < len(ids); i++ {
		for j := i; j > 0 && ids[j-1] > ids[j]; j-- {
			ids[j-1], ids[j] = ids[j], ids[j-1]
		}
	}
	var b strings.Builder
	b.WriteByte('{')
	for i, id := range ids {
		if i > 0 {
			b.WriteByte(' ')
		}
		fmt.Fprintf(&b, "%s:%d", id, v[NodeID(id)])
	}
	b.WriteByte('}')
	return b.String()
}

// Envelope is the unit of broadcast. It is what travels over HTTP and what the
// store persists. VC is the event's causal context captured at its origin: it
// equals the origin's local clock just after the event was applied there, so
// it counts the event itself at component Origin.
type Envelope struct {
	// ID is globally unique and deterministic for (origin, seq) collisions:
	// "<origin>#<seq>". A redelivered network packet therefore has the same ID.
	ID string `json:"id"`
	// Origin is the member that first submitted the event.
	Origin NodeID `json:"origin"`
	// Seq is origin's monotonically increasing event sequence, 1-based.
	Seq int `json:"seq"`
	// Payload is the application data, opaque to the ordering layer.
	Payload string `json:"payload"`
	// VC is the event vector clock (causal context at origin).
	VC VC `json:"vc"`
}

// Reason is the machine-readable category of a non-delivered Receive.
// Distinct categories are required: tests assert on the exact failure class
// instead of a uniform "not delivered" or a swallowed error.
type Reason string

const (
	// ReasonAlreadyDelivered: the packet's event was delivered before.
	ReasonAlreadyDelivered Reason = "already_delivered"
	// ReasonAlreadyBuffered: a duplicate copy is sitting in the hold buffer.
	ReasonAlreadyBuffered Reason = "already_buffered"
	// ReasonGap: an earlier event from the same origin has never arrived
	// (FIFO/causal predecessor on the origin's own lane is missing).
	ReasonGap Reason = "missing_origin_predecessor"
	// ReasonDepMissing: every earlier event of the origin lane is present, but
	// the clock references events from other members that have not been
	// delivered yet. Missing lists exactly which (node -> delivered count vs.
	// required count).
	ReasonDepMissing Reason = "missing_causal_dependency"
	// ReasonUnknownSender: origin is not a member of the fixed group.
	ReasonUnknownSender Reason = "unknown_sender"
	// ReasonBufferFull: the hold buffer is at capacity; the caller must apply
	// backpressure. Predecessors are never dropped to make room.
	ReasonBufferFull Reason = "buffer_full"
	// ReasonMalformed: structural problems (bad ID/seq/clock consistency).
	ReasonMalformed Reason = "malformed"
)

// HoldInfo explains why one envelope is waiting in the buffer. It powers the
// "missing causal predecessor retains a waiting reason" requirement and the
// operator-facing /debug/buffer inspection endpoint.
type HoldInfo struct {
	Env    Envelope
	Reason Reason
	// Missing maps each member whose delivered counter is behind the
	// envelope's clock to a human-readable "have<need" note.
	Missing map[NodeID]string
}

// ReceiveResult reports exactly what Receive did with one packet.
type ReceiveResult struct {
	// Delivered is true iff the packet was delivered synchronously (possibly
	// after flushing a cascade of buffered predecessors).
	Delivered bool
	// Reason is set when Delivered is false.
	Reason Reason
	// Detail carries the structured waiting explanation when buffered.
	Detail *HoldInfo
	// Flushed lists envelopes delivered as a cascade while processing this
	// packet, in delivery order. The received envelope itself is last.
	Flushed []Envelope
}
