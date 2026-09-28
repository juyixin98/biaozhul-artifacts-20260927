package coord_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"fieldapply/internal/coord"
	"fieldapply/internal/diag"
	"fieldapply/internal/model"
	"fieldapply/internal/store"
)

func decodeJSON(t *testing.T, raw string) json.RawMessage {
	t.Helper()
	if !json.Valid([]byte(raw)) {
		t.Fatalf("invalid fixture json: %s", raw)
	}
	return json.RawMessage(raw)
}

// liveOf decodes an outcome's live body preserving integer form.
func liveOf(t *testing.T, raw json.RawMessage) map[string]any {
	t.Helper()
	v, err := model.DecodeValue(raw)
	if err != nil {
		t.Fatalf("decode live: %v", err)
	}
	return v.(map[string]any)
}

// End-to-end scenario through load -> pure merge -> commit, including the
// journal: two managers, nested keyed list, delete, conflict and force.
func TestCoordinatorEndToEnd(t *testing.T) {
	var journal bytes.Buffer
	st := store.NewMemory()
	co := coord.New(st, diag.NewLogger(&journal))
	defer co.Close()
	ctx := context.Background()

	schema := model.Schema{Lists: map[string]model.ListKind{
		".spec.tags":       model.ListSet,
		".spec.containers": model.ListKeyed,
	}, Keys: map[string]string{".spec.containers": "name"}}
	initial := decodeJSON(t, `{
		"spec": {
			"replicas": 3,
			"tags": ["x", "y"],
			"containers": [
				{"name": "web", "image": "web:1", "port": 8080},
				{"name": "worker", "image": "worker:1"}
			]
		}
	}`)
	snap, err := st.Create(ctx, "app", initial, initial, "platform", schema)
	if err != nil {
		t.Fatal(err)
	}
	_ = snap

	// autoscaler first adopts replicas at its current value (shares the
	// leaf), then scales it with force (platform still holds a share).
	if _, err = co.Apply(ctx, coord.ApplyRequest{
		ResourceID: "app", Manager: "autoscaler",
		Config: decodeJSON(t, `{"spec":{"replicas":3}}`),
	}); err != nil {
		t.Fatalf("autoscaler adopt: %v", err)
	}
	r1, err := co.Apply(ctx, coord.ApplyRequest{
		ResourceID: "app", Manager: "autoscaler",
		Config: decodeJSON(t, `{"spec":{"replicas":7}}`),
		Force:  true,
		Reason: "scale-up",
	})
	if err != nil {
		t.Fatalf("autoscaler apply: %v", err)
	}
	live := liveOf(t, r1.Live)
	replicas := live["spec"].(map[string]any)["replicas"].(json.Number)
	if replicas.String() != "7" {
		t.Fatalf("replicas = %s, want 7", replicas)
	}

	// netpol adopts web.port at current value then changes it.
	if _, err = co.Apply(ctx, coord.ApplyRequest{
		ResourceID: "app", Manager: "netpol",
		Config: decodeJSON(t, `{"spec":{"containers":[{"name":"web","port":8080}]}}`),
	}); err != nil {
		t.Fatalf("netpol adopt: %v", err)
	}
	r2, err := co.Apply(ctx, coord.ApplyRequest{
		ResourceID: "app", Manager: "netpol",
		Config: decodeJSON(t, `{"spec":{"containers":[{"name":"web","port":9090}]}}`),
		Force:  true,
		Reason: "remap",
	})
	if err != nil {
		t.Fatalf("netpol change: %v", err)
	}
	live = liveOf(t, r2.Live)
	containers := live["spec"].(map[string]any)["containers"].([]any)
	if len(containers) != 2 {
		t.Fatalf("container count = %d, want 2", len(containers))
	}

	// platform deletes worker by omitting it. Its config carries the current
	// values of the fields it still shares (replicas scaled by autoscaler,
	// port remapped by netpol) so those foreign-owned leaves are untouched.
	r3, err := co.Apply(ctx, coord.ApplyRequest{
		ResourceID: "app", Manager: "platform",
		Config: decodeJSON(t, `{"spec":{"replicas":7,"tags":["x","y"],"containers":[
			{"name":"web","image":"web:1","port":9090}
		]}}`),
	})
	if err != nil {
		t.Fatalf("platform delete worker: %v", err)
	}
	live = liveOf(t, r3.Live)
	containers = live["spec"].(map[string]any)["containers"].([]any)
	if len(containers) != 1 || containers[0].(map[string]any)["name"] != "web" {
		t.Fatalf("worker not deleted: %v", containers)
	}
	// netpol's forced port survived platform's narrower config.
	port := containers[0].(map[string]any)["port"].(json.Number)
	if port.String() != "9090" {
		t.Fatalf("foreign-owned port lost: %s", port)
	}
	// unrelated tags still present.
	tags := live["spec"].(map[string]any)["tags"].([]any)
	if len(tags) != 2 {
		t.Fatalf("tags lost: %v", tags)
	}

	// autoscaler (owning replicas) sees unrelated platform scale attempt...
	// conflict path: platform tries replicas without force -> 409 semantics.
	_, err = co.Apply(ctx, coord.ApplyRequest{
		ResourceID: "app", Manager: "platform",
		Config: decodeJSON(t, `{"spec":{"replicas":11}}`),
	})
	var se *model.Error
	if !errors.As(err, &se) || se.Code != "field_conflict" {
		t.Fatalf("want field_conflict, got %v", err)
	}
	if len(se.Conflicts) != 1 || se.Conflicts[0].Path != ".spec.replicas" ||
		len(se.Conflicts[0].Owners) != 1 || se.Conflicts[0].Owners[0] != "autoscaler" {
		t.Fatalf("conflict detail wrong: %+v", se.Conflicts)
	}

	// History is auditable and ordered newest-first.
	hist, err := st.History(ctx, "app", 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(hist) < 4 {
		t.Fatalf("history length = %d, want >=4", len(hist))
	}
	if hist[0].Manager != "platform" || hist[0].Changes.Removed == nil {
		t.Fatalf("latest history entry wrong: %+v", hist[0])
	}
	foundRemoval := false
	for _, ch := range hist[0].Changes.Removed {
		if strings.Contains(ch.Path, `[name="worker"]`) {
			foundRemoval = true
		}
	}
	if !foundRemoval {
		t.Fatalf("worker removal not audited: %+v", hist[0].Changes)
	}

	// Journal: every run id, phases and the conflict decision are recorded.
	lines := strings.Split(strings.TrimSpace(journal.String()), "\n")
	if len(lines) < 5 {
		t.Fatalf("journal too short: %d lines", len(lines))
	}
	runIDs := map[string]bool{}
	sawConflict := false
	for _, line := range lines {
		var ev diag.Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("journal line not json: %v (%s)", err, line)
		}
		if ev.RunID == "" || !strings.HasPrefix(ev.RunID, "r-") {
			t.Fatalf("bad run id %q", ev.RunID)
		}
		runIDs[ev.RunID] = true
		if ev.Phase == "conflict" && ev.Code == "field_conflict" {
			sawConflict = true
			if !strings.Contains(string(ev.Detail), ".spec.replicas") {
				t.Fatalf("conflict detail missing path: %s", ev.Detail)
			}
		}
	}
	if !sawConflict {
		t.Fatalf("no conflict phase in journal")
	}
	if len(runIDs) < 4 {
		t.Fatalf("distinct run ids = %d", len(runIDs))
	}
}

