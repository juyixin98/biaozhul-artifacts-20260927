package merge_test

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"fieldapply/internal/merge"
	"fieldapply/internal/model"
)

func mustDecode(t *testing.T, raw string) any {
	t.Helper()
	v, err := model.DecodeValue([]byte(raw))
	if err != nil {
		t.Fatalf("decode %q: %v", raw, err)
	}
	return v
}

func ownersOf(t *testing.T, o model.Owners, path string) []string {
	t.Helper()
	got, ok := o[path]
	if !ok {
		return nil
	}
	var out []string
	for m := range got {
		out = append(out, m)
	}
	return out
}

func run(t *testing.T, in *merge.Inputs) *merge.Result {
	t.Helper()
	res, err := merge.Apply(in)
	if err != nil {
		t.Fatalf("Apply unexpected error: %v", err)
	}
	return res
}

func expectConflict(t *testing.T, err error, code string, paths ...string) []model.Conflict {
	t.Helper()
	var se *model.Error
	if !errors.As(err, &se) {
		t.Fatalf("want structured error, got %T %[1]v", err)
	}
	if se.Category != model.CatStateConflict {
		t.Fatalf("category = %s, want state_conflict", se.Category)
	}
	if se.Code != code {
		t.Fatalf("code = %s, want %s", se.Code, code)
	}
	if len(se.Conflicts) != len(paths) {
		t.Fatalf("conflict count = %d (%v), want %d (%v)", len(se.Conflicts), se.Conflicts, len(paths), paths)
	}
	for i, p := range paths {
		if se.Conflicts[i].Path != p {
			t.Fatalf("conflict[%d] path = %q, want %q; all=%v", i, se.Conflicts[i].Path, p, se.Conflicts)
		}
	}
	return se.Conflicts
}

// Two managers own disjoint fields. Each can update its own; an unrelated
// field the other manager contributed must never be lost.
func TestTwoManagersDisjointFields(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}

	// A creates with replicas + image.
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live:   nil,
		Owners: model.Owners{},
		Config: mustDecode(t, `{"spec":{"replicas":3,"image":"web:1"}}`),
		Schema: schema, Rev: 1,
	})

	// B adopts only image; replicas stays owned by A.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Previous: nil,
		Config:   mustDecode(t, `{"spec":{"image":"web:1"}}`),
		Schema:   schema, Rev: 2,
	})

	// A bumps replicas; omits image entirely. image must survive.
	r3 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: r2.Live, Owners: r2.Owners,
		Previous: mustDecode(t, `{"spec":{"replicas":3,"image":"web:1"}}`),
		Config:   mustDecode(t, `{"spec":{"replicas":5}}`),
		Schema:   schema, Rev: 3,
	})

	live := r3.Live.(map[string]any)
	spec := live["spec"].(map[string]any)
	if got := spec["replicas"].(json.Number); got.String() != "5" {
		t.Fatalf("replicas = %s, want 5", got)
	}
	if got := spec["image"].(string); got != "web:1" {
		t.Fatalf("unrelated field image lost: got %q, want web:1", got)
	}
	if ms := ownersOf(t, r3.Owners, ".spec.replicas"); len(ms) != 1 || ms[0] != "a" {
		t.Fatalf("replicas owners = %v, want [a]", ms)
	}
	ms := ownersOf(t, r3.Owners, ".spec.image")
	if len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("image owners = %v, want [b]", ms)
	}

	// B can still update image.
	r4 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r3.Live, Owners: r3.Owners,
		Previous: mustDecode(t, `{"spec":{"image":"web:1"}}`),
		Config:   mustDecode(t, `{"spec":{"image":"web:2"}}`),
		Schema:   schema, Rev: 4,
	})
	spec = r4.Live.(map[string]any)["spec"].(map[string]any)
	if spec["image"] != "web:2" || spec["replicas"].(json.Number).String() != "5" {
		t.Fatalf("post-B update wrong: %v", spec)
	}
}

