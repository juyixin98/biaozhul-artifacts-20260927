// Package uid generates resource UIDs. A UID is an incarnation identity:
// it MUST stay unique even after a same-namespace/name row has been
// deleted and recreated, so that owner references pinned to the old
// incarnation never silently resolve against the new one.
package uid

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sync"
	"time"
)

// Generator produces unique UID strings.
type Generator interface {
	NewUID() string
}

// RandomGenerator is the production generator: 48 bits of millisecond
// timestamp + 80 random bits, hex encoded. Collision probability is
// negligible; the storage layer additionally enforces uniqueness.
type RandomGenerator struct{}

// NewRandom returns the production generator.
func NewRandom() *RandomGenerator { return &RandomGenerator{} }

// NewUID implements Generator.
func (g *RandomGenerator) NewUID() string {
	var b [10]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is not recoverable for an identity
		// generator; fall through to a time-derived suffix rather than
		// minting a collision-prone zero UID.
		return fmt.Sprintf("uid-%012x-%020x", time.Now().UnixMilli()&0xffffffffffff, time.Now().UnixNano())
	}
	return fmt.Sprintf("uid-%012x-%s", time.Now().UnixMilli()&0xffffffffffff, hex.EncodeToString(b[:]))
}

// SeededGenerator is a deterministic generator for tests:
// uid-000000000001-<10 hex digits from a per-seed counter>.
// Same seed + same call order yields the same UIDs, which lets test
// logs correlate inputs and assertions.
type SeededGenerator struct {
	mu    sync.Mutex
	seed  uint64
	calls uint64
}

// NewSeeded returns a deterministic generator seeded with seed.
func NewSeeded(seed uint64) *SeededGenerator {
	if seed == 0 {
		seed = 1
	}
	return &SeededGenerator{seed: seed}
}

// NewUID implements Generator.
func (g *SeededGenerator) NewUID() string {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.calls++
	// splitmix64 mixing of seed+calls gives well-distributed hex
	// suffixes while remaining fully deterministic.
	x := g.seed + g.calls*0x9e3779b97f4a7c15
	x ^= x >> 30
	x *= 0xbf58476d1ce4e5b9
	x ^= x >> 27
	x *= 0x94d049bb133111eb
	x ^= x >> 31
	return fmt.Sprintf("uid-%012x-%020x", g.calls, x&0xffffffffffffffff)
}
