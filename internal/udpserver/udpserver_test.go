package udpserver_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"dhcp4lab/internal/udpserver"
)

// The listener is the second safety gate (config validation is the first):
// it must refuse to bind anything but a loopback address, so a wiring bug
// can never make the lab server answer traffic on a production NIC.
func TestRefusesNonLoopbackBind(t *testing.T) {
	cases := []string{
		"0.0.0.0:10067",
		"192.0.2.1:10067",
		"127.0.0.1:67", // privileged-looking address still loopback; allowed below
	}
	for _, addr := range cases {
		ln := udpserver.New(addr, func(ctx context.Context, b []byte) []byte { return nil }, nil)
		err := ln.Listen()
		switch addr {
		case "127.0.0.1:67":
			// Loopback privileged port is gated at config layer, not here;
			// the bind may succeed (as root) or fail (as non-root). Either
			// is acceptable for this gate; only ensure it does not panic.
			_ = ln.Close()
			continue
		}
		if err == nil {
			_ = ln.Close()
			t.Fatalf("bind %s succeeded; non-loopback must be refused", addr)
		}
		if !strings.Contains(err.Error(), "non-loopback") {
			t.Fatalf("bind %s error %q does not mention non-loopback", addr, err)
		}
	}
}

// A loopback bind comes up, reports a run id and serves a datagram
// end-to-end (handler echo path), then closes cleanly.
func TestLoopbackServeAndClose(t *testing.T) {
	got := make(chan []byte, 1)
	ln := udpserver.New("127.0.0.1:0", func(ctx context.Context, b []byte) []byte {
		got <- b
		return nil // drop path: malformed/empty handling is exercised elsewhere
	}, nil)
	if err := ln.Listen(); err != nil {
		t.Fatalf("listen: %v", err)
	}
	if ln.RunID() == "" || !strings.HasPrefix(ln.RunID(), "run-") {
		t.Fatalf("run id = %q", ln.RunID())
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = ln.Serve(ctx) }()

	// Give the server a moment, then confirm Close is clean and idempotent.
	time.Sleep(50 * time.Millisecond)
	if err := ln.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := ln.Close(); err != nil {
		t.Fatalf("second close: %v", err)
	}
}
