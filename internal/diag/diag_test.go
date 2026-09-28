package diag

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestSensitiveKeyRedaction(t *testing.T) {
	meta := map[string]string{
		"community":   "private-public-as",
		"api-token":   "abc123",
		"peer-secret": "shh",
		"description": "uplink to rack B",
		"password":    "hunter2",
	}
	got := RedactMeta(meta)
	for k, v := range got {
		if IsSensitiveKey(k) && v != Redacted {
			t.Errorf("key %q leaked value %q", k, v)
		}
	}
	if got["description"] != "uplink to rack B" {
		t.Errorf("non-sensitive value changed: %q", got["description"])
	}
}

func TestRequestIDFormat(t *testing.T) {
	id := NewRequestID()
	if !strings.HasPrefix(id, "req-") || len(id) < 10 {
		t.Fatalf("request id looks wrong: %q", id)
	}
	if NewRequestID() == id {
		t.Fatal("request ids must be unique")
	}
}

func TestLoggerEmitsJSONAndRedactState(t *testing.T) {
	var buf bytes.Buffer
	lg := NewLogger(&buf)
	lg.Log(Record{
		RequestID: "req-x", Event: "lookup", Verdict: "rejected",
		TableVer: 3, Target: "10.0.0.1", Detail: "loop",
		State: map[string]any{"meta": RedactMeta(map[string]string{"token": "leak-me"})},
	})
	lg.Close()
	line := strings.TrimSpace(buf.String())
	if !strings.Contains(line, `"request_id":"req-x"`) || !strings.Contains(line, `"verdict":"rejected"`) {
		t.Fatalf("record missing fields: %s", line)
	}
	if strings.Contains(line, "leak-me") {
		t.Fatal("sensitive value leaked into log")
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(line), &parsed); err != nil {
		t.Fatalf("log line not valid JSON: %v", err)
	}
}
