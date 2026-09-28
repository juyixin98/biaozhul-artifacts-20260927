// Package ids 生成本地唯一标识（无外部依赖）。
package ids

import (
	"crypto/rand"
	"encoding/hex"
)

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// New 生成带前缀的 ID，如 app_3f9a...。
func New(prefix string) string { return prefix + "_" + randHex(6) }
