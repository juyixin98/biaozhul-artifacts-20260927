// Package portalloc hands out external source ports for NAT mappings.
//
// Invariants:
//   - one pool per protocol (TCP/UDP get independent number spaces);
//   - a held port is never handed out twice while its mapping is active, nor
//     during the post-release cooldown;
//   - allocation is deterministic: the lowest free port in range is chosen,
//     so replayed traces always produce the same mapping and a released port
//     is observably reusable at once (subject to cooldown).
package portalloc

import (
	"sync"
	"time"

	"natlab/internal/model"
)

// Pool allocates ports of one protocol from a fixed range.
type Pool struct {
	proto    model.Protocol
	min, max uint16
	cooldown time.Duration

	mu    sync.Mutex
	used  map[uint16]bool      // ports owned by active mappings
	freed map[uint16]time.Time // port -> earliest realloc time
}

// NewPool creates a pool over [min, max] inclusive.
func NewPool(proto model.Protocol, min, max uint16, cooldown time.Duration) *Pool {
	return &Pool{
		proto: proto, min: min, max: max,
		cooldown: cooldown,
		used:     map[uint16]bool{},
		freed:    map[uint16]time.Time{},
	}
}

// Alloc returns the lowest free port at instant now. It returns ok=false
// (resource exhaustion) only when every port in range is active or cooling.
func (p *Pool) Alloc(now time.Time) (uint16, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()

	for i := uint32(p.min); i <= uint32(p.max); i++ {
		port := uint16(i)
		if p.used[port] {
			continue
		}
		if until, cooling := p.freed[port]; cooling && now.Before(until) {
			continue
		}
		p.used[port] = true
		delete(p.freed, port)
		return port, true
	}
	return 0, false
}

// Hold marks a port active without allocating one (used when restoring state).
func (p *Pool) Hold(port uint16) {
	p.mu.Lock()
	p.used[port] = true
	delete(p.freed, port)
	p.mu.Unlock()
}

// Release returns a port from an active mapping. With cooldown zero it becomes
// allocatable immediately; otherwise it is held until now+cooldown.
func (p *Pool) Release(port uint16, now time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.used, port)
	if p.cooldown > 0 {
		p.freed[port] = now.Add(p.cooldown)
	} else {
		delete(p.freed, port)
	}
}

// Stats returns (active, cooling) counts for logs/assertions.
func (p *Pool) Stats(now time.Time) (active, cooling int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for port := range p.freed {
		if now.Before(p.freed[port]) {
			cooling++
		} else {
			delete(p.freed, port)
		}
	}
	return len(p.used), cooling
}

// Size reports the inclusive range size.
func (p *Pool) Size() int { return int(p.max) - int(p.min) + 1 }
