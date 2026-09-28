// Package diagnose renders reassembly decisions as structured, correlatable
// log records. Every line carries the request id (and record id when known)
// plus the key state that motivated the decision: raw and absolute sequence
// span, next contiguous position and FIN position.
//
// Payload safety: stream bytes are application data and are treated as
// sensitive. They are never logged by default. A record contains payload
// bytes only when the operator explicitly enabled previews in configuration;
// even then at most 16 hex bytes are emitted and the full length is given
// instead of the content.
package diagnose

import (
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"

	"tcpreplay/internal/reassembly"
)

// Logger wraps slog with the fields every diagnostic line must carry.
type Logger struct {
	log     *slog.Logger
	preview bool
}

// NewLogger builds a JSON structured logger writing to w (os.Stderr when nil).
func NewLogger(w io.Writer, preview bool) *Logger {
	if w == nil {
		w = os.Stderr
	}
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug})
	return &Logger{log: slog.New(h), preview: preview}
}

// RequestBound returns a child logger. The request id is emitted by Event
// from the event record itself (the authoritative correlation field); this
// method exists so callers can keep one logger per request scope.
func (l *Logger) RequestBound(requestID string) *Logger {
	return &Logger{log: l.log.With("scope", "request:"+requestID), preview: l.preview}
}

func level(lvl reassembly.EventLevel) slog.Level {
	switch lvl {
	case reassembly.LevelReject:
		return slog.LevelWarn
	case reassembly.LevelWarn:
		return slog.LevelWarn
	case reassembly.LevelUndecided:
		return slog.LevelWarn
	default:
		return slog.LevelInfo
	}
}

// Event writes one decision record. Payload content is redacted unless
// previews were explicitly enabled.
func (l *Logger) Event(e reassembly.Event) {
	attrs := []any{
		"request_id", e.RequestID,
		"event_seq", e.Seq,
		"code", string(e.Code),
		"flow", e.Flow,
		"generation", e.Generation,
		"direction", e.Direction,
		"raw_seq", fmt.Sprintf("0x%08x", e.RawSeq),
		"abs_span", fmt.Sprintf("[%d,%d)", e.AbsStart, e.AbsEnd),
		"next_contiguous", e.NextContig,
		"fin_pos", e.FINPos,
		"decision", explain(e.Code),
	}
	if e.RecordID != "" {
		attrs = append(attrs, "record_id", e.RecordID)
	}
	if e.Timestamp != "" {
		attrs = append(attrs, "pcap_ts", e.Timestamp)
	}
	if e.PreviewTotal > 0 {
		if l.preview && e.PayloadPreview != "" {
			attrs = append(attrs, "payload_hex_preview", e.PayloadPreview)
		} else {
			attrs = append(attrs, "payload", "<redacted>")
		}
		attrs = append(attrs, "payload_bytes", e.PreviewTotal)
	}
	l.log.Log(nil, level(e.Level), e.Msg, attrs...)
}

// Redact returns a safe representation of an arbitrary byte slice: when
// previews are disabled it returns "<n bytes>"; otherwise an unprefixed hex
// string capped at 16 bytes. It is the single choke point used by the service.
func (l *Logger) Redact(p []byte) string {
	if len(p) == 0 {
		return ""
	}
	if !l.preview {
		return fmt.Sprintf("<%d bytes>", len(p))
	}
	n := len(p)
	if n > 16 {
		p = p[:16]
	}
	var sb strings.Builder
	sb.WriteString(hex.EncodeToString(p))
	if n > 16 {
		sb.WriteString(fmt.Sprintf("…(%d bytes total)", n))
	}
	return sb.String()
}

// explain states why the code means accept, reject or undecided, in one short
// phrase, so logs are self-describing during review.
func explain(code reassembly.EventCode) string {
	switch code {
	case reassembly.EvSYNOpened, reassembly.EvSYNACKEstablished, reassembly.EvNewGeneration:
		return "accept: handshake state established"
	case reassembly.EvSYNDuplicate, reassembly.EvFINDuplicate:
		return "accept: identical retransmission, no new output"
	case reassembly.EvRetransmitIdent:
		return "accept-no-output: identical data already present"
	case reassembly.EvSegmentAccepted:
		return "accept: new contiguous or buffered bytes"
	case reassembly.EvFINAccepted, reassembly.EvGenerationComplete:
		return "accept: orderly close state"
	case reassembly.EvOverlapConflict:
		return "isolate: overlap conflict handled by configured policy"
	case reassembly.EvDataAfterFIN:
		return "reject: data cannot occupy FIN-or-later sequence numbers"
	case reassembly.EvRSTClosed:
		return "accept: abortive close recorded"
	case reassembly.EvPacketAfterRST:
		return "undecided: post-RST data without reopening SYN is not output"
	case reassembly.EvHandshakeAbsent:
		return "undecided: no SYN observed; offsets relative to first seen byte"
	case reassembly.EvFINConflict:
		return "undecided: contradictory FIN positions retained as evidence"
	default:
		return "see message"
	}
}
