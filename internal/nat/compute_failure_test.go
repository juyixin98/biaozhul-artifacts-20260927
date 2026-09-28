package nat_test

import (
	"context"
	"errors"
	"testing"

	"natlab/internal/memstore"
	"natlab/internal/nat"
)

// TestComputeFailureDistinguishable forces the store's next write to fail and
// asserts the engine surfaces a *ComputeError (HTTP 500 class) rather than a
// model rejection.
func TestComputeFailureDistinguishable(t *testing.T) {
	cfg := testCfg()
	st := memstore.New()
	eng := nat.NewEngine(cfg, st)

	st.FailNextWrite() // the very first SetClock write errors
	res, err := eng.Evaluate(context.Background(), "fail-run",
		udpOut(mustTS(t, 0), "10.1.1.1", 1000, "198.51.100.1", 53))
	if err == nil {
		t.Fatalf("expected compute error, got result=%+v", res)
	}
	var ce *nat.ComputeError
	if !errors.As(err, &ce) {
		t.Fatalf("error %v is not *ComputeError", err)
	}
	// A compute failure must not be confused with a model rejection: no
	// accepted/rejected decision should be returned.
	if res != nil {
		t.Fatalf("compute failure must yield a nil result, got %+v", res)
	}
}