// Different values on a foreign-owned leaf conflict and return owners + values;
// the live state is untouched. Force takes the field over.
func TestConflictReportsOwnersAndForce(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"spec":{"replicas":3}}`),
		Schema: schema, Rev: 1,
	})

	_, err := merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"spec":{"replicas":7}}`),
		Schema: schema, Rev: 2,
	})
	conflicts := expectConflict(t, err, "field_conflict", ".spec.replicas")
	c := conflicts[0]
	if len(c.Owners) != 1 || c.Owners[0] != "a" {
		t.Fatalf("conflict owners = %v, want [a]", c.Owners)
	}
	if string(c.Current) != "3" || string(c.Applied) != "7" {
		t.Fatalf("conflict values current=%s applied=%s", c.Current, c.Applied)
	}

	// Forced apply by B takes the leaf; A is removed from its owners.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"spec":{"replicas":7}}`),
		Force:  true,
		Schema: schema, Rev: 2,
	})
	spec := r2.Live.(map[string]any)["spec"].(map[string]any)
	if spec["replicas"].(json.Number).String() != "7" {
		t.Fatalf("forced value = %v, want 7", spec["replicas"])
	}
	if ms := ownersOf(t, r2.Owners, ".spec.replicas"); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("owners after force = %v, want [b]", ms)
	}

	// A's old ownership is gone: A now conflicts if it retries its stale value.
	_, err = merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: r2.Live, Owners: r2.Owners,
		Previous: mustDecode(t, `{"spec":{"replicas":3}}`),
		Config:   mustDecode(t, `{"spec":{"replicas":3}}`),
		Schema:   schema, Rev: 3,
	})
	expectConflict(t, err, "field_conflict", ".spec.replicas")
}

