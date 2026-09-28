package diag

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestRedactLabelsMasksSensitiveValues(t *testing.T) {
	in := map[string]string{
		"app":           "billing",
		"db_password":   "supersecret-123",
		"api-token":     "tok_live_abc",
		"authorization": "Bearer zzz",
		"tier":          "backend",
		"privateKey":    "RSA-PRIVATE-DATA",
		"safe":          "value",
	}
	out := RedactLabels(in)
	if out["db_password"] != Redacted || out["api-token"] != Redacted ||
		out["authorization"] != Redacted || out["privateKey"] != Redacted {
		t.Fatalf("sensitive values not masked: %+v", out)
	}
	if out["app"] != "billing" || out["tier"] != "backend" || out["safe"] != "value" {
		t.Fatalf("non-sensitive values altered: %+v", out)
	}
	// Original map must not be mutated.
	if in["db_password"] != "supersecret-123" {
		t.Fatal("RedactLabels mutated the caller's map")
	}
	// No secret material may appear in the JSON log form.
	b, _ := json.Marshal(out)
	for _, leak := range []string{"supersecret-123", "tok_live_abc", "Bearer zzz", "RSA-PRIVATE-DATA"} {
		if bytes.Contains(b, []byte(leak)) {
			t.Fatalf("redacted map JSON leaks %q: %s", leak, b)
		}
	}
}

func TestRedactLabelsDoesNotLeakInLogs(t *testing.T) {
	var buf bytes.Buffer
	log := NewLogger("info", &buf)
	labels := map[string]string{"app": "web", "secretToken": "LEAKMARKER-XYZ"}
	log.Info("endpoint snapshot", "labels", RedactLabels(labels))
	if strings.Contains(buf.String(), "LEAKMARKER-XYZ") {
		t.Fatalf("log line leaks sensitive value: %s", buf.String())
	}
	if !strings.Contains(buf.String(), Redacted) {
		t.Fatalf("log line should contain redaction marker: %s", buf.String())
	}
}

func TestIsSensitiveKey(t *testing.T) {
	for _, k := range []string{"password", "userPasswordHash", "TOKEN", "credentials", "private_key", "apiKey"} {
		if !IsSensitiveKey(k) {
			t.Errorf("key %q should be treated sensitive", k)
		}
	}
	for _, k := range []string{"app", "env", "tier", "namespace"} {
		if IsSensitiveKey(k) {
			t.Errorf("key %q should not be treated sensitive", k)
		}
	}
}
