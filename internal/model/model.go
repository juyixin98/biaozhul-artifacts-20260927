// Package model defines the data and error contracts shared by every module of
// the NAT laboratory: configuration parsing, the network model, state storage
// and the replay interface all speak these types.
package model

import (
	"errors"
	"time"
)

// Protocol is the transport protocol carried inside the IP packet.
// Only reassembled TCP and UDP inputs are supported; ICMP and other protocols
// are rejected as protocol_unsupported.
type Protocol string

const (
	TCP Protocol = "TCP"
	UDP Protocol = "UDP"
)

// Direction tells the model which side of the NAT the packet arrived from.
type Direction string

const (
	Outbound Direction = "outbound" // internal -> external
	Inbound  Direction = "inbound"  // external -> internal
)

// Verdict is the final decision the model makes for a packet.
type Verdict string

const (
	// AcceptTranslate means the packet is allowed and its 5-tuple is translated.
	AcceptTranslate Verdict = "accept_translate"
	// AcceptForward means the packet matches an existing mapping and is forwarded
	// with the existing translation (state refresh may occur).
	AcceptForward Verdict = "accept_forward"
	// Reject means the packet is dropped for one of the classified RejectReason
	// values. Nothing is forwarded.
	Reject Verdict = "reject"
	// ComputeFailure means the model could not complete the decision because of
	// an internal/storage failure, distinct from every policy rejection.
	ComputeFailure Verdict = "compute_failure"
)

// RejectReason classifies every rejection into exactly one of four required
// error classes, plus an internal storage-failure sentinel.
type RejectReason string

const (
	// ---- input error: malformed or unsupported packet metadata ----
	ReasonInvalidInput      RejectReason = "invalid_input"
	ReasonProtocolUnsupport RejectReason = "protocol_unsupported"
	ReasonFragmentDropped   RejectReason = "fragment_not_reassembled"
	ReasonExternalMismatch  RejectReason = "external_address_mismatch"

	// ---- state conflict: return packet does not match connection state ----
	ReasonNoMapping      RejectReason = "no_matching_mapping"
	ReasonMappingExpired RejectReason = "mapping_expired"
	ReasonRemoteMismatch RejectReason = "remote_endpoint_mismatch"
	ReasonStateConflict  RejectReason = "tcp_state_conflict"

	// ---- resource exhaustion ----
	ReasonPortExhausted RejectReason = "port_pool_exhausted"

	// ---- compute / persistence failure ----
	ReasonStorageFailure RejectReason = "storage_failure"
)

// Class is the coarse error class required by the spec; it lets tests assert
// the failure category without coupling to individual reason strings.
type Class string

const (
	ClassInput      Class = "input_error"
	ClassState      Class = "state_conflict"
	ClassExhaustion Class = "resource_exhaustion"
	ClassCompute    Class = "compute_failure"
	ClassNone       Class = ""
)

// ClassOf maps a reject reason to its coarse error class.
func ClassOf(r RejectReason) Class {
	switch r {
	case ReasonInvalidInput, ReasonProtocolUnsupport, ReasonFragmentDropped,
		ReasonExternalMismatch:
		return ClassInput
	case ReasonNoMapping, ReasonMappingExpired, ReasonRemoteMismatch,
		ReasonStateConflict:
		return ClassState
	case ReasonPortExhausted:
		return ClassExhaustion
	case ReasonStorageFailure:
		return ClassCompute
	}
	return ClassNone
}

// ErrComputeFailure is returned (alongside a Decision) when the model cannot
// finish a decision because the persistence layer failed. It is deliberately a
// distinct error from every policy reject: policy rejections never come back as
// Go errors.
var ErrComputeFailure = errors.New("nat: compute/storage failure while processing packet")

// FiveTuple is an IP+transport endpoint pair in the input's own addressing.
// All fields are host-order integers; IPs are canonical strings ("10.0.0.2").
type FiveTuple struct {
	SrcIP   string   `json:"src_ip"`
	SrcPort uint16   `json:"src_port"`
	DstIP   string   `json:"dst_ip"`
	DstPort uint16   `json:"dst_port"`
	Proto   Protocol `json:"proto"`
}

// TCPFlagBits carries the SYN/ACK/FIN/RST bits relevant to the simplified
// state machine. Other bits are ignored but must not make a packet invalid.
type TCPFlagBits struct {
	SYN bool `json:"syn"`
	ACK bool `json:"ack"`
	FIN bool `json:"fin"`
	RST bool `json:"rst"`
}

