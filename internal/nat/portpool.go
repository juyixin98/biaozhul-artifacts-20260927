package nat

import (
	"container/heap"
)

// PortPool hands out deterministic lowest-free external ports. Allocated
// ports are never handed to a second active mapping: a port is reusable only
// after Release returns it, and Release is called exclusively for mappings
// that have expired or been closed. The pool is shared between TCP and UDP,
// matching a device with one external address and one port number space.
type PortPool struct {
	low, high int
	next      int          // lowest never-issued candidate
	used      map[int]bool // ports currently owned by an active mapping
	free      minHeap      // released holes, reusable first
}

// NewPortPool builds a pool over the inclusive range [low, high].
func NewPortPool(low, high uint16) *PortPool {
	return &PortPool{low: int(low), high: int(high), next: int(low), used: map[int]bool{}}
}

// Allocate returns the lowest currently free port, or 0 with false when the
// pool is exhausted.
func (p *PortPool) Allocate() (uint16, bool) {
	// Reuse a released hole first; skip entries that are somehow still owned.
	for p.free.Len() > 0 {
		cand := heap.Pop(&p.free).(int)
		if p.used[cand] {
			continue
		}
		p.used[cand] = true
		return uint16(cand), true
	}
	// Issue a never-used port, advancing past externally burned ports.
	for p.next <= p.high {
		port := p.next
		p.next++
		if p.used[port] {
			continue
		}
		p.used[port] = true
		return uint16(port), true
	}
	return 0, false
}

// Release returns a port to the pool after its mapping expired or closed.
func (p *PortPool) Release(port uint16) {
	v := int(port)
	if v < p.low || v > p.high || !p.used[v] {
		return
	}
	delete(p.used, v)
	heap.Push(&p.free, v)
}

// Burn marks an already-allocated port as owned (used when reconstructing the
// pool from persisted active mappings).
func (p *PortPool) Burn(port uint16) {
	v := int(port)
	if v < p.low || v > p.high {
		return
	}
	if p.used[v] {
		return
	}
	p.used[v] = true
	// A port that was released into the free heap must no longer be handed out.
	for i, cand := range p.free {
		if cand == v {
			heap.Remove(&p.free, i)
			break
		}
	}
}

// Size is the configured pool capacity.
func (p *PortPool) Size() int { return p.high - p.low + 1 }

// ActiveEstimate is the number of currently owned ports; tests use it for
// invariant checks.
func (p *PortPool) ActiveEstimate() int { return len(p.used) }

// minHeap is a min-heap of released ports.
type minHeap []int

func (h minHeap) Len() int           { return len(h) }
func (h minHeap) Less(i, j int) bool { return h[i] < h[j] }
func (h minHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *minHeap) Push(x any)        { *h = append(*h, x.(int)) }
func (h *minHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	*h = old[:n-1]
	return x
}
