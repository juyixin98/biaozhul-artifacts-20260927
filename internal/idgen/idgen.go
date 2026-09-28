// Package idgen generates sortable, collision-free identifiers for messages
// and receipts without external dependencies.
//
// Message IDs are time-sortable (48-bit millisecond timestamp prefix) which
// yields FIFO claim order naturally in both stores. Receipts are 256 bits of
// crypto/rand hex: a receipt is a capability and must be unguessable.
package idgen

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sync/atomic"
	"time"
)

// epoch is the fixed reference instant (2026-01-01 UTC) for id timestamps.
var epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

var counter uint32 // process-local tiebreaker within a millisecond

// MessageID returns a 128-bit sortable id: 48-bit ms timestamp | 16-bit
// monotonic counter | 64 random bits, hex-encoded (32 chars).
func MessageID() (string, error) {
	ms := uint64(time.Now().UTC().Sub(epoch).Milliseconds())
	c := uint16(atomic.AddUint32(&counter, 1))
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("idgen: %w", err)
	}
	var out [16]byte
	out[0] = byte(ms >> 40)
	out[1] = byte(ms >> 32)
	out[2] = byte(ms >> 24)
	out[3] = byte(ms >> 16)
	out[4] = byte(ms >> 8)
	out[5] = byte(ms)
	out[6] = byte(c >> 8)
	out[7] = byte(c)
	copy(out[8:], b[:])
	return hex.EncodeToString(out[:]), nil
}

// Receipt returns a 256-bit random hex receipt (64 chars).
func Receipt() (string, error) {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("idgen: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}
