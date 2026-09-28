package quota

import (
	"errors"
	"testing"
)

func TestReserveIdempotentAndExhaustion(t *testing.T) {
	l := NewMemoryLedger(1000, 1<<30)

	if err := l.Reserve("u1", 600, 128<<20); err != nil {
		t.Fatalf("first reserve: %v", err)
	}
	// Duplicate reservation for same uid consumes nothing.
	if err := l.Reserve("u1", 600, 128<<20); err != nil {
		t.Fatalf("duplicate reserve: %v", err)
	}
	cpu, _ := l.Used()
	if cpu != 600 {
		t.Fatalf("duplicate reserve consumed capacity: used cpu=%d want 600", cpu)
	}

	// Another 600m no longer fits (capacity 1000, used 600).
	err := l.Reserve("u2", 600, 1)
	var ex *ExhaustedError
	if !errors.As(err, &ex) {
		t.Fatalf("expected ExhaustedError, got %v", err)
	}
	if ex.Resource != "cpu" {
		t.Fatalf("expected cpu exhaustion, got %s", ex.Resource)
	}
	if ex.CapacityCPU != 1000 || ex.UsedCPU != 600 || ex.RequestCPU != 600 {
		t.Fatalf("exhaustion detail wrong: %+v", ex)
	}

	// A fitting reservation succeeds and release frees capacity.
	if err := l.Reserve("u3", 400, 1); err != nil {
		t.Fatalf("fitting reserve: %v", err)
	}
	l.Release("u3")
	cpu, _ = l.Used()
	if cpu != 600 {
		t.Fatalf("release did not free capacity: used cpu=%d", cpu)
	}
}

func TestMemoryExhaustion(t *testing.T) {
	l := NewMemoryLedger(100000, 100)
	err := l.Reserve("u1", 1, 200)
	var ex *ExhaustedError
	if !errors.As(err, &ex) || ex.Resource != "memory" {
		t.Fatalf("expected memory ExhaustedError, got %v", err)
	}
}

func TestCheckDoesNotConsume(t *testing.T) {
	l := NewMemoryLedger(100, 100)
	if err := l.Check(50, 50); err != nil {
		t.Fatalf("check: %v", err)
	}
	cpu, mem := l.Used()
	if cpu != 0 || mem != 0 {
		t.Fatalf("Check must not book capacity, got cpu=%d mem=%d", cpu, mem)
	}
}