// Omitting a field the manager owns is an explicit delete; omitting a field it
// never owned leaves the uncommitted value intact.
func TestExplicitDeleteVsUncommitted(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"a":1,"b":2}`),
		Schema: schema, Rev: 1,
	})

	// Direct client writes "note" outside any apply: it is uncommitted.
	live := r1.Live.(map[string]any)
	live["note"] = "manual"

	// A re-applies with only "a": "b" (A-owned) must be deleted; "note"
	// (uncommitted) must survive.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: live, Owners: r1.Owners,
		Previous: mustDecode(t, `{"a":1,"b":2}`),
		Config:   mustDecode(t, `{"a":1}`),
		Schema:   schema, Rev: 2,
	})
	got := r2.Live.(map[string]any)
	if _, has := got["b"]; has {
		t.Fatalf("owned field b should be deleted, live=%v", got)
	}
	if got["note"] != "manual" {
		t.Fatalf("uncommitted note must survive, live=%v", got)
	}
	if got["a"].(json.Number).String() != "1" {
		t.Fatalf("a = %v", got["a"])
	}
	if removed := r2.Changes.Removed; len(removed) != 1 || removed[0].Path != ".b" {
		t.Fatalf("changes.removed = %+v, want [.b]", removed)
	}
}

// Equal desired value shares ownership without a conflict and without changing
// live (the SSA "share when identical" rule).
func TestEqualValueSharesOwnership(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"x":10}`),
		Schema: schema, Rev: 1,
	})
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"x":10}`),
		Schema: schema, Rev: 2,
	})
	ms := ownersOf(t, r2.Owners, ".x")
	if len(ms) != 2 {
		t.Fatalf("owners = %v, want shared [a b]", ms)
	}
	// A deleting x must NOT remove it: B still owns a share.
	r3 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: r2.Live, Owners: r2.Owners,
		Previous: mustDecode(t, `{"x":10}`),
		Config:   mustDecode(t, `{}`),
		Schema:   schema, Rev: 3,
	})
	got := r3.Live.(map[string]any)
	if got["x"].(json.Number).String() != "10" {
		t.Fatalf("co-owned field deleted by one manager: %v", got)
	}
	if ms := ownersOf(t, r3.Owners, ".x"); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("owners after partial release = %v, want [b]", ms)
	}
}

// Nested keyed list: two managers modify different containers by key and
// different fields of the same container.
func TestNestedKeyedListMerge(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{
		".spec.containers": model.ListKeyed,
		".spec.ingress":    model.ListKeyed,
	}, Keys: map[string]string{
		".spec.containers": "name",
		".spec.ingress":    "host",
	}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","image":"web:1","port":8080},
			{"name":"worker","image":"worker:1"}
		]}}`),
		Schema: schema, Rev: 1,
	})

	// B adopts web.port at its current value to share ownership.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","port":8080}
		]}}`),
		Schema: schema, Rev: 2,
	})
	web := findContainer(t, r2.Live, "web")
	if web["image"] != "web:1" {
		t.Fatalf("web.image lost: %v", web)
	}
	if web["port"].(json.Number).String() != "8080" {
		t.Fatalf("web.port = %v, want 8080", web["port"])
	}
	worker := findContainer(t, r2.Live, "worker")
	if worker["image"] != "worker:1" {
		t.Fatalf("worker container lost: %v", worker)
	}
	pPath := `.spec.containers[name="web"].port`
	if ms := ownersOf(t, r2.Owners, pPath); len(ms) != 2 {
		t.Fatalf("port owners = %v, want [a b]", ms)
	}
	iPath := `.spec.containers[name="web"].image`
	if ms := ownersOf(t, r2.Owners, iPath); len(ms) != 1 || ms[0] != "a" {
		t.Fatalf("image owners = %v, want [a]", ms)
	}

	// B now changes the shared port: because A still holds a share, B must
	// force; only that leaf changes hands — image stays A's.
	r2b := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r2.Live, Owners: r2.Owners,
		Previous: mustDecode(t, `{"spec":{"containers":[{"name":"web","port":8080}]}}`),
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","port":9090}
		]}}`),
		Force:  true,
		Schema: schema, Rev: 3,
	})
	web = findContainer(t, r2b.Live, "web")
	if web["port"].(json.Number).String() != "9090" {
		t.Fatalf("web.port = %v, want 9090", web["port"])
	}
	if ms := ownersOf(t, r2b.Owners, pPath); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("port owners after force = %v, want [b]", ms)
	}
	if ms := ownersOf(t, r2b.Owners, iPath); len(ms) != 1 || ms[0] != "a" {
		t.Fatalf("image owners after port force = %v, want [a]", ms)
	}

	// A deletes the worker container by omitting it; web stays intact.
	r3 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: r2b.Live, Owners: r2b.Owners,
		Previous: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","image":"web:1","port":8080},
			{"name":"worker","image":"worker:1"}
		]}}`),
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","image":"web:1"}
		]}}`),
		Schema: schema, Rev: 4,
	})
	containers := containersOf(t, r3.Live)
	if len(containers) != 1 || containers[0]["name"] != "web" {
		t.Fatalf("worker not deleted as expected: %v", containers)
	}
	if containers[0]["port"].(json.Number).String() != "9090" {
		t.Fatalf("B-owned port lost during A delete: %v", containers[0])
	}

	// B conflicting change to A-owned image fails naming a and leaves live.
	_, err := merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r3.Live, Owners: r3.Owners,
		Previous: mustDecode(t, `{"spec":{"containers":[{"name":"web","port":9090}]}}`),
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","port":9090,"image":"hacked:9"}
		]}}`),
		Schema: schema, Rev: 5,
	})
	expectConflict(t, err, "field_conflict", `.spec.containers[name="web"].image`)

	// Nested conflict: fresh manager C tries to take image + port with
	// differing values; both paths and their owners must be reported.
	_, err = merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "c",
		Live: r3.Live, Owners: r3.Owners,
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","port":1,"image":"x"}
		]}}`),
		Schema: schema, Rev: 5,
	})
	var se *model.Error
	if !errors.As(err, &se) {
		t.Fatalf("want conflict for c, got %v", err)
	}
	if len(se.Conflicts) != 2 {
		t.Fatalf("want 2 conflicts, got %v", se.Conflicts)
	}
	byPath := map[string][]string{}
	for _, cf := range se.Conflicts {
		byPath[cf.Path] = cf.Owners
	}
	if ms := byPath[iPath]; len(ms) != 1 || ms[0] != "a" {
		t.Fatalf("image conflict owners = %v, want [a]", ms)
	}
	if ms := byPath[pPath]; len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("port conflict owners = %v, want [b]", ms)
	}
}

