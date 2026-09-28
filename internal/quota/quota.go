// Package quota defines the external-actor adapter the validators depend on:
// a local, in-memory capacity ledger seeded from fixtures. No production
// account or remote system is contacted.
package quota

import (
	"fmt"
	"sync"

	"admission/internal/quantity"
	"admission/internal/types"
)

// Adapter is the capacity-checking dependency. Reserve is idempotent under
// (uid): reserving the same uid twice consumes no additional capacity and
// returns the same result — this is what makes duplicate calls safe.
type Adapter interface {
	Check(cpuMilli int, memoryBytes int64) error
	Reserve(uid string, cpuMilli int, memoryBytes int64) error
	Release(uid string)
	Used() (cpuMilli int, memoryBytes int64)
}

// ExhaustedError is the adapter-side resource-exhaustion contract.
type ExhaustedError struct {
	RequestCPU    int
	RequestMemory int64
	UsedCPU       int
	UsedMemory    int64
	CapacityCPU   int
	CapacityMem   int64
	Resource      string // "cpu" | "memory"
}

func (e *ExhaustedError) Error() string {
	return fmt.Sprintf("quota exhausted on %s: request exceeds capacity", e.Resource)
}

// MemoryLedger is the synthetic adapter.
type MemoryLedger struct {
	mu         sync.Mutex
	capCPU     int
	capMemory  int64
	usedCPU    int
	usedMemory int64
	reserved   map[string]reservation
}

type reservation struct {
	cpu    int
	memory int64
}

// NewMemoryLedger constructs a ledger with the given capacity (millicores and
// bytes). Zero/negative capacities create a closed ledger.
func NewMemoryLedger(capCPU int, capMemory int64) *MemoryLedger {
	return &MemoryLedger{
		capCPU:    capCPU,
		capMemory: capMemory,
		reserved:  map[string]reservation{},
	}
}

// Check reports whether the requested shape would fit right now, without
// reserving it.
func (l *MemoryLedger) Check(cpuMilli int, memoryBytes int64) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if cpuMilli > l.capCPU-l.usedCPU {
		return &ExhaustedError{
			RequestCPU: cpuMilli, RequestMemory: memoryBytes,
			UsedCPU: l.usedCPU, UsedMemory: l.usedMemory,
			CapacityCPU: l.capCPU, CapacityMem: l.capMemory, Resource: "cpu",
		}
	}
	if memoryBytes > l.capMemory-l.usedMemory {
		return &ExhaustedError{
			RequestCPU: cpuMilli, RequestMemory: memoryBytes,
			UsedCPU: l.usedCPU, UsedMemory: l.usedMemory,
			CapacityCPU: l.capCPU, CapacityMem: l.capMemory, Resource: "memory",
		}
	}
	return nil
}

// Reserve atomically checks and books capacity for uid.
func (l *MemoryLedger) Reserve(uid string, cpuMilli int, memoryBytes int64) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.reserved[uid]; ok {
		return nil // idempotent: duplicate reservation is a no-op
	}
	if cpuMilli > l.capCPU-l.usedCPU {
		return &ExhaustedError{
			RequestCPU: cpuMilli, RequestMemory: memoryBytes,
			UsedCPU: l.usedCPU, UsedMemory: l.usedMemory,
			CapacityCPU: l.capCPU, CapacityMem: l.capMemory, Resource: "cpu",
		}
	}
	if memoryBytes > l.capMemory-l.usedMemory {
		return &ExhaustedError{
			RequestCPU: cpuMilli, RequestMemory: memoryBytes,
			UsedCPU: l.usedCPU, UsedMemory: l.usedMemory,
			CapacityCPU: l.capCPU, CapacityMem: l.capMemory, Resource: "memory",
		}
	}
	l.usedCPU += cpuMilli
	l.usedMemory += memoryBytes
	l.reserved[uid] = reservation{cpu: cpuMilli, memory: memoryBytes}
	return nil
}

// Release frees a uid's booking.
func (l *MemoryLedger) Release(uid string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if r, ok := l.reserved[uid]; ok {
		l.usedCPU -= r.cpu
		l.usedMemory -= r.memory
		delete(l.reserved, uid)
	}
}

// Used returns the currently consumed capacity.
func (l *MemoryLedger) Used() (int, int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.usedCPU, l.usedMemory
}

// RequestFromObject parses cpu/memory of an object into ledger units.
func RequestFromObject(obj types.Object) (int, int64, error) {
	cpu, err := quantity.ParseCPU(obj.Spec.CPU)
	if err != nil {
		return 0, 0, err
	}
	mem, err := quantity.ParseMemory(obj.Spec.Memory)
	if err != nil {
		return 0, 0, err
	}
	return cpu, mem, nil
}
