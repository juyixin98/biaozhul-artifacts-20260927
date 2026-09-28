// Package model defines the data and error contracts shared by the config,
// NAT engine, state store, replay runner and HTTP boundary. No package other
// than the low-level helpers may invent ad-hoc packet/decision shapes: every
// layer speaks the types in this file.
package model

import (
	"encoding/json"
	"time"
)

// Protocol is restricted to the transports this NAT models.
type Protocol string

const (
	TCP Protocol = "TCP"
	UDP Protocol = "UDP"
)

// Direction is relative to the private side of the NAT.
type Direction string

const (
	Outbound Direction = "outbound"
	Inbound  Direction = "inbound"
)

// FiveTuple is a classic 5-tuple. Protocol is carried alongside the endpoints.
type FiveTuple struct {
	SrcIP    string   `json:"src_ip"`
	SrcPort  uint16   `json:"src_port"`
	DstIP    string   `json:"dst_ip"`
	DstPort  uint16   `json:"dst_port"`
	Protocol Protocol `json:"protocol"`
}

// FragInfo carries the minimal IPv4 fragmentation metadata required to enforce
// the "reassembled input only" rule. Offset is in 8-octet units as on the wire.
type FragInfo struct {
	Offset        uint16 `json:"offset"`
	MoreFragments bool   `json:"more_fragments"`
}

// IsFragment reports whether the metadata describes a non-first fragment or a
// fragment that announces followers.
func (f *FragInfo) IsFragment() bool {
	return f != nil && (f.Offset > 0 || f.MoreFragments)
}

// Packet is one observed datagram, in evaluation order. ObservedAt is the
// capture timestamp; the engine owns the effective (monotonic) clock.
type Packet struct {
	Seq        int64     `json:"seq,omitempty"`
	ObservedAt time.Time `json:"ts"`
	FiveTuple  FiveTuple `json:"-"`
	Direction  Direction `json:"direction"`
	// Flags is a "+" separated TCP flag list, e.g. "SYN", "SYN+ACK", "FIN+ACK".
	Flags string `json:"flags,omitempty"`
	// Fragment is nil for ordinary datagrams.
	Fragment *FragInfo `json:"fragment,omitempty"`
}

// Marshal the embedded five-tuple fields inline.
type packetWire struct {
	Seq        int64     `json:"seq,omitempty"`
	ObservedAt time.Time `json:"ts"`
	SrcIP      string    `json:"src_ip"`
	SrcPort    uint16    `json:"src_port"`
	DstIP      string    `json:"dst_ip"`
	DstPort    uint16    `json:"dst_port"`
	Protocol   Protocol  `json:"protocol"`
	Direction  Direction `json:"direction"`
	Flags      string    `json:"flags,omitempty"`
	Fragment   *FragInfo `json:"fragment,omitempty"`
}

// MarshalJSON implements json.Marshaler.
func (p Packet) MarshalJSON() ([]byte, error) {
	return json.Marshal(packetWire{
		Seq: p.Seq, ObservedAt: p.ObservedAt,
		SrcIP: p.FiveTuple.SrcIP, SrcPort: p.FiveTuple.SrcPort,
		DstIP: p.FiveTuple.DstIP, DstPort: p.FiveTuple.DstPort,
		Protocol: p.FiveTuple.Protocol, Direction: p.Direction,
		Flags: p.Flags, Fragment: p.Fragment,
	})
}

// UnmarshalJSON implements json.Unmarshaler and keeps the wire format flat.
func (p *Packet) UnmarshalJSON(b []byte) error {
	var w packetWire
	if err := json.Unmarshal(b, &w); err != nil {
		return err
	}
	*p = Packet{
		Seq: w.Seq, ObservedAt: w.ObservedAt,
		FiveTuple: FiveTuple{
			SrcIP: w.SrcIP, SrcPort: w.SrcPort,
			DstIP: w.DstIP, DstPort: w.DstPort, Protocol: w.Protocol,
		},
		Direction: w.Direction, Flags: w.Flags, Fragment: w.Fragment,
	}
	return nil
}

// Mapping states. UDP mappings always use StateOpen; TCP walks the handshake.
const (
	StateSynSent     = "syn_sent"
	StateSynAckRcvd  = "syn_ack_rcvd"
	StateEstablished = "established"
	StateFinWait     = "fin_wait"
	StateOpen        = "open" // UDP
	StateClosed      = "closed"
)

