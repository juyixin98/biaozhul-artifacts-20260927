package reconcile

import (
	"context"
	"io"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"resourcecontroller/internal/diag"
	"resourcecontroller/internal/model"
	"resourcecontroller/internal/store"
)

// scriptedClient is an independent in-memory implementation of
// ExternalClient used only by unit tests. It is deliberately separate from
// the HTTP fake service and from the code under test.
type scriptedClient struct {
	mu sync.Mutex

	objects map[string]*ExternalResource

	createAmbiguousTimes int // commit the object, return ambiguous
	createFailTimes      int // return transient, do not commit
	updateConflictTimes  int // refuse the update with a version conflict
	deleteFailTimes      int // refuse delete with a transient error
	staleGets            int // serve the previous spec for this many GETs after an update

	gets, creates, updates, deletes int

	// staleSnapshot holds the pre-update observation while staleGets > 0.
	staleSnapshot *ExternalResource
}

func newScriptedClient() *scriptedClient {
	return &scriptedClient{objects: map[string]*ExternalResource{}}
}

func (c *scriptedClient) Observe(_ context.Context, id string) (*ExternalResource, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.gets++
	if c.staleGets > 0 && c.staleSnapshot != nil {
		c.staleGets--
		cp := *c.staleSnapshot
		return &cp, nil
	}
	r, ok := c.objects[id]
	if !ok {
		return nil, &CallError{Attempt: "get", Category: CategoryNotFound, Code: "NotFound"}
	}
	cp := *r
	return &cp, nil
}

func (c *scriptedClient) Create(_ context.Context, id, key string, spec ExternalSpec) (*ExternalResource, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.creates++
	if c.createFailTimes > 0 {
		c.createFailTimes--
		return nil, &CallError{Attempt: "create", Category: CategoryTransient, Code: "CreateFailed"}
	}
	now := &ExternalResource{
		ID: id, Name: spec.Name, Version: 1, Spec: spec,
		SpecFP: model.SpecFingerprint(model.WidgetSpec{
			Replicas: spec.Replicas, Color: spec.Color, SecretToken: spec.SecretToken,
		}),
	}
	c.objects[id] = now
	if c.createAmbiguousTimes > 0 {
		c.createAmbiguousTimes--
		return nil, &CallError{Attempt: "create", Category: CategoryAmbiguous, Code: "ResponseLost",
			Message: "committed but response lost"}
	}
	cp := *now
	return &cp, nil
}

func (c *scriptedClient) Update(_ context.Context, id string, expectedVersion int64, spec ExternalSpec) (*ExternalResource, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.updates++
	cur := c.objects[id]
	if c.updateConflictTimes > 0 {
		c.updateConflictTimes--
		return nil, &CallError{Attempt: "update", Category: CategoryConflict, Code: "VersionConflict",
			Message: "stale version"}
	}
	if cur != nil && cur.Version != expectedVersion {
		return nil, &CallError{Attempt: "update", Category: CategoryConflict, Code: "VersionConflict"}
	}
	c.staleSnapshot = nil
	if cur != nil {
		cp := *cur
		c.staleSnapshot = &cp
	}
	cur.Spec = spec
	cur.SpecFP = model.SpecFingerprint(model.WidgetSpec{
		Replicas: spec.Replicas, Color: spec.Color, SecretToken: spec.SecretToken,
	})
	cur.Version++
	out := *cur
	return &out, nil
}

func (c *scriptedClient) Delete(_ context.Context, id string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.deletes++
	if c.deleteFailTimes > 0 {
		c.deleteFailTimes--
		return &CallError{Attempt: "delete", Category: CategoryTransient, Code: "DeleteFailed"}
	}
	delete(c.objects, id)
	return nil
}

// seed places an existing external object (independent of the controller).
func (c *scriptedClient) seed(id string, spec model.WidgetSpec, version int64) {
	c.objects[id] = &ExternalResource{
		ID: id, Name: id, Version: version,
		Spec:   ExternalSpec{Name: id, Replicas: spec.Replicas, Color: spec.Color, SecretToken: spec.SecretToken},
		SpecFP: model.SpecFingerprint(spec),
	}
}

func (c *scriptedClient) counts() (g, cr, u, d int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.gets, c.creates, c.updates, c.deletes
}

// ---- test harness ----

type unitFixture struct {
	t   *testing.T
	st  *store.Store
	ext *scriptedClient
	r   *Reconciler
	ctx context.Context
}

