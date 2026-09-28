package logx

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestRedaction(t *testing.T) {
	spec := map[string]any{
		"image":  "widget:1",
		"secret": "do-not-print-me",
		"nested": map[string]any{
			"apiKey":  "abc123",
			"visible": 42,
			"creds":   map[string]any{"password": "hunter2"},
		},
		"items": []any{
			map[string]any{"token": "t-1"},
			map[string]any{"ok": true},
		},
	}
	out := RedactSpec(spec)
	raw := toJSON(out)
	for _, leak := range []string{"do-not-print-me", "abc123", "hunter2", "t-1"} {
		if strings.Contains(raw, leak) {
			t.Fatalf("sensitive value %q leaked into redacted JSON: %s", leak, raw)
		}
	}
	if !strings.Contains(raw, "***REDACTED***") {
		t.Fatalf("expected redaction marker, got %s", raw)
	}
	if !strings.Contains(raw, "widget:1") || !strings.Contains(raw, "visible") {
		t.Fatalf("non-sensitive fields must be preserved: %s", raw)
	}
}

func TestLoggerEmitsRedactedFields(t *testing.T) {
	var buf bytes.Buffer
	l := New(&buf)
	l.Info("test", "spec applied", map[string]any{
		"requestID": "req-1", "secret": "topsecret",
	})
	line := buf.String()
	if strings.Contains(line, "topsecret") {
		t.Fatalf("logger leaked secret: %s", line)
	}
	if !strings.Contains(line, "req-1") || !strings.Contains(line, "spec applied") {
		t.Fatalf("logger dropped non-sensitive fields: %s", line)
	}
}

func toJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
