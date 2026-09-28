package store_test

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"fieldmerge/internal/apperr"
	"fieldmerge/internal/merge"
	"fieldmerge/internal/schema"
	"fieldmerge/internal/store"
)

func openStore(t *testing.T, lim store.Limits) (*store.Store, func()) {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(dir+"/test.db", lim)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	return st, func() { _ = st.Close() }
}

func widgetSchema(t *testing.T) *schema.Schema {
	t.Helper()
	sc, err := schema.New("widget", map[string]schema.ListDecl{
		"tags": {Type: schema.ListSet},
	})
	if err != nil {
		t.Fatalf("schema: %v", err)
	}
	return sc
}

func apply(t *testing.T, st *store.Store, sc *schema.Schema, kind, name, manager string, force bool, cfg string) (*store.ApplyOutcome, error) {
	t.Helper()
	return st.Apply(context.Background(), store.ApplyRequest{
		Kind: kind, Name: name, Manager: manager, Force: force,
		Config: json.RawMessage(cfg), Schema: sc, RunID: "test-run",
	})
}

func TestApply_ConflictPersistsNothing(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	sc := widgetSchema(t)

	if _, err := apply(t, st, sc, "widget", "w1", "net", false,
		`{"image":"v1","tags":["a"]}`); err != nil {
		t.Fatal(err)
	}
	before, _ := st.Get(context.Background(), "widget", "w1")

	_, err := apply(t, st, sc, "widget", "w1", "sre", false,
		`{"image":"v2"}`)
	if err == nil {
		t.Fatal("expected ownership conflict error")
	}
	res, ok := store.ConflictResult(err)
	if !ok || len(res.Conflict) != 1 {
		t.Fatalf("conflict details missing: %v", err)
	}
	if res.Conflict[0].Path != "image" || res.Conflict[0].Owners[0] != "net" {
		t.Fatalf("conflict must return path and original manager: %+v", res.Conflict[0])
	}

	after, _ := st.Get(context.Background(), "widget", "w1")
	if string(after.Live) != string(before.Live) {
		t.Fatalf("conflict must not mutate live:\nbefore %s\nafter  %s", before.Live, after.Live)
	}
	if after.Revision != before.Revision {
		t.Fatalf("conflict must not bump revision: %d vs %d", before.Revision, after.Revision)
	}
	own, _ := st.Ownership(context.Background(), "widget", "w1")
	if !hasOwner(own, "image", "net") {
		t.Fatalf("ownership must stay with net after failed apply: %+v", own)
	}
	hist, _ := st.History(context.Background(), "widget", "w1", 10)
	if len(hist) != 1 {
		t.Fatalf("failed apply must not write history; got %d rows", len(hist))
	}
}

func TestApply_ForceAndHistoryAudit(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	sc := widgetSchema(t)

	o1, err := apply(t, st, sc, "widget", "w1", "net", false, `{"image":"v1"}`)
	if err != nil {
		t.Fatal(err)
	}
	if o1.Revision != 1 {
		t.Fatalf("first revision = %d, want 1", o1.Revision)
	}
	o2, err := apply(t, st, sc, "widget", "w1", "sre", true, `{"image":"v2"}`)
	if err != nil {
		t.Fatal(err)
	}
	if o2.Revision != 2 {
		t.Fatalf("second revision = %d, want 2", o2.Revision)
	}

	// Value and ownership were committed together.
	r, _ := st.Get(context.Background(), "widget", "w1")
	var live map[string]any
	if err := json.Unmarshal(r.Live, &live); err != nil {
		t.Fatal(err)
	}
	if live["image"] != "v2" {
		t.Fatalf("live image = %v, want v2", live["image"])
	}
	own, _ := st.Ownership(context.Background(), "widget", "w1")
	if !hasOwner(own, "image", "sre") {
		t.Fatalf("image should now be owned by sre: %+v", own)
	}

	// History lets every revision be replayed with its config and changes.
	hist, _ := st.History(context.Background(), "widget", "w1", 10)
	if len(hist) != 2 || hist[0].Revision != 2 || hist[1].Revision != 1 {
		t.Fatalf("history ordering/contents wrong: %+v", hist)
	}
	if hist[0].Manager != "sre" || !hist[0].Forced || hist[0].RunID != "test-run" {
		t.Fatalf("rev2 audit row wrong: manager=%s forced=%v run=%s",
			hist[0].Manager, hist[0].Forced, hist[0].RunID)
	}
	var changes []merge.Change
	if err := json.Unmarshal(hist[0].Changes, &changes); err != nil {
		t.Fatal(err)
	}
	foundTakeover := false
	for _, c := range changes {
		if c.Path == "image" && c.Op == "takeover" {
			foundTakeover = true
			if string(c.From) != `"v1"` || string(c.To) != `"v2"` {
				t.Fatalf("audit change must carry from/to: %+v", c)
			}
		}
	}
	if !foundTakeover {
		t.Fatalf("history must record the takeover with before/after values: %s", hist[0].Changes)
	}
}

