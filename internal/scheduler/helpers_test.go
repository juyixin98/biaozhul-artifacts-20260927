package scheduler_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"placer/internal/model"
	"placer/internal/scheduler"
)

// testdata fixture mirror (kept local to tests so scenarios are explicit).
type fixture struct {
	Name      string           `json:"name"`
	Nodes     []model.Node     `json:"nodes"`
	Instances []model.Instance `json:"instances"`
	Policy    model.Policy     `json:"policy"`
}

func loadFixture(t *testing.T, name string) fixture {
	t.Helper()
	path := filepath.Join("..", "..", "test", "testdata", name)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	var fx fixture
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatalf("parse fixture %s: %v", name, err)
	}
	return fx
}

func res(cpu, mem int64) model.Resources {
	return model.Resources{MilliCPU: cpu, Memory: mem, Storage: 10000000000}
}

func pending(id string, r model.Resources, groups map[string]string) model.Instance {
	return model.Instance{ID: id, Request: r, Groups: groups, State: model.StatePending}
}

// boundView splits a fixture into nodes, pending instances and bindings.
func boundView(fx fixture) ([]model.Node, []model.Instance, []model.Binding) {
	var p []model.Instance
	var b []model.Binding
	for _, in := range fx.Instances {
		switch in.State {
		case model.StateBound:
			b = append(b, model.Binding{
				InstanceID: in.ID, NodeID: in.NodeID,
				Request: in.Request, Groups: in.Groups,
			})
		default:
			in.State = model.StatePending
			p = append(p, in)
		}
	}
	return fx.Nodes, p, b
}

func mustPlan(t *testing.T, req model.PlanRequest) *model.PlanResult {
	t.Helper()
	res, err := scheduler.Plan(req)
	if err != nil {
		t.Fatalf("scheduler.Plan returned error: %v", err)
	}
	return res
}

func findDecision(t *testing.T, r *model.PlanResult, instID string) string {
	t.Helper()
	for _, d := range r.Decisions {
		if d.InstanceID == instID {
			return d.NodeID
		}
	}
	t.Fatalf("no decision for instance %q", instID)
	return ""
}

func conflictCodes(r *model.PlanResult) map[string]model.RejectCode {
	out := map[string]model.RejectCode{}
	for _, c := range r.Conflicts {
		out[c.InstanceID] = c.Code
	}
	return out
}
