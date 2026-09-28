package diag

import (
	"context"
	"net/netip"
	"strings"
	"testing"
)

func TestRequestIDGeneratedAndStable(t *testing.T) {
	d := NewContext("", false)
	if d.RequestID == "" || !strings.HasPrefix(d.RequestID, "req-") {
		t.Fatalf("request id=%q", d.RequestID)
	}
	d2 := NewContext("fixed-id", false)
	if d2.RequestID != "fixed-id" {
		t.Fatalf("explicit id not kept: %q", d2.RequestID)
	}
}

func TestNotesCarryWhy(t *testing.T) {
	d := NewContext("r1", false)
	d.Note("accept: route %s chosen", "x")
	d.Note("reject: loop at %s", "y")
	texts := d.EntriesText()
	if len(texts) != 2 || texts[0] != "accept: route x chosen" || texts[1] != "reject: loop at y" {
		t.Fatalf("notes=%v", texts)
	}
	if len(d.Entries()) != 2 || d.Entries()[0].At.IsZero() {
		t.Fatal("entries must carry timestamps")
	}
}

func TestRedactAddr(t *testing.T) {
	cases := []struct{ in, want string }{
		{"203.0.113.77", "203.0.x.x"},
		{"10.255.255.255", "10.255.x.x"},
		{"2001:db8:dead:beef::1", "2001::xxxx"},
		{"::1", "::xxxx"},
	}
	for _, c := range cases {
		a := netip.MustParseAddr(c.in)
		if got := RedactAddr(a); got != c.want {
			t.Errorf("redact(%s)=%q want %q", c.in, got, c.want)
		}
	}
	if got := RedactAddr(netip.Addr{}); got != "<addr>" {
		t.Errorf("zero addr=%q", got)
	}
}

func TestSprintfRedactsAddrArgs(t *testing.T) {
	a := netip.MustParseAddr("198.51.100.9")
	got := SprintfRedacted(true, "lookup %s failed", a)
	if strings.Contains(got, "198.51.100.9") {
		t.Fatalf("sensitive address leaked: %q", got)
	}
	if !strings.Contains(got, "198.51.x.x") {
		t.Fatalf("expected masked form, got %q", got)
	}
	// 关闭脱敏时保留完整地址。
	raw := SprintfRedacted(false, "lookup %s failed", a)
	if !strings.Contains(raw, "198.51.100.9") {
		t.Fatalf("non-redacted output wrong: %q", raw)
	}
}

func TestContextPropagation(t *testing.T) {
	d := NewContext("z", false)
	ctx := IntoContext(context.Background(), d)
	if FromContext(ctx).RequestID != "z" {
		t.Fatal("context round trip failed")
	}
	if FromContext(context.Background()) != nil {
		t.Fatal("absent diag should be nil")
	}
}
