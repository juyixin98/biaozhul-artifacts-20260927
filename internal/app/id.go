package app

import (
	"crypto/rand"
	"encoding/hex"
)

func newRequestID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "req-" + hex.EncodeToString(b)
}