func newFixture(t *testing.T) *unitFixture {
	t.Helper()
	dsn := "file:" + filepath.Join(t.TempDir(), "rc.db") + "?_pragma=busy_timeout(2000)"
	st, err := store.Open(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	log := diag.New(io.Discard, "test")
	ext := newScriptedClient()
	r := New(st, ext, log, Config{
		Workers: 1, ResyncInterval: time.Hour,
		BackoffBase: time.Millisecond, BackoffMax: 5 * time.Millisecond,
	})
	return &unitFixture{t: t, st: st, ext: ext, r: r, ctx: context.Background()}
}

func newTestWidget(name string, gen int64, spec model.WidgetSpec) *model.Widget {
	now := time.Now().UTC()
	return &model.Widget{
		Meta: model.ObjectMeta{
			Name: name, UID: "uid-" + name, Generation: gen, ResourceVersion: 1,
			CreatedAt: now, UpdatedAt: now,
		},
		Spec:   spec,
		Status: model.WidgetStatus{Phase: model.PhasePending},
	}
}

func (f *unitFixture) createInStore(name string, spec model.WidgetSpec) *model.Widget {
	w := newTestWidget(name, 1, spec)
	if err := f.st.Create(f.ctx, w); err != nil {
		f.t.Fatal(err)
	}
	return w
}

// drive runs single reconcile passes until settle reports true or the pass
// budget is exhausted. Delays are collapsed: backoff is retry policy, not part
// of the state machine, so waiting would test the clock for nothing.
func (f *unitFixture) drive(name string, maxPasses int, settle func(*model.Widget) bool) *model.Widget {
	var last *model.Widget
	for i := 0; i < maxPasses; i++ {
		f.r.handle(f.ctx, name)
		w, err := f.st.Get(f.ctx, name)
		if err == store.ErrNotFound {
			return nil
		}
		if err != nil {
			f.t.Fatalf("load after pass %d: %v", i, err)
		}
		last = w
		if settle(w) {
			return w
		}
	}
	f.t.Fatalf("resource %q did not settle within %d passes; last=%+v", name, maxPasses, last)
	return nil
}

func settledReady(gen int64) func(*model.Widget) bool {
	return func(w *model.Widget) bool {
		return w.Status.ReconciledGeneration == gen &&
			w.Status.ObservedGeneration == gen && w.Status.Phase == model.PhaseReady
	}
}

var testSpec = model.WidgetSpec{Replicas: 2, Color: "blue", SecretToken: "secret-token-xyz"}

func TestHappyPathConvergesWithFinalizer(t *testing.T) {
	f := newFixture(t)
	f.createInStore("happy", testSpec)

	final := f.drive("happy", 10, settledReady(1))
	if final == nil {
		t.Fatal("resource vanished")
	}
	if len(final.Meta.Finalizers) != 1 || final.Meta.Finalizers[0] != FinalizerExternalCleanup {
		t.Fatalf("finalizer missing: %#v", final.Meta.Finalizers)
	}
	if final.Status.ExternalID == "" || final.Status.ExternalVersion != 1 {
		t.Fatalf("external bookkeeping wrong: %+v", final.Status)
	}
	if final.Status.LastAttempt == nil || final.Status.LastAttempt.Decision != diag.DecisionAccepted {
		t.Fatalf("last attempt should be accepted: %+v", final.Status.LastAttempt)
	}
	_, creates, _, _ := f.ext.counts()
	if creates != 1 {
		t.Fatalf("creates = %d, want 1", creates)
	}
	if _, ok := f.ext.objects[final.Status.ExternalID]; !ok {
		t.Fatal("external object missing after reconcile")
	}
}

// TestCreateInterruptedClaimsNotRecreates covers "external create succeeded
// but the response was lost": the next pass must claim by observation and
// never issue a second create.
func TestCreateInterruptedClaimsNotRecreates(t *testing.T) {
	f := newFixture(t)
	f.createInStore("interrupted", testSpec)
	f.ext.createAmbiguousTimes = 1

	// Pass 1: finalizer; pass 2: observe 404 -> create committed but ambiguous.
	f.r.handle(f.ctx, "interrupted")
	f.r.handle(f.ctx, "interrupted")
	mid, err := f.st.Get(f.ctx, "interrupted")
	if err != nil {
		t.Fatal(err)
	}
	if mid.Status.ReconciledGeneration != 0 {
		t.Fatalf("after lost response, reconciledGen = %d, want 0", mid.Status.ReconciledGeneration)
	}
	if mid.Status.LastAttempt == nil ||
		mid.Status.LastAttempt.Decision != diag.DecisionUndecidable ||
		mid.Status.LastAttempt.Reason != "create-response-lost" ||
		mid.Status.LastAttempt.Category != CategoryAmbiguous {
		t.Fatalf("last attempt should be undecidable/ambiguous: %+v", mid.Status.LastAttempt)
	}
	_, creates, _, _ := f.ext.counts()
	if creates != 1 {
		t.Fatalf("creates after ambiguity = %d, want exactly 1", creates)
	}

	final := f.drive("interrupted", 5, settledReady(1))
	if final == nil {
		t.Fatal("resource vanished")
	}
	if final.Status.ReconciledGeneration != 1 {
		t.Fatalf("reconciledGen = %d, want 1", final.Status.ReconciledGeneration)
	}
	_, creates, _, _ = f.ext.counts()
	if creates != 1 {
		t.Fatalf("creates after convergence = %d, claim path duplicated create", creates)
	}
	if _, ok := f.ext.objects[final.Status.ExternalID]; !ok {
		t.Fatal("claimed external object missing")
	}
}

// TestStaleObservationDoesNotCompleteGeneration ensures an old observation
// cannot advance reconciledGeneration; convergence still follows.
func TestStaleObservationDoesNotCompleteGeneration(t *testing.T) {
	f := newFixture(t)
	f.createInStore("stale", model.WidgetSpec{Replicas: 1, Color: "red", SecretToken: "s1"})
	first := f.drive("stale", 10, settledReady(1))

	// User moves to generation 2.
	gen2 := model.WidgetSpec{Replicas: 4, Color: "green", SecretToken: "s2"}
	up := *first
	up.Spec = gen2
	up.Meta.Generation = 2
	saved, err := f.st.SaveSpec(f.ctx, &up, first.Meta.ResourceVersion)
	if err != nil {
		t.Fatal(err)
	}

	// The first GET after the update will observe the pre-update snapshot.
	f.ext.staleGets = 1
	f.r.handle(f.ctx, "stale")
	mid, _ := f.st.Get(f.ctx, "stale")
	if mid.Status.ReconciledGeneration != 1 {
		t.Fatalf("stale confirm advanced reconciledGen to %d, must stay 1", mid.Status.ReconciledGeneration)
	}
	if mid.Meta.Generation != 2 {
		t.Fatalf("desired generation lost: %d", mid.Meta.Generation)
	}
	if mid.Status.LastAttempt == nil || mid.Status.LastAttempt.Decision != diag.DecisionUndecidable {
		t.Fatalf("stale observation must be undecidable: %+v", mid.Status.LastAttempt)
	}

	final := f.drive("stale", 6, settledReady(2))
	if final == nil {
		t.Fatal("resource vanished")
	}
	obj := f.ext.objects[final.Status.ExternalID]
	if obj.Spec.Replicas != 4 || obj.Spec.Color != "green" {
		t.Fatalf("external spec not at gen2: %+v", obj.Spec)
	}
	_ = saved
}

// TestConflictUpdateRequeuesWithoutOverwrite: an external version conflict
// must be rejected with a reload-requeue; the newer external state is never
// overwritten blind.
func TestConflictUpdateRequeuesWithoutOverwrite(t *testing.T) {
	f := newFixture(t)
	f.createInStore("conflict", testSpec)
	first := f.drive("conflict", 10, settledReady(1))
	// happy-path seeding uses name "conflict"
	if got := f.ext.objects[first.Status.ExternalID].Spec.Color; got != "blue" {
		t.Fatalf("setup wrong: %s", got)
	}

	up := *first
	up.Spec = model.WidgetSpec{Replicas: 5, Color: "black", SecretToken: "s3"}
	up.Meta.Generation = 2
	if _, err := f.st.SaveSpec(f.ctx, &up, first.Meta.ResourceVersion); err != nil {
		t.Fatal(err)
	}

	f.ext.updateConflictTimes = 1
	f.r.handle(f.ctx, "conflict")
	mid, _ := f.st.Get(f.ctx, "conflict")
	if mid.Status.ReconciledGeneration != 1 {
		t.Fatalf("conflict must not complete gen2: reconciledGen=%d", mid.Status.ReconciledGeneration)
	}
	if mid.Status.LastAttempt == nil ||
		mid.Status.LastAttempt.Decision != diag.DecisionRejected ||
		mid.Status.LastAttempt.Reason != "external-version-conflict" {
		t.Fatalf("attempt must be rejected/external-version-conflict: %+v", mid.Status.LastAttempt)
	}

	final := f.drive("conflict", 6, settledReady(2))
	obj := f.ext.objects[final.Status.ExternalID]
	if obj.Spec.Color != "black" || obj.Version != 2 {
		t.Fatalf("external object not updated after reload: %+v", obj)
	}
	_, _, updates, _ := f.ext.counts()
	if updates != 2 {
		t.Fatalf("updates = %d, want 2 (one refused conflict, one applied)", updates)
	}
}

// TestDeleteRetryKeepsRecordUntilCleanup: failed external deletes retry; the
// record survives until cleanup is confirmed and the finalizer is removed.
func TestDeleteRetryKeepsRecordUntilCleanup(t *testing.T) {
	f := newFixture(t)
	f.createInStore("doomed", testSpec)
	first := f.drive("doomed", 10, settledReady(1))
	extID := first.Status.ExternalID

	f.ext.deleteFailTimes = 1
	now := time.Now().UTC()
	del := *first
	del.Meta.DeletionTimestamp = &now
	if _, err := f.st.SaveSpec(f.ctx, &del, first.Meta.ResourceVersion); err != nil {
		t.Fatal(err)
	}

	// Pass budget covers: observe-present -> delete fails -> observe-present
	// -> delete ok -> observe 404 -> finalizer removal -> observe 404 ->
	// record removal.
	for i := 0; i < 12; i++ {
		f.r.handle(f.ctx, "doomed")
		if _, err := f.st.Get(f.ctx, "doomed"); err == store.ErrNotFound {
			break
		} else if err != nil {
			t.Fatal(err)
		}
	}
	if _, err := f.st.Get(f.ctx, "doomed"); err != store.ErrNotFound {
		t.Fatalf("record still present after confirmed cleanup: %v", err)
	}
	if _, ok := f.ext.objects[extID]; ok {
		t.Fatal("external object still present")
	}
	_, _, _, deletes := f.ext.counts()
	if deletes != 2 {
		t.Fatalf("deletes = %d, want 2 (one failed retry, one success)", deletes)
	}
}

// TestDeletePhasesOwnership checks that mid-deletion the record still exists,
// still carries its finalizer, and status names the external object.
func TestDeletePhasesOwnership(t *testing.T) {
	f := newFixture(t)
	f.createInStore("phases", testSpec)
	first := f.drive("phases", 10, settledReady(1))

	// Arm a persistent delete failure and step once through deletion.
	f.ext.deleteFailTimes = 100
	now := time.Now().UTC()
	del := *first
	del.Meta.DeletionTimestamp = &now
	if _, err := f.st.SaveSpec(f.ctx, &del, first.Meta.ResourceVersion); err != nil {
		t.Fatal(err)
	}
	f.r.handle(f.ctx, "phases") // object present, possibly records id
	f.r.handle(f.ctx, "phases") // delete attempted, fails
	mid, err := f.st.Get(f.ctx, "phases")
	if err != nil {
		t.Fatal(err)
	}
	if mid.Status.ExternalID == "" {
		t.Fatal("status must record the claimed external id during deletion")
	}
	if len(mid.Meta.Finalizers) != 1 {
		t.Fatalf("finalizer must remain while external object exists: %#v", mid.Meta.Finalizers)
	}
	if mid.Status.LastAttempt == nil || mid.Status.LastAttempt.Phase != "delete" {
		t.Fatalf("expected delete-phase attempt record: %+v", mid.Status.LastAttempt)
	}
	if _, ok := f.ext.objects[mid.Status.ExternalID]; !ok {
		t.Fatal("external object must still belong to the undeleted resource")
	}
}

// TestFailedCreateRetriesAndEventuallySucceeds: a hard create failure leaves
// generations untouched and the backoff retry creates exactly once when the
// service recovers.
func TestFailedCreateRetriesAndEventuallySucceeds(t *testing.T) {
	f := newFixture(t)
	f.createInStore("flaky", testSpec)
	f.ext.createFailTimes = 1

	f.r.handle(f.ctx, "flaky") // finalizer
	f.r.handle(f.ctx, "flaky") // create fails transiently
	mid, _ := f.st.Get(f.ctx, "flaky")
	if mid.Status.ReconciledGeneration != 0 {
		t.Fatalf("failed create must not complete generation: %d", mid.Status.ReconciledGeneration)
	}
	if mid.Status.LastAttempt == nil || mid.Status.LastAttempt.Category != "CreateFailed" {
		t.Fatalf("attempt must name the failure category: %+v", mid.Status.LastAttempt)
	}
	final := f.drive("flaky", 6, settledReady(1))
	if final == nil || final.Status.ReconciledGeneration != 1 {
		t.Fatalf("did not converge after retry: %+v", final)
	}
}
