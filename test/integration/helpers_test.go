package integration

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"admission/internal/types"
)

func num(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	}
	return -1
}

func jsonBytes(v any) ([]byte, error) { return json.Marshal(v) }

// waitForAttempts polls the durable audit trail until the uid has want events
// in the given category, or fails the test after the deadline.
func waitForAttempts(t *testing.T, a *app, uid string, cat types.Category, want int, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		events, err := a.store.RecentAudit(context.Background(), 50)
		if err != nil {
			t.Fatal(err)
		}
		var n int
		for _, ev := range events {
			if ev.UID == uid && ev.Category == cat {
				n++
			}
		}
		if n >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d %s attempts for %s", want, cat, uid)
}