// ActiveStates are the states that hold a port reservation.
var ActiveStates = []string{
	StateSynSent, StateSynAckRcvd, StateEstablished, StateFinWait, StateOpen,
}

// IsActiveState reports whether s holds a port reservation.
func IsActiveState(s string) bool {
	for _, a := range ActiveStates {
		if s == a {
			return true
		}
	}
	return false
}

// Mapping is one NAT binding. The private flow (Src*) and remote endpoint
// (Dst*) together identify an endpoint-dependent mapping.
type Mapping struct {
	ID         int64
	RunID      string
	Protocol   Protocol
	SrcIP      string
	SrcPort    uint16
	DstIP      string
	DstPort    uint16
	MappedPort uint16
	State      string
	CreatedAt  time.Time
	LastUsedAt time.Time
	ExpiresAt  time.Time
}

// FlowKey is the lookup key for an outbound-originated mapping.
type FlowKey struct {
	Protocol Protocol
	SrcIP    string
	SrcPort  uint16
	DstIP    string
	DstPort  uint16
}

// Flow returns the mapping's private flow key.
func (m *Mapping) Flow() FlowKey {
	return FlowKey{m.Protocol, m.SrcIP, m.SrcPort, m.DstIP, m.DstPort}
}

// Category partitions every failure into one of four buckets required by the
// replay contract: input errors, state conflicts, resource exhaustion and
// compute failures.
type Category string

const (
	CatInvalidInput      Category = "invalid_input"
	CatStateConflict     Category = "state_conflict"
	CatResourceExhausted Category = "resource_exhausted"
	CatComputeFailure    Category = "compute_failure"
)

// Failure codes. They are stable: fixtures and tests assert on them.
const (
	// invalid_input
	CodeInvalidTimestamp   = "INVALID_TS"
	CodeBadSrcIP           = "BAD_SRC_IP"
	CodeBadDstIP           = "BAD_DST_IP"
	CodeBadSrcPort         = "BAD_SRC_PORT"
	CodeBadDstPort         = "BAD_DST_PORT"
	CodeUnsupportedProto   = "UNSUPPORTED_PROTOCOL"
	CodeBadDirection       = "BAD_DIRECTION"
	CodeBadFlag            = "BAD_FLAG"
	CodeUDPFlags           = "UDP_FLAGS_NOT_ALLOWED"
	CodeFragment           = "FRAGMENT_NOT_REASSEMBLED"
	CodeSrcNotPrivate      = "SRC_NOT_PRIVATE"
	CodeInboundDstMismatch = "INBOUND_DST_MISMATCH"
	CodeUnknownRun         = "UNKNOWN_RUN"

	// state_conflict
	CodeTCPNonSynOutbound = "TCP_NON_SYN_OUTBOUND"
	CodeTCPBadState       = "TCP_BAD_STATE"
	CodeEndpointFiltered  = "ENDPOINT_FILTERED"
	CodeInboundNoMapping  = "INBOUND_NO_MAPPING"
	CodeLateReturnExpired = "LATE_RETURN_EXPIRED"

	// resource_exhausted
	CodePortExhausted = "PORT_EXHAUSTED"

	// compute_failure
	CodeStoreError = "STORE_ERROR"
)

// Decision is the engine verdict for one packet.
type Decision struct {
	Accepted    bool
	Category    Category // empty when Accepted
	Code        string
	Reason      string
	Mapping     *Mapping   // populated whenever a mapping was touched
	Translated  *FiveTuple // post-NAT five-tuple on accept
	ObservedAt  time.Time
	EffectiveAt time.Time
	ClockRewind bool
	// Key intermediate state, recorded verbatim into the event log.
	ActiveCount int   // active mappings after the decision
	Swept       int64 // mappings expired by the sweep preceding this packet
}

// Event is one persisted row of the replayable decision log.
type Event struct {
	ID          int64
	RunID       string
	Seq         int64
	ObservedAt  time.Time
	EffectiveAt time.Time
	ClockRewind bool
	Packet      Packet
	Accepted    bool
	Category    Category
	Code        string
	Reason      string
	MappingID   int64
	MappedPort  uint16
	State       string
	Detail      string // JSON: active_count, swept, ttl, translated tuple
}
