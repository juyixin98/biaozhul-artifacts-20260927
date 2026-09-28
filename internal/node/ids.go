package node

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
)

// newMsgID produces a unique message id without depending on the per-channel
// seq (seq is assigned atomically at enqueue time).
func newMsgID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "msg-" + hex.EncodeToString(b[:])
}

// debugJSON is a small helper used in tests/errors paths.
func debugJSON(v interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(b)
}
