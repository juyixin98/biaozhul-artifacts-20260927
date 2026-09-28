package director

import (
	"context"
	"testing"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

func minimalScenario() *Scenario {
	return &Scenario{
		Name:  "unit",
		Nodes: []protocol.NodeID{"n1", "n2", "n3"},
		Accounts: []protocol.Account{
			{ID: "a", Owner: "n1", Balance: 10},
			{ID: "d", Owner: "n2", Balance: 10},
			{ID: "g", Owner: "n3", Balance: 10},
		},
	}
}

func wiredCluster(sc *Scenario) *Cluster {
	c := NewCluster(sc)
	c.Transfer = func(context.Context, protocol.NodeID, protocol.Transfer) error { return nil }
	c.Snapshot = func(context.Context, protocol.NodeID, protocol.SnapshotID) error { return nil }
	c.Flush = func(context.Context, protocol.NodeID, protocol.NodeID) (int, error) { return 0, nil }
	return c
}

func TestUnknownStepIsInputError(t *testing.T) {
	sc := minimalScenario()
	sc.Steps = []Step{{Kind: "teleport"}}
	err := wiredCluster(sc).Run(context.Background(), sc)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindInput || ae.Code != apperr.CodeUnknownScenario {
		t.Fatalf("got %v, want input_error/unknown_scenario", err)
	}
}

func TestExpectErrorAnnotationMismatchFails(t *testing.T) {
	sc := minimalScenario()
	// Step succeeds but is annotated to expect state_conflict: must be a
	// malformed-fixture input error rather than a silent pass.
	sc.Steps = []Step{{Kind: StepSnapshot, Node: "n1", Snapshot: "S1", ExpectError: "state_conflict"}}
	err := wiredCluster(sc).Run(context.Background(), sc)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindInput {
		t.Fatalf("got %v, want input_error for wrong expect_error annotation", err)
	}
}

func TestExpectErrorMatchingKindPasses(t *testing.T) {
	sc := minimalScenario()
	c := NewCluster(sc)
	c.Transfer = func(context.Context, protocol.NodeID, protocol.Transfer) error { return nil }
	c.Flush = func(context.Context, protocol.NodeID, protocol.NodeID) (int, error) { return 0, nil }
	seen := map[protocol.SnapshotID]bool{}
	c.Snapshot = func(_ context.Context, _ protocol.NodeID, s protocol.SnapshotID) error {
		if seen[s] {
			return apperr.Conflict(apperr.CodeSnapshotInProgress, "duplicate")
		}
		seen[s] = true
		return nil
	}
	sc.Steps = []Step{
		{Kind: StepSnapshot, Node: "n1", Snapshot: "S1"},
		{Kind: StepSnapshot, Node: "n1", Snapshot: "S1", ExpectError: "state_conflict"},
	}
	if err := c.Run(context.Background(), sc); err != nil {
		t.Fatalf("expected annotated conflict to be accepted, got %v", err)
	}
}

func TestPumpUnknownChannelIsInputError(t *testing.T) {
	sc := minimalScenario()
	sc.Steps = []Step{{Kind: StepPump, Src: "n1", Dst: "n9", N: -1}}
	err := wiredCluster(sc).Run(context.Background(), sc)
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindInput || ae.Code != apperr.CodeUnknownPeer {
		t.Fatalf("got %v, want input_error/unknown_peer", err)
	}
}
