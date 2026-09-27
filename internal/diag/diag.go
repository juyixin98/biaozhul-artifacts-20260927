// Package diag carries the decision record: why each packet or byte range
// was accepted, rejected or left undecidable, with enough key state to
// reproduce the verdict.
//
// Redaction rules: raw payload bytes never enter a DiagRecord. Records hold
// payload length, a SHA-256 fingerprint and an optional short masked
// preview. The Rendered/Text form additionally masks endpoint IPs on demand.
package diag

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/netip"
	"strings"
)

// Decision is the top-level verdict.
type Decision string

const (
	Accepted    Decision = "ACCEPTED"
	Rejected    Decision = "REJECTED"
	Undecidable Decision = "UNDECIDABLE"
)

// Category refines a verdict. Categories are stable strings used by tests
// and log filters; do not renumber them.
type Category string

const (
	// Accepted.
	CatHandshakeSYN        Category = "ACCEPTED_HANDSHAKE_SYN"
	CatHandshakeSYNACK     Category = "ACCEPTED_HANDSHAKE_SYNACK"
	CatInOrderData         Category = "ACCEPTED_IN_ORDER_DATA"
	CatOutOfOrderData      Category = "ACCEPTED_OUT_OF_ORDER_DATA"
	CatRetransmitIdentical Category = "ACCEPTED_RETRANSMIT_IDENTICAL"
	CatFIN                 Category = "ACCEPTED_FIN"
	CatRST                 Category = "ACCEPTED_RST"
	CatKeepalive           Category = "ACCEPTED_KEEPALIVE"
	CatGenerationReuse     Category = "ACCEPTED_GENERATION_REUSE_SYN"
	CatInferredGeneration  Category = "ACCEPTED_INFERRED_GENERATION_NO_HANDSHAKE"

	// Rejected.
	CatBadPacket            Category = "REJECTED_MALFORMED"
	CatUnknownFlow          Category = "REJECTED_UNKNOWN_FLOW"
	CatCannotOrient         Category = "REJECTED_CANNOT_ORIENT"
	CatOutsideWindow        Category = "REJECTED_OUTSIDE_WINDOW"
	CatDataAfterFIN         Category = "REJECTED_DATA_AFTER_FIN"
	CatAfterRST             Category = "REJECTED_AFTER_RST"
	CatStraySYNACK          Category = "REJECTED_STRAY_SYNACK"
	CatLateSYNConflict      Category = "REJECTED_LATE_SYN_CONFLICTS_DATA"
	CatRepeatedSYN          Category = "REJECTED_REPEATED_SYN_NO_REUSE"
	CatBufferLimit          Category = "REJECTED_BUFFER_LIMIT"
	CatConflictFirstWins    Category = "REJECTED_CONFLICT_FIRST_WINS"
	CatConflictLastReplaced Category = "REJECTED_CONFLICT_LAST_WINS_REPLACED_INCUMBENT"

	// Undecidable.
	CatConflictDelivered    Category = "UNDECIDABLE_CONFLICT_AGAINST_DELIVERED"
	CatConflictUnverifiable Category = "UNDECIDABLE_CONFLICT_UNVERIFIABLE_EVIDENCE_GONE"
	CatConflictQuarantined  Category = "UNDECIDABLE_CONFLICT_QUARANTINED"
	CatGenerationAmbiguous  Category = "UNDECIDABLE_GENERATION_AMBIGUOUS"
	CatHalfSpaceUnordered   Category = "UNDECIDABLE_HALF_SPACE_UNORDERED"
	CatMissingHandshakeHold Category = "UNDECIDABLE_MISSING_HANDSHAKE_HELD"
	CatGap                  Category = "UNDECIDABLE_GAP"
)

// SeqState is a state snapshot included verbatim in every record. Absolute
// sequence numbers are 64-bit (see seqnum.Extend); stream offsets are bytes
// relative to ISN+1.
type SeqState struct {
	ISN             uint32 `json:"isn"`
	RcvNxtAbs       uint64 `json:"rcv_nxt_abs"`
	DeliveredOffset uint64 `json:"delivered_offset"`
	BufferedBytes   int    `json:"buffered_bytes"`
	FINSeen         bool   `json:"fin_seen"`
	FINEndAbs       uint64 `json:"fin_end_abs,omitempty"`
	Closed          bool   `json:"closed"`
	Reset           bool   `json:"reset"`
	Generation      int    `json:"generation"`
	InferredGen     bool   `json:"inferred_generation"`
}

// Fingerprint identifies a payload without exposing it.
type Fingerprint struct {
	Length int    `json:"length"`
	SHA256 string `json:"sha256"`
	// Preview is an optional, masked ASCII rendering of the first bytes.
	Preview string `json:"preview,omitempty"`
}

