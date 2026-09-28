// Package reassembly implements the TCP stream reassembly core: sequence-space
// bookkeeping with 32-bit wrap semantics, per-connection generations, overlap
// policy enforcement and gap/conflict evidence. It is pure logic: storage and
// HTTP live in other packages and depend on this one, never the reverse.
package reassembly

import "time"

// OverlapPolicy decides what happens when a segment delivers a byte different
// from the one already accepted at the same sequence position.
type OverlapPolicy string

const (
	// PolicyFirstWins keeps the original byte; the conflicting replacement is
	// rejected but fully recorded as evidence. This matches RFC 9293's
	// historical recommendation and Wireshark's default.
	PolicyFirstWins OverlapPolicy = "first_wins"
	// PolicyLastWins lets the new byte replace the established one. The
	// displaced byte and segment are retained as evidence.
	PolicyLastWins OverlapPolicy = "last_wins"
	// PolicyQuarantine keeps the stream unchanged and files the entire
	// conflicting fragment in a quarantine area for manual review; nothing it
	// contributes is output.
	PolicyQuarantine OverlapPolicy = "quarantine"
)

// ValidPolicy reports whether p is a supported policy.
func ValidPolicy(p OverlapPolicy) bool {
	switch p {
	case PolicyFirstWins, PolicyLastWins, PolicyQuarantine:
		return true
	}
	return false
}

// Disposition values for a conflicting byte.
const (
	DispRejected    = "rejected"    // first_wins: offered byte dropped
	DispReplaced    = "replaced"    // last_wins: established byte displaced
	DispQuarantined = "quarantined" // quarantine: new fragment held aside
)

// EventLevel classifies diagnostic records.
type EventLevel string

const (
	LevelInfo      EventLevel = "info"      // accepted / normal state change
	LevelWarn      EventLevel = "warn"      // suspicious but handled
	LevelReject    EventLevel = "reject"    // bytes refused by a rule
	LevelUndecided EventLevel = "undecided" // evidence insufficient to decide
)

// EventCode enumerates every decision the core can emit.
type EventCode string

const (
	EvSYNOpened          EventCode = "SYN_OPENED"
	EvSYNDuplicate       EventCode = "SYN_DUPLICATE"
	EvSYNACKEstablished  EventCode = "SYNACK_ESTABLISHED"
	EvHandshakeAbsent    EventCode = "HANDSHAKE_ABSENT"
	EvNewGeneration      EventCode = "NEW_GENERATION"
	EvSegmentAccepted    EventCode = "SEGMENT_ACCEPTED"
	EvRetransmitIdent    EventCode = "RETRANSMIT_IDENTICAL"
	EvOverlapConflict    EventCode = "OVERLAP_CONFLICT"
	EvDataAfterFIN       EventCode = "DATA_AFTER_FIN_REJECTED"
	EvFINAccepted        EventCode = "FIN_ACCEPTED"
	EvFINDuplicate       EventCode = "FIN_DUPLICATE"
	EvFINConflict        EventCode = "FIN_POSITION_CONFLICT"
	EvRSTClosed          EventCode = "RST_CLOSED"
	EvPacketAfterRST     EventCode = "PACKET_AFTER_RST_UNDECIDED"
	EvGenerationComplete EventCode = "GENERATION_COMPLETE"
)

// Event is one diagnostic record explaining one decision.
//
// Payload never contains user data; PayloadPreview carries at most a few
// masked/redacted bytes only when the caller explicitly enables previewing.
type Event struct {
	Seq        int64      `json:"seq"` // monotonic event sequence within the run
	RequestID  string     `json:"request_id"`
	RecordID   string     `json:"record_id,omitempty"`
	Timestamp  string     `json:"timestamp,omitempty"`
	Code       EventCode  `json:"code"`
	Level      EventLevel `json:"level"`
	Flow       string     `json:"flow"`
	Generation int        `json:"generation"`
	Direction  string     `json:"direction"`
	Msg        string     `json:"msg"`
	// Key state at decision time.
	RawSeq     uint32 `json:"raw_seq,omitempty"`
	AbsStart   int64  `json:"abs_start,omitempty"` // first data byte coordinate
	AbsEnd     int64  `json:"abs_end,omitempty"`   // one past last data byte
	NextContig int64  `json:"next_contiguous,omitempty"`
	FINPos     int64  `json:"fin_pos,omitempty"` // -1 when no FIN seen
	// PayloadPreview is empty unless redaction is disabled. It is truncated to
	// previewMax bytes with the remainder length reported.
	PayloadPreview string `json:"payload_preview,omitempty"`
	PreviewTotal   int    `json:"preview_total_bytes,omitempty"`
}

// Conflict is byte-level evidence of a data disagreement between segments.
type Conflict struct {
	ID          string        `json:"id"`
	RequestID   string        `json:"request_id"`
	RecordID    string        `json:"offered_record_id"` // the new segment
	Flow        string        `json:"flow"`
	Generation  int           `json:"generation"`
	Direction   string        `json:"direction"`
	ByteOffset  int64         `json:"byte_offset"` // monotonic coordinate of the byte
	RawSeq      uint32        `json:"raw_seq"`     // and its raw 32-bit seq
	Accepted    byte          `json:"accepted_byte"`
	Offered     byte          `json:"offered_byte"`
	AcceptedBy  string        `json:"accepted_by_record_id"`
	Policy      OverlapPolicy `json:"policy"`
	Disposition string        `json:"disposition"`
	Timestamp   string        `json:"timestamp,omitempty"`
}

// HeldByte is one quarantined byte (policy=quarantine): position, value and
// the record that offered it. Nothing held is ever output as stream data.
type HeldByte struct {
	Offset   int64  `json:"offset"`
	RawSeq   uint32 `json:"raw_seq"`
	Value    byte   `json:"value"`
	RecordID string `json:"record_id"`
}

// byteConflict is the policy-free description the buffer hands up; the manager
// annotates flow identity and request id.
type byteConflict struct {
	offset        int64
	rawSeq        uint32
	existing      byte
	offered       byte
	existingOwner string
	disposition   string
}

func nowTS() string { return time.Now().UTC().Format(time.RFC3339Nano) }