// Set lists merge by membership across managers; atomic lists are one leaf and
// conflict wholesale on differing payloads.
func TestSetAndAtomicLists(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{
		".tags":    model.ListSet,
		".command": model.ListAtomic,
	}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"tags":["x","y"],"command":["sh","-c","run"]}`),
		Schema: schema, Rev: 1,
	})

	// B adds set element "z" and claims "x" with same value.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"tags":["x","z"]}`),
		Schema: schema, Rev: 2,
	})
	tags := r2.Live.(map[string]any)["tags"].([]any)
	if len(tags) != 3 {
		t.Fatalf("tags = %v, want [x y z] membership", tags)
	}
	xPath := `.tags[_="x"]`
	if ms := ownersOf(t, r2.Owners, xPath); len(ms) != 2 {
		t.Fatalf("x owners = %v, want [a b]", ms)
	}
	zPath := `.tags[_="z"]`
	if ms := ownersOf(t, r2.Owners, zPath); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("z owners = %v, want [b]", ms)
	}

	// A withdraws "y" (owned only by A): it disappears; x,z survive.
	r3 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: r2.Live, Owners: r2.Owners,
		Previous: mustDecode(t, `{"tags":["x","y"],"command":["sh","-c","run"]}`),
		Config:   mustDecode(t, `{"tags":["x"]}`),
		Schema:   schema, Rev: 3,
	})
	tags = r3.Live.(map[string]any)["tags"].([]any)
	if len(tags) != 2 {
		t.Fatalf("tags after withdraw = %v, want [x z]", tags)
	}

	// A's r3 omitted command: as sole owner A released it, so it is gone
	// from live (explicit deletion of an atomic list).
	if _, has := r3.Live.(map[string]any)["command"]; has {
		t.Fatalf("sole-owner omitted atomic list should be deleted: %v", r3.Live)
	}
}

// Atomic lists are a single owned leaf: any differing payload from a
// non-owner conflicts on the whole array and leaves it untouched.
func TestAtomicListConflictAndForce(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{
		".command": model.ListAtomic,
	}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"command":["sh","-c","run"],"x":1}`),
		Schema: schema, Rev: 1,
	})
	_, err := merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"command":["sh","-c","other"]}`),
		Schema: schema, Rev: 2,
	})
	expectConflict(t, err, "field_conflict", ".command")
	cmd := r1.Live.(map[string]any)["command"].([]any)
	if len(cmd) != 3 || cmd[2] != "run" {
		t.Fatalf("atomic command mutated by failed apply: %v", cmd)
	}

	// Force replaces the whole atom; the unrelated x is unaffected.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"command":["sh","-c","other"]}`),
		Force:  true,
		Schema: schema, Rev: 2,
	})
	got := r2.Live.(map[string]any)
	if got["command"].([]any)[2] != "other" {
		t.Fatalf("forced command wrong: %v", got["command"])
	}
	if got["x"].(json.Number).String() != "1" {
		t.Fatalf("unrelated x lost during forced atomic replace: %v", got)
	}
	if ms := ownersOf(t, r2.Owners, ".command"); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("command owners = %v, want [b]", ms)
	}
}

// Forced takeover inside a keyed list: B force-updates A-owned image field;
// unrelated worker/image remains A-owned.
func TestForceTakeoverScopedToLeaf(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{
		".spec.containers": model.ListKeyed,
	}, Keys: map[string]string{".spec.containers": "name"}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","image":"web:1"},
			{"name":"worker","image":"worker:1"}
		]}}`),
		Schema: schema, Rev: 1,
	})
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"spec":{"containers":[
			{"name":"web","image":"web:2"},
			{"name":"worker","image":"worker:2"}
		]}}`),
		Force:  true,
		Schema: schema, Rev: 2,
	})
	if ms := ownersOf(t, r2.Owners, `.spec.containers[name="web"].image`); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("web image owners = %v, want [b]", ms)
	}
	if ms := ownersOf(t, r2.Owners, `.spec.containers[name="worker"].image`); len(ms) != 1 || ms[0] != "b" {
		t.Fatalf("worker image owners = %v, want [b]", ms)
	}
	containers := containersOf(t, r2.Live)
	if containers[0]["image"] != "web:2" || containers[1]["image"] != "worker:2" {
		t.Fatalf("forced values wrong: %v", containers)
	}
}

// Invalid inputs are a distinct category from state conflicts.
func TestInvalidInputCategory(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{".xs": model.ListSet}}
	cases := []struct {
		name   string
		config string
		code   string
	}{
		{"set element compound", `{"xs":[{"a":1}]}`, "set_element_not_scalar"},
		{"config array", `[1,2]`, "config_not_object"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := merge.Apply(&merge.Inputs{
				ResourceID: "app", Manager: "a",
				Owners: model.Owners{},
				Config: mustDecode(t, tc.config),
				Schema: schema, Rev: 1,
			})
			var se *model.Error
			if !errors.As(err, &se) {
				t.Fatalf("want structured error, got %v", err)
			}
			if se.Category != model.CatInvalidInput || se.Code != tc.code {
				t.Fatalf("got %s/%s, want invalid_input/%s", se.Category, se.Code, tc.code)
			}
		})
	}
}