// Any reports whether any of the tracked bits is set.
func (f TCPFlagBits) Any() bool { return f.SYN || f.ACK || f.FIN || f.RST }

// Packet is the input metadata for one IP packet. The model never touches a
// real network: only metadata is processed.
type Packet struct {
	// Seq is the position of the packet inside the replayed trace and is used
	// to order decision logs deterministically.
	Seq int64 `json:"seq"`
	// Label is an optional human-readable fixture tag.
	Label string `json:"label"`
	// ObservedAt is the timestamp the simulated observer stamped the packet.
	// The model keeps a monotonic watermark: a value below the watermark does
	// not turn back the clock and cannot revive an expired mapping.
	ObservedAt time.Time `json:"observed_at"`
	Direction  Direction `json:"direction"`
	Tuple      FiveTuple `json:"tuple"`
	// Fragmented marks an IP fragment. Only reassembled input is supported,
	// so a true value is rejected with fragment_not_reassembled.
	Fragmented bool `json:"fragmented,omitempty"`
	// TCP only; ignored for UDP.
	TCP TCPFlagBits `json:"tcp_flags,omitempty"`
}

// NewPacket is a compact constructor used by fixtures and tests.
func NewPacket(seq int64, at time.Time, dir Direction, srcIP string, srcPort uint16,
	dstIP string, dstPort uint16, proto Protocol, tcp TCPFlagBits) Packet {
	return Packet{
		Seq: seq, ObservedAt: at, Direction: dir,
		Tuple: FiveTuple{SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP,
			DstPort: dstPort, Proto: proto},
		TCP: tcp,
	}
}

// Decision is the full record produced for one input packet: the verdict, the
// mapped 5-tuple when translation happens, and the exact rejection reason and
// human-auditable rationale when it does not.
type Decision struct {
	RunID     string       `json:"run_id"`
	Seq       int64        `json:"seq"`
	Label     string       `json:"label,omitempty"`
	At        time.Time    `json:"at"`
	Verdict   Verdict      `json:"verdict"`
	Reason    RejectReason `json:"reason,omitempty"`
	Class     Class        `json:"class,omitempty"`
	Pre       FiveTuple    `json:"pre"`
	Post      *FiveTuple   `json:"post,omitempty"`
	MappingID string       `json:"mapping_id,omitempty"`
	// StateBefore/StateAfter are the mapping's TCP states ("" for UDP).
	StateBefore string `json:"state_before,omitempty"`
	StateAfter  string `json:"state_after,omitempty"`
	// AllocatedPort is set when this decision created a new mapping.
	AllocatedPort uint16 `json:"allocated_port,omitempty"`
	// Watermark is the model clock used for this decision (never decreases).
	Watermark time.Time `json:"watermark"`
	// Rationale states the key intermediate facts and why the verdict followed;
	// it is part of the replayable problem record, not free-form prose.
	Rationale string `json:"rationale"`
}

// MappingView is a snapshot of one active translation.
type MappingView struct {
	ID           string    `json:"id"`
	Proto        Protocol  `json:"proto"`
	Internal     FiveTuple `json:"internal"` // internal-side 5-tuple (endpoint pair)
	External     FiveTuple `json:"external"` // external-side 5-tuple
	ExternalPort uint16    `json:"external_port"`
	State        string    `json:"state"`
	CreatedAt    time.Time `json:"created_at"`
	LastSeen     time.Time `json:"last_seen"`
	ExpiresAt    time.Time `json:"expires_at"`
}

// Stats are the counters exposed for test assertions and logs.
type Stats struct {
	OutboundFirst      int64 `json:"outbound_first"`
	OutboundForward    int64 `json:"outbound_forward"`
	InboundForward     int64 `json:"inbound_forward"`
	RejectedInput      int64 `json:"rejected_input"`
	RejectedState      int64 `json:"rejected_state"`
	RejectedExhaustion int64 `json:"rejected_exhaustion"`
	ComputeFailures    int64 `json:"compute_failures"`
	MappingsCreated    int64 `json:"mappings_created"`
	MappingsExpired    int64 `json:"mappings_expired"`
	ClockRollbacks     int64 `json:"clock_rollbacks"`
	ActiveMappings     int   `json:"active_mappings"`
}