// Queue saturation surfaces as resource_exhausted/queue_full rather than
// blocking forever or growing memory silently.
func TestQueueFullIsResourceExhausted(t *testing.T) {
	base := store.NewMemory()
	slow := &slowStore{Store: base, gate: make(chan struct{})}
	co := coord.New(slow, diag.NewLogger(nil))
	ctx := context.Background()
	if _, err := base.Create(ctx, "r", json.RawMessage(`{}`), json.RawMessage(`{}`), "a", model.Schema{}); err != nil {
		t.Fatal(err)
	}

	// First job enters the single-writer loop and blocks in Commit.
	done0 := make(chan error, 1)
	go func() {
		_, err := co.Apply(ctx, coord.ApplyRequest{
			ResourceID: "r", Manager: "m0", Config: json.RawMessage(`{"v":0}`),
		})
		done0 <- err
	}()
	if !slow.waitEntered(1, time.Second) {
		t.Fatal("first job never entered the loop")
	}

	// Fill the bounded queue (capacity 32) without processing them.
	dones := make([]chan error, 32)
	for i := range dones {
		dones[i] = make(chan error, 1)
		i := i
		go func() {
			_, err := co.Apply(ctx, coord.ApplyRequest{
				ResourceID: "r", Manager: "m-fill", Config: json.RawMessage(`{"v":0}`),
			})
			dones[i] <- err
		}()
	}

	// Give the fill goroutines time to occupy every buffered slot.
	time.Sleep(100 * time.Millisecond)

	// Attempt further applies concurrently: while the queue is full each
	// send is rejected up front (queue_full); any attempt that wins a slot
	// merely blocks until the gate opens below.
	var got int64
	var attemptWg sync.WaitGroup
	attemptCtx, cancelAttempts := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancelAttempts()
	for i := 0; i < 8; i++ {
		attemptWg.Add(1)
		go func() {
			defer attemptWg.Done()
			_, err := co.Apply(attemptCtx, coord.ApplyRequest{
				ResourceID: "r", Manager: "late", Config: json.RawMessage(`{"v":2}`),
			})
			if model.IsCategory(err, model.CatResourceExhausted) {
				var se *model.Error
				errors.As(err, &se)
				if se.Code == "queue_full" {
					atomic.StoreInt64(&got, 1)
				}
			}
		}()
	}
	attemptWg.Wait()
	if atomic.LoadInt64(&got) != 1 {
		t.Fatal("no attempt was rejected with queue_full")
	}

	// Release and drain.
	close(slow.gate)
	if err := <-done0; err != nil {
		t.Fatalf("first job: %v", err)
	}
	for _, d := range dones {
		if err := <-d; err != nil {
			t.Fatalf("queued job: %v", err)
		}
	}
	co.Close()
}