// Stale optimistic-concurrency token is a state conflict before merging.
func TestStaleRevision(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}
	_, err := merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{}, BaseRev: 4, Rev: 5,
		Config: mustDecode(t, `{"a":1}`),
		Schema: schema,
	})
	var se *model.Error
	if !errors.As(err, &se) || se.Code != "revision_stale" {
		t.Fatalf("want revision_stale, got %v", err)
	}
}

// Explicit JSON null is pruned like an omission: the sole owner releases the
// field (it is deleted), but a non-owner sending null cannot remove a value
// owned by someone else.
func TestExplicitNullSemantics(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"a":1,"b":2}`),
		Schema: schema, Rev: 1,
	})
	// A nulls b as sole owner: b is explicitly removed.
	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Live: r1.Live, Owners: r1.Owners,
		Previous: mustDecode(t, `{"a":1,"b":2}`),
		Config:   mustDecode(t, `{"a":1,"b":null}`),
		Schema:   schema, Rev: 2,
	})
	got := r2.Live.(map[string]any)
	if _, has := got["b"]; has {
		t.Fatalf("sole-owner null should remove b, live=%v", got)
	}
	if _, owned := r2.Owners[".b"]; owned {
		t.Fatalf("ownership of nulled b should be released: %v", r2.Owners)
	}
	if removed := r2.Changes.Removed; len(removed) != 1 || removed[0].Path != ".b" {
		t.Fatalf("changes.removed = %+v, want [.b]", removed)
	}

	// B (non-owner) sends null for an A-owned field and null for a fresh
	// field: nothing is deleted, no conflict arises.
	r3 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r2.Live, Owners: r2.Owners,
		Config: mustDecode(t, `{"a":null,"c":null}`),
		Schema: schema, Rev: 3,
	})
	got = r3.Live.(map[string]any)
	if got["a"].(json.Number).String() != "1" {
		t.Fatalf("non-owner null removed foreign field a: %v", got)
	}
	if _, has := got["c"]; has {
		t.Fatalf("null for fresh field should not create null value: %v", got)
	}
}

// Structural collision object<->array: blocked per foreign leaf, force replaces.
func TestStructuralCollision(t *testing.T) {
	schema := model.Schema{Lists: map[string]model.ListKind{}}
	r1 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "a",
		Owners: model.Owners{},
		Config: mustDecode(t, `{"x":{"y":1,"z":2}}`),
		Schema: schema, Rev: 1,
	})
	_, err := merge.Apply(&merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"x":[1,2,3]}`),
		Schema: schema, Rev: 2,
	})
	var se *model.Error
	if !errors.As(err, &se) || se.Category != model.CatStateConflict {
		t.Fatalf("want state conflict, got %v", err)
	}
	paths := []string{se.Conflicts[0].Path, se.Conflicts[1].Path}
	if paths[0] != ".x.y" || paths[1] != ".x.z" {
		t.Fatalf("collision conflicts = %v", paths)
	}

	r2 := run(t, &merge.Inputs{
		ResourceID: "app", Manager: "b",
		Live: r1.Live, Owners: r1.Owners,
		Config: mustDecode(t, `{"x":[1,2,3]}`),
		Force:  true,
		Schema: schema, Rev: 2,
	})
	x := r2.Live.(map[string]any)["x"].([]any)
	if len(x) != 3 || x[0].(json.Number).String() != "1" {
		t.Fatalf("forced structural replacement wrong: %v", x)
	}
	// Stale object ownership must be gone.
	for path := range r2.Owners {
		if strings.HasPrefix(path, ".x.y") || strings.HasPrefix(path, ".x.z") {
			t.Fatalf("stale ownership remained at %s", path)
		}
	}
}

func findContainer(t *testing.T, live any, name string) map[string]any {
	t.Helper()
	for _, c := range containersOf(t, live) {
		if c["name"] == name {
			return c
		}
	}
	t.Fatalf("container %q not found", name)
	return nil
}

func containersOf(t *testing.T, live any) []map[string]any {
	t.Helper()
	spec := live.(map[string]any)["spec"].(map[string]any)
	raw := spec["containers"].([]any)
	out := make([]map[string]any, len(raw))
	for i, e := range raw {
		out[i] = e.(map[string]any)
	}
	return out
}