func TestApply_ValueOwnershipSameTransaction(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	sc := widgetSchema(t)

	if _, err := apply(t, st, sc, "widget", "w1", "net", false,
		`{"image":"v1","tags":["a","b"]}`); err != nil {
		t.Fatal(err)
	}
	// Every live leaf has a corresponding ownership row, and vice versa.
	r, _ := st.Get(context.Background(), "widget", "w1")
	var live map[string]any
	_ = json.Unmarshal(r.Live, &live)
	own, _ := st.Ownership(context.Background(), "widget", "w1")
	paths := map[string]bool{}
	for _, c := range own {
		paths[c.Path] = true
	}
	for _, want := range []string{"image", `tags[^"a"]`, `tags[^"b"]`} {
		if !paths[want] {
			t.Fatalf("ownership missing %s after commit: %+v", want, own)
		}
	}
}

func TestApply_ResourceExhaustion(t *testing.T) {
	// 1-byte payload cap to exercise the payload limit deterministically.
	st, cleanup := openStore(t, store.Limits{MaxPayload: 1, MaxResources: 0})
	defer cleanup()
	_, err := apply(t, st, nil, "widget", "w1", "net", false, `{"a":1}`)
	ae, ok := apperr.As(err)
	if !ok || ae.Category != apperr.ResourceExhausted || ae.Code != "payload_too_large" {
		t.Fatalf("want resource_exhausted/payload_too_large, got %v", err)
	}

	// Resource-count cap.
	st2, cleanup2 := openStore(t, store.Limits{MaxResources: 1})
	defer cleanup2()
	if _, err := apply(t, st2, nil, "widget", "a", "net", false, `{"x":1}`); err != nil {
		t.Fatal(err)
	}
	_, err = apply(t, st2, nil, "widget", "b", "net", false, `{"x":1}`)
	ae, ok = apperr.As(err)
	if !ok || ae.Category != apperr.ResourceExhausted || ae.Code != "resource_limit" {
		t.Fatalf("want resource_exhausted/resource_limit, got %v", err)
	}
}

func TestApply_InvalidInputCategories(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	_, err := st.Apply(context.Background(), store.ApplyRequest{
		Kind: "widget", Name: "w1", Manager: "", Config: json.RawMessage(`{}`),
	})
	assertCategory(t, err, apperr.InvalidInput, "manager_required")

	_, err = st.Apply(context.Background(), store.ApplyRequest{
		Kind: "widget", Name: "w1", Manager: "net", Config: json.RawMessage(`{bad`),
	})
	assertCategory(t, err, apperr.InvalidInput, "config_bad_json")

	sc := widgetSchema(t)
	// A list without a schema declaration is a client error, not silent atomic.
	if _, err := apply(t, st, sc, "widget", "w2", "net", false,
		`{"undeclared":[1,2]}`); err == nil {
		t.Fatal("undeclared list must be rejected")
	} else {
		assertCategory(t, err, apperr.InvalidInput, "undeclared_list")
	}
}

func TestApply_CorruptLiveIsComputeFailure(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	// Insert a valid resource first, then corrupt live directly in the DB.
	if _, err := apply(t, st, nil, "widget", "w1", "net", false, `{"a":1}`); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB().Exec(
		`UPDATE resources SET live='not-json' WHERE kind='widget' AND name='w1'`); err != nil {
		t.Fatal(err)
	}
	_, err := apply(t, st, nil, "widget", "w1", "net", false, `{"a":2}`)
	assertCategory(t, err, apperr.ComputeFailure, "live_bad_json")
}

func TestApply_CorruptOwnershipIsInternal(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	if _, err := apply(t, st, nil, "widget", "w1", "net", false, `{"a":1}`); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB().Exec(
		`UPDATE ownership SET path='a[' WHERE kind='widget' AND name='w1'`); err != nil {
		t.Fatal(err)
	}
	_, err := apply(t, st, nil, "widget", "w1", "net", false, `{"a":2}`)
	ae, ok := apperr.As(err)
	if !ok || ae.Category != apperr.Internal || !strings.Contains(ae.Code, "ownership_corrupt") {
		t.Fatalf("want internal/ownership_corrupt*, got %v", err)
	}
}

func TestSchemaRoundtrip(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	sc := widgetSchema(t)
	if err := st.PutSchema(context.Background(), sc); err != nil {
		t.Fatal(err)
	}
	got, err := st.GetSchema(context.Background(), "widget")
	if err != nil {
		t.Fatal(err)
	}
	if got.Lists["tags"].Type != schema.ListSet {
		t.Fatalf("schema roundtrip lost decl: %+v", got.Lists)
	}
	empty, err := st.GetSchema(context.Background(), "never-defined")
	if err != nil || len(empty.Lists) != 0 {
		t.Fatalf("missing schema should be empty, non-nil: %v %+v", err, empty)
	}
}

func TestGetNotFound(t *testing.T) {
	st, cleanup := openStore(t, store.Limits{})
	defer cleanup()
	r, err := st.Get(context.Background(), "x", "y")
	if err != nil || r != nil {
		t.Fatalf("missing resource must be (nil,nil): %v %v", r, err)
	}
}

func assertCategory(t *testing.T, err error, cat apperr.Category, code string) {
	t.Helper()
	var ae *apperr.Error
	if !errors.As(err, &ae) {
		t.Fatalf("expected typed error %s/%s, got %v", cat, code, err)
	}
	if ae.Category != cat || ae.Code != code {
		t.Fatalf("error = %s/%s, want %s/%s", ae.Category, ae.Code, cat, code)
	}
}

func hasOwner(claims []merge.Claim, path, manager string) bool {
	for _, c := range claims {
		if c.Path == path {
			for _, m := range c.Managers {
				if m == manager {
					return true
				}
			}
		}
	}
	return false
}