// slowStore blocks every Commit until gate is closed and tracks entries.
type slowStore struct {
	store.Store
	gate    chan struct{}
	entered int64
}

func (s *slowStore) waitEntered(n int64, within time.Duration) bool {
	deadline := time.After(within)
	for atomic.LoadInt64(&s.entered) < n {
		select {
		case <-deadline:
			return false
		case <-s.gate:
			return false
		default:
			time.Sleep(2 * time.Millisecond)
		}
	}
	return true
}

func (s *slowStore) Commit(ctx context.Context, id string, c store.Commit) (int64, error) {
	atomic.AddInt64(&s.entered, 1)
	select {
	case <-s.gate:
	case <-ctx.Done():
		return 0, ctx.Err()
	}
	return s.Store.Commit(ctx, id, c)
}

// Malformed persisted live state is a computation failure, never a panic.
func TestCorruptStateIsComputationFailure(t *testing.T) {
	base := store.NewMemory()
	if _, err := base.Create(context.Background(), "r", json.RawMessage(`{"x":0}`),
		json.RawMessage(`{"x":0}`), "a", model.Schema{}); err != nil {
		t.Fatal(err)
	}
	st := &corruptStore{Store: base}
	co := coord.New(st, diag.NewLogger(nil))
	defer co.Close()
	_, err := co.Apply(context.Background(), coord.ApplyRequest{
		ResourceID: "r", Manager: "a",
		Config: json.RawMessage(`{"x":1}`),
	})
	var se *model.Error
	if !errors.As(err, &se) || se.Category != model.CatComputationFailure {
		t.Fatalf("want computation_failure, got %v", err)
	}
}

type corruptStore struct {
	store.Store
}

func (c *corruptStore) Snapshot(ctx context.Context, id string) (*store.Snapshot, error) {
	snap, err := c.Store.Snapshot(ctx, id)
	if err != nil {
		return nil, err
	}
	snap.Live = json.RawMessage(`{not json`)
	return snap, nil
}

// Invalid requests are rejected before they touch the loop.
func TestApplyValidation(t *testing.T) {
	co := coord.New(store.NewMemory(), diag.NewLogger(nil))
	defer co.Close()
	_, err := co.Apply(context.Background(), coord.ApplyRequest{
		ResourceID: "r", Manager: "", Config: json.RawMessage(`{}`),
	})
	if !model.IsCategory(err, model.CatInvalidInput) {
		t.Fatalf("want invalid_input, got %v", err)
	}
	_, err = co.Apply(context.Background(), coord.ApplyRequest{
		ResourceID: "r", Manager: "a", Config: json.RawMessage(`[1,2]`),
	})
	if !model.IsCategory(err, model.CatInvalidInput) {
		t.Fatalf("want invalid_input for array body, got %v", err)
	}
}
