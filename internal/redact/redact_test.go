package redact_test

import (
	"strings"
	"testing"

	"igmpq/internal/redact"
)

func TestIPv4Masked(t *testing.T) {
	if got := redact.IP("10.0.0.1"); got != "10.0.0.x" {
		t.Fatalf("got %q, want 10.0.0.x", got)
	}
	if got := redact.IP("192.168.100.200"); got != "192.168.100.x" {
		t.Fatalf("got %q, want 192.168.100.x", got)
	}
}

func TestIPv6Masked(t *testing.T) {
	full := "2001:db8:85a3:8d3:1319:8a2e:370:7348"
	got := redact.IP(full)
	if got == full || !strings.HasPrefix(got, "2001:db8:85a3:8d3") {
		t.Fatalf("got %q, want truncated prefix of %s", got, full)
	}
	if strings.Contains(got, "7348") {
		t.Fatalf("got %q still contains the tail of the address", got)
	}
}

func TestNonIPUnchanged(t *testing.T) {
	if got := redact.IP("not-an-ip"); got != "not-an-ip" {
		t.Fatalf("got %q, want unchanged", got)
	}
	if got := redact.IP(""); got != "" {
		t.Fatalf("got %q, want empty", got)
	}
}