// Record is one diagnostic entry.
type Record struct {
	RequestID string   `json:"request_id,omitempty"`
	RecordID  string   `json:"record_id,omitempty"` // packet record id
	FlowKey   string   `json:"flow_key,omitempty"`
	Direction string   `json:"direction,omitempty"`
	Decision  Decision `json:"decision"`
	Category  Category `json:"category"`
	Reason    string   `json:"reason"`

	// Sequence context (absolute 64-bit space).
	SegSeqAbs uint64   `json:"seg_seq_abs,omitempty"`
	SegEndAbs uint64   `json:"seg_end_abs,omitempty"`
	State     SeqState `json:"state"`

	Payload *Fingerprint `json:"payload,omitempty"`
	// Conflict evidence, populated only for conflict categories.
	Conflict *Conflict `json:"conflict,omitempty"`

	// Endpoints, masked only in the rendered text line (kept here for the
	// database/API so reviewers can correlate synthetic fixtures).
	Src string `json:"src,omitempty"`
	Dst string `json:"dst,omitempty"`
}

// Conflict captures contradictory bytes at one range.
type Conflict struct {
	StartAbs     uint64 `json:"start_abs"`
	EndAbs       uint64 `json:"end_abs"`
	IncumbentSHA string `json:"incumbent_sha256"`
	NewcomerSHA  string `json:"newcomer_sha256"`
	// Winner says which copy the configured policy keeps; "held" means the
	// bytes are quarantined and not delivered in either form.
	Winner string `json:"winner"`
}

// FingerprintPayload builds the redacted payload descriptor. previewBytes
// limits the masked preview (0 disables it).
func FingerprintPayload(b []byte, previewBytes int) *Fingerprint {
	if b == nil {
		return nil
	}
	sum := sha256.Sum256(b)
	fp := &Fingerprint{Length: len(b), SHA256: hex.EncodeToString(sum[:])}
	if previewBytes > 0 {
		n := previewBytes
		if n > len(b) {
			n = len(b)
		}
		fp.Preview = MaskPreview(b[:n])
	}
	return fp
}

// MaskPreview renders bytes for logs: printable ASCII kept, everything else
// replaced with '.', so control sequences or binary data cannot forge log
// lines or leak content at a glance.
func MaskPreview(b []byte) string {
	var sb strings.Builder
	for _, c := range b {
		if c >= 0x20 && c < 0x7f {
			sb.WriteByte(c)
		} else {
			sb.WriteByte('.')
		}
	}
	return sb.String()
}

// MaskAddr redacts an endpoint address for textual logs, keeping /24 (v4) or
// /48 (v6) for correlation.
func MaskAddr(a netip.Addr) string {
	if !a.IsValid() {
		return "?"
	}
	if a.Is4In6() {
		a = a.Unmap()
	}
	var p netip.Prefix
	var err error
	if a.Is4() {
		p, err = a.Prefix(24)
	} else {
		p, err = a.Prefix(48)
	}
	if err != nil {
		return "?"
	}
	return p.Masked().String()
}

// Text renders one stable, greppable log line.
func (r Record) Text(maskIPs bool) string {
	parts := []string{
		string(r.Decision),
		string(r.Category),
	}
	id := r.RequestID
	if id == "" {
		id = "-"
	}
	parts = append(parts, "req="+id)
	if r.RecordID != "" {
		parts = append(parts, "pkt="+r.RecordID)
	}
	if r.FlowKey != "" {
		parts = append(parts, "flow="+r.FlowKey)
	}
	if r.Direction != "" {
		parts = append(parts, "dir="+r.Direction)
	}
	if r.Src != "" || r.Dst != "" {
		src, dst := r.Src, r.Dst
		if maskIPs {
			src = maskHost(src)
			dst = maskHost(dst)
		}
		parts = append(parts, src+"->"+dst)
	}
	st := r.State
	parts = append(parts, fmt.Sprintf(
		"gen=%d isn=%d nxt=%d deliv=%d buf=%d fin=%v closed=%v reset=%v inferred=%v",
		st.Generation, st.ISN, st.RcvNxtAbs, st.DeliveredOffset, st.BufferedBytes,
		st.FINSeen, st.Closed, st.Reset, st.InferredGen))
	if r.SegEndAbs > r.SegSeqAbs || r.SegSeqAbs != 0 {
		parts = append(parts, fmt.Sprintf("seg=[%d,%d)", r.SegSeqAbs, r.SegEndAbs))
	}
	if r.Payload != nil {
		parts = append(parts, fmt.Sprintf("payload(len=%d sha=%s", r.Payload.Length, shortHash(r.Payload.SHA256)))
		if r.Payload.Preview != "" {
			parts[len(parts)-1] += " preview=" + r.Payload.Preview
		}
		parts[len(parts)-1] += ")"
	}
	if r.Conflict != nil {
		c := r.Conflict
		parts = append(parts, fmt.Sprintf(
			"CONFLICT[%d,%d) incumbent=%s newcomer=%s winner=%s",
			c.StartAbs, c.EndAbs, shortHash(c.IncumbentSHA), shortHash(c.NewcomerSHA), c.Winner))
	}
	parts = append(parts, "reason="+r.Reason)
	return strings.Join(parts, " ")
}

func shortHash(h string) string {
	if len(h) > 12 {
		return h[:12]
	}
	return h
}

// maskHost masks the host part of a "host:port" rendering.
func maskHost(hp string) string {
	if hp == "" {
		return ""
	}
	if a, err := netip.ParseAddrPort(hp); err == nil {
		return MaskAddr(a.Addr()) + ":" + fmt.Sprint(a.Port())
	}
	return "masked"
}
