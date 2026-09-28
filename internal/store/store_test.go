package store_test

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"testing"

	"fieldapply/internal/model"
	"fieldapply/internal/store"
)

func schemaForTest() model.Schema {
	return model.Schema{Lists: map[string]model.ListKind{
		".spec.tags":       model.ListSet,
		".spec.containers": model.ListKeyed,
	}, Keys: map[string]string{".spec.containers": "name"}}
}

// conformance exercises the Store contract once against every adapter, so the
// in-memory test double and SQLite cannot drift apart.
func TestStoreConformanceMemory(t *testing.T) {
	conformance(t, func(t *testing.T) store.Store { return store.NewMemory() })
}

func TestStoreConformanceSQLite(t *testing.T) {
	conformance(t, func(t *testing.T) store.Store {
		dir := t.TempDir()
		st, err := store.OpenSQLite("file:" + filepath.Join(dir, "t.db") + "?_pragma=busy_timeout(2000)")
		if err != nil {
			t.Fatalf("open: %v", err)
		}
		t.Cleanup(func() { _ = st.Close() })
		return st
	})
}

func conformance(t *testing.T, open func(t *testing.T) store.Store) {
	t.Helper()
	ctx := context.Background()
	schema := schemaForTest()
	body := json.RawMessage(`{"spec":{"replicas":3,"containers":[{"name":"web","image":"w1"}],"tags":["a"]}}`)

	t.Run("create and snapshot", func(t *testing.T) {
		st := open(t)
		defer st.Close()
		snap, err := st.Create(ctx, "r1", body, body, "a", schema)
		if err != nil {
			t.Fatalf("create: %v", err)
		}
		if snap.Revision != 1 {
			t.Fatalf("revision = %d, want 1", snap.Revision)
		}
		// Leaves: .spec.replicas, .spec.containers[name="web"].name,
		// .spec.containers[name="web"].image, .spec.tags[_="a"]
		if got := len(snap.Owners); got != 4 {
			t.Fatalf("owner rows = %d, want 4", got)
		}
	})

	t.Run("duplicate create conflicts", func(t *testing.T) {
		st := open(t)
		defer st.Close()
		if _, err := st.Create(ctx, "r1", body, body, "a", schema); err != nil {
			t.Fatal(err)
		}
		_, err := st.Create(ctx, "r1", body, body, "a", schema)
		var se *model.Error
		if !errors.As(err, &se) || se.Category != model.CatStateConflict {
			t.Fatalf("want state_conflict, got %v", err)
		}
	})

	t.Run("missing resource is not_found", func(t *testing.T) {
		st := open(t)
		defer st.Close()
		if _, err := st.Snapshot(ctx, "nope"); !model.IsCategory(err, model.CatNotFound) {
			t.Fatalf("snapshot err = %v", err)
		}
		if _, _, err := st.AppliedOf(ctx, "nope", "a"); !model.IsCategory(err, model.CatNotFound) {
			t.Fatalf("applied err = %v", err)
		}
		if _, err := st.History(ctx, "nope", 10); !model.IsCategory(err, model.CatNotFound) {
			t.Fatalf("history err = %v", err)
		}
	})

	t.Run("commit is atomic with optimistic revision", func(t *testing.T) {
		st := open(t)
		defer st.Close()
		snap, _ := st.Create(ctx, "r1", body, body, "a", schema)

		live2 := json.RawMessage(`{"spec":{"replicas":4,"containers":[{"name":"web","image":"w1"}],"tags":["a"]}}`)
		owners := snap.Owners.Clone()
		owners.Add(".spec.replicas", "a")
		rev, err := st.Commit(ctx, "r1", store.Commit{
			Manager: "a", Reason: "scale", RunID: "run-1", BaseRev: snap.Revision,
			Live: live2, Applied: live2, Owners: owners,
			Changes: model.ChangeSet{Changed: []model.Change{{
				Path: ".spec.replicas", Old: json.RawMessage("3"), New: json.RawMessage("4"),
			}}},
			At: snap.UpdatedAt.Add(1),
		})
		if err != nil {
			t.Fatalf("commit: %v", err)
		}
		if rev != 2 {
			t.Fatalf("rev = %d, want 2", rev)
		}

		// A stale BaseRev must be rejected without side effects.
		_, err = st.Commit(ctx, "r1", store.Commit{
			Manager: "a", BaseRev: snap.Revision, Live: live2, Applied: live2,
			Owners: owners, At: snap.UpdatedAt.Add(2),
		})
		var se *model.Error
		if !errors.As(err, &se) || se.Code != "revision_stale" {
			t.Fatalf("want revision_stale, got %v", err)
		}
		snap2, _ := st.Snapshot(ctx, "r1")
		if snap2.Revision != 2 {
			t.Fatalf("revision advanced after failed commit: %d", snap2.Revision)
		}

		hist, err := st.History(ctx, "r1", 10)
		if err != nil {
			t.Fatal(err)
		}
		if len(hist) != 1 || hist[0].Revision != 2 || hist[0].Manager != "a" ||
			hist[0].RunID != "run-1" || len(hist[0].Changes.Changed) != 1 {
			t.Fatalf("history wrong: %+v", hist)
		}
	})

	t.Run("per-manager applied configs", func(t *testing.T) {
		st := open(t)
		defer st.Close()
		aBody := json.RawMessage(`{"a":1}`)
		bBody := json.RawMessage(`{"b":2}`)
		snap, _ := st.Create(ctx, "r1", aBody, aBody, "a", model.Schema{})
		owners := model.Owners{}
		owners.Add(".a", "a")
		owners.Add(".b", "b")
		live := json.RawMessage(`{"a":1,"b":2}`)
		if _, err := st.Commit(ctx, "r1", store.Commit{
			Manager: "b", BaseRev: snap.Revision, Live: live, Applied: bBody,
			Owners: owners, At: snap.UpdatedAt.Add(1),
		}); err != nil {
			t.Fatal(err)
		}
		got, ok, err := st.AppliedOf(ctx, "r1", "a")
		if err != nil || !ok || string(got) != `{"a":1}` {
			t.Fatalf("applied a = %s ok=%v err=%v", got, ok, err)
		}
		got, ok, err = st.AppliedOf(ctx, "r1", "b")
		if err != nil || !ok || string(got) != `{"b":2}` {
			t.Fatalf("applied b = %s ok=%v err=%v", got, ok, err)
		}
		if _, ok, _ := st.AppliedOf(ctx, "r1", "ghost"); ok {
			t.Fatalf("ghost manager should have no applied config")
		}
	})
}
