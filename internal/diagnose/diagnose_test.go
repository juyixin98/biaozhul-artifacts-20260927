package diagnose

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"tcpreplay/internal/reassembly"
)

func TestRedactionDefaultsHidePayload(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(&buf, false)
	got := l.Redact([]byte("secret"))
	if !strings.Contains(got, "6 bytes") || strings.Contains(got, "secret") {
		t.Fatalf("redaction leaked payload: %q", got)
	}

	l.Event(reassembly.Event{
		Seq: 1, RequestID: "req-42", RecordID: "rec-7", Code: reassembly.EvSegmentAccepted,
		Level: reassembly.LevelInfo, Flow: "f", Direction: "a_to_b",
		Msg: "accepted", PayloadPreview: "736563726574", PreviewTotal: 6,
	})
	line := buf.String()
	if strings.Contains(line, "736563726574") {
		t.Fatalf("payload hex leaked into default log: %s", line)
	}
	if !strings.Contains(line, "<redacted>") || !strings.Contains(line, "req-42") ||
		!strings.Contains(line, "rec-7") {
		t.Fatalf("log missing redaction marker or correlation ids: %s", line)
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(line)), &parsed); err != nil {
		t.Fatalf("log line not JSON: %v", err)
	}
}

func TestExplicitPreviewShowsCappedHex(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(&buf, true)
	got := l.Redact(make([]byte, 40))
	if !strings.Contains(got, "40 bytes total") {
		t.Fatalf("preview should report total length, got %q", got)
	}
	if hex := strings.TrimSuffix(strings.SplitN(got, "…", 2)[0], ""); len(hex) != 32 {
		t.Fatalf("preview should cap at 16 bytes (32 hex chars), got %d: %q", len(hex), got)
	}
}

func TestDecisionExplanationPresent(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(&buf, false)
	l.Event(reassembly.Event{
		Seq: 2, RequestID: "r", Code: reassembly.EvDataAfterFIN,
		Level: reassembly.LevelReject, Msg: "x",
	})
	if !strings.Contains(buf.String(), "reject:") {
		t.Fatalf("reject explanation missing: %s", buf.String())
	}
}
