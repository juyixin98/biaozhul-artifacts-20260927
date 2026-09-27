package portalloc

import (
	"testing"
	"time"

	"natlab/internal/model"
)

func t0() time.Time { return time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC) }

func TestAllocDeterministicInRange(t *testing.T) {
	p := NewPool(model.TCP, 20000, 20002, 0)
	want := []uint16{20000, 20001, 20002}
	got := make([]uint16, 0, 3)
	for range want {
		port, ok := p.Alloc(t0())
		if !ok {
			t.Fatalf("unexpected exhaustion")
		}
		got = append(got, port)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("alloc order = %v, want %v", got, want)
		}
	}
}

func TestExhaustionAndReuseAfterRelease(t *testing.T) {
	p := NewPool(model.UDP, 30000, 30001, 0)
	a, ok := p.Alloc(t0())
	if !ok || a != 30000 {
		t.Fatalf("first alloc = %d,%v", a, ok)
	}
	b, ok := p.Alloc(t0())
	if !ok || b != 30001 {
		t.Fatalf("second alloc = %d,%v", b, ok)
	}
	if _, ok := p.Alloc(t0()); ok {
		t.Fatal("expected port_pool exhaustion while all ports held")
	}
	if active, _ := p.Stats(t0()); active != 2 {
		t.Fatalf("active = %d, want 2", active)
	}
	// Release one: exactly that range now has one allocatable port.
	p.Release(a, t0())
	if active, _ := p.Stats(t0()); active != 1 {
		t.Fatalf("active after release = %d, want 1", active)
	}
	c, ok := p.Alloc(t0())
	if !ok || c != a {
		t.Fatalf("reallocation = %d,%v, want %d,true", c, ok, a)
	}
}

func TestActivePortNeverReused(t *testing.T) {
	// The central NAPT guarantee: a held port must not be handed to a second
	// flow, even when the cursor wraps across the whole range.
	p := NewPool(model.TCP, 20000, 20004, 0)
	held, ok := p.Alloc(t0()) // holds 20000
	if !ok {
		t.Fatal("alloc")
	}
	for range 4 { // exhaust all other four ports
		if _, ok := p.Alloc(t0()); !ok {
			t.Fatal("alloc remaining")
		}
	}
	if _, ok := p.Alloc(t0()); ok {
		t.Fatal("must not reuse any active port after full scan")
	}
	// Determinism: same allocation sequence on a fresh pool gives the same port.
	q := NewPool(model.TCP, 20000, 20004, 0)
	again, _ := q.Alloc(t0())
	if again != held {
		t.Fatalf("fresh pool allocated %d, want deterministic %d", again, held)
	}
}

func TestCooldownBlocksReuseThenUnblocks(t *testing.T) {
	now := t0()
	p := NewPool(model.UDP, 40000, 40000, 5*time.Second)
	port, _ := p.Alloc(now)
	p.Release(port, now)

	// Immediately after release, the port is cooling down => exhausted.
	if _, ok := p.Alloc(now); ok {
		t.Fatal("reused port during cooldown window")
	}
	later := now.Add(5 * time.Second)
	got, ok := p.Alloc(later)
	if !ok || got != port {
		t.Fatalf("after cooldown alloc = %d,%v, want %d,true", got, ok, port)
	}
}

func TestPoolsAreIndependentPerProtocol(t *testing.T) {
	tp := NewPool(model.TCP, 50000, 50000, 0)
	up := NewPool(model.UDP, 50000, 50000, 0)
	tport, tok := tp.Alloc(t0())
	uport, uok := up.Alloc(t0())
	if !tok || !uok || tport != uport {
		t.Fatalf("TCP and UDP number spaces must be independent: %d,%v %d,%v",
			tport, tok, uport, uok)
	}
}
