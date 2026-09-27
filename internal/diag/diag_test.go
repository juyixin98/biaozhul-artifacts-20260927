package diag

import (
	"strings"
	"testing"
)

func TestFingerprintNeverContainsPayload(t *testing.T) {
	secret := []byte("password=hunter2")
	fp := FingerprintPayload(secret, len(secret))
	if fp.Length != len(secret) {
		t.Fatal("length mismatch")
	}
	if len(fp.SHA256) != 64 {
		t.Fatal("sha256 hex must be 64 chars")
	}
	if strings.Contains(fp.SHA256, "hunter2") {
		t.Fatal("payload leaked into fingerprint")
	}
	if fp.Preview == "" {
		t.Fatal("preview must render printable bytes (still no secret hash)")
	}
}

func TestMaskPreviewReplacesControlBytes(t *testing.T) {
	got := MaskPreview([]byte{'a', 0x00, '\n', 0x7f, 'z'})
	if got != "a...z" {
		t.Fatalf("masking wrong: %q", got)
	}
}

func TestRecordTextMasksIPs(t *testing.T) {
	r := Record{
		Decision: Accepted, Category: CatInOrderData,
		Src: "203.0.113.5:41234", Dst: "10.0.0.1:8080",
		Reason: "x",
	}
	txt := r.Text(true)
	if strings.Contains(txt, "203.0.113.5") {
		t.Fatalf("full source IP must be masked, got %s", txt)
	}
	if !strings.Contains(txt, "/24") {
		t.Fatalf("masked text should retain /24 for correlation, got %s", txt)
	}
}

func TestRecordTextCarriesRequestAndState(t *testing.T) {
	r := Record{
		RequestID: "req-42", RecordID: "pkt-7", FlowKey: "f",
		Direction: "c2s", Decision: Rejected, Category: CatOutsideWindow,
		Reason: "far", State: SeqState{Generation: 2, RcvNxtAbs: 99, DeliveredOffset: 40},
	}
	txt := r.Text(false)
	for _, want := range []string{"req=req-42", "pkt=pkt-7", "REJECTED_OUTSIDE_WINDOW", "gen=2", "nxt=99", "deliv=40"} {
		if !strings.Contains(txt, want) {
			t.Fatalf("text line %q missing %q", txt, want)
		}
	}
}
