package planner

import (
	"testing"

	"infraplanner/internal/model"
	"infraplanner/internal/spec"
)

// desired helpers -----------------------------------------------------------

func vpc(name, cidr, region string, protected bool) model.Desired {
	return model.Desired{Kind: model.KindVPC, Name: name, Protected: protected,
		Attrs: map[string]string{"cidr": cidr, "region": region}}
}
func subnet(name, cidr, zone, vpcName string) model.Desired {
	return model.Desired{Kind: model.KindSubnet, Name: name,
		Attrs: map[string]string{"cidr": cidr, "zone": zone},
		Refs:  map[string]model.Ref{"network_ref": {Kind: model.KindVPC, Name: vpcName}}}
}
func instance(name, image, shape, subnetName string) model.Desired {
	return model.Desired{Kind: model.KindInstance, Name: name,
		Attrs: map[string]string{"image": image, "shape": shape},
		Refs:  map[string]model.Ref{"subnet_ref": {Kind: model.KindSubnet, Name: subnetName}}}
}
func bucket(name string) model.Desired {
	return model.Desired{Kind: model.KindBucket, Name: name}
}

func mustParse(t *testing.T, res []model.Desired) *spec.Spec {
	t.Helper()
	p, err := spec.Parse(res)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return p
}

func liveOf(d model.Desired, id string, protected bool) model.Live {
	return model.Live{Key: d.Key(), ID: id, Attrs: d.Attrs, Refs: d.Refs, Protected: protected}
}

// tests ---------------------------------------------------------------------

func TestBuild_CreateOrderFollowsDependencies(t *testing.T) {
	desired := []model.Desired{
		instance("api", "img", "small", "web"),
		subnet("web", "10.1/16", "z1", "main"),
		vpc("main", "10/8", "east", false),
	}
	p, err := Build(Input{Spec: mustParse(t, desired)})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 3 {
		t.Fatalf("ops = %d, want 3: %+v", len(p.Operations), p.Operations)
	}
	wantOrder := []model.Key{
		{Kind: model.KindVPC, Name: "main"},
		{Kind: model.KindSubnet, Name: "web"},
		{Kind: model.KindInstance, Name: "api"},
	}
	for i, want := range wantOrder {
		if p.Operations[i].Type != OpCreate || p.Operations[i].Key != want {
			t.Fatalf("op[%d] = %s %s, want create %s", i, p.Operations[i].Type, p.Operations[i].Key, want)
		}
		if p.Operations[i].Seq != i+1 {
			t.Fatalf("op[%d].Seq = %d, want %d", i, p.Operations[i].Seq, i+1)
		}
	}
}

func TestBuild_DeleteOrderIsReverseOfCreate(t *testing.T) {
	// Live world has vpc<-subnet<-instance; desired spec deletes subnet+
	// instance but keeps vpc. Destruction must be instance then subnet.
	live := []model.Live{
		liveOf(vpc("main", "10/8", "east", false), "id-vpc", false),
		liveOf(subnet("web", "10.1/16", "z1", "main"), "id-sub", false),
		liveOf(instance("api", "img", "small", "web"), "id-inst", false),
	}
	desired := []model.Desired{vpc("main", "10/8", "east", false)}
	p, err := Build(Input{
		Spec:     mustParse(t, desired),
		Observed: model.Observation{Resources: live},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 2 {
		t.Fatalf("ops = %d, want 2", len(p.Operations))
	}
	if p.Operations[0].Type != OpDelete || p.Operations[0].Key.Name != "api" ||
		p.Operations[0].ExistingID != "id-inst" {
		t.Fatalf("first delete = %+v, want delete instance/api id-inst", p.Operations[0])
	}
	if p.Operations[1].Type != OpDelete || p.Operations[1].Key.Name != "web" {
		t.Fatalf("second delete = %+v, want delete subnet/web", p.Operations[1])
	}
}

func TestBuild_ImmutableAttrChangeIsReplace(t *testing.T) {
	old := vpc("main", "10/8", "east", false)
	newV := vpc("main", "172.16/12", "east", false) // cidr is immutable
	p, err := Build(Input{
		Spec:     mustParse(t, []model.Desired{newV}),
		Observed: model.Observation{Resources: []model.Live{liveOf(old, "id-1", false)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 2 {
		t.Fatalf("ops = %d, want 2 (delete+create): %+v", len(p.Operations), p.Operations)
	}
	if p.Operations[0].Type != OpDelete || p.Operations[0].ExistingID != "id-1" {
		t.Fatalf("op0 = %+v, want delete old id-1", p.Operations[0])
	}
	if p.Operations[1].Type != OpCreate {
		t.Fatalf("op1 = %+v, want create new", p.Operations[1])
	}
	// New create must not carry the old physical id (replacement identity).
	if p.Operations[1].ExistingID != "" {
		t.Fatalf("replace create must not reuse id, got %q", p.Operations[1].ExistingID)
	}
}

func TestBuild_MutableAttrChangeIsInPlaceUpdate(t *testing.T) {
	old := instance("api", "img", "small", "web")
	newI := instance("api", "img", "large", "web") // shape is mutable
	base := []model.Live{
		liveOf(vpc("main", "10/8", "east", false), "id-vpc", false),
		liveOf(subnet("web", "10.1/16", "z1", "main"), "id-sub", false),
		liveOf(old, "id-inst", false),
	}
	desired := []model.Desired{
		vpc("main", "10/8", "east", false), subnet("web", "10.1/16", "z1", "main"), newI,
	}
	p, err := Build(Input{Spec: mustParse(t, desired), Observed: model.Observation{Resources: base}})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 1 {
		t.Fatalf("ops = %d, want 1: %+v", len(p.Operations), p.Operations)
	}
	op := p.Operations[0]
	if op.Type != OpUpdate || op.Key.Name != "api" || op.ExistingID != "id-inst" {
		t.Fatalf("op = %+v, want in-place update of id-inst", op)
	}
	if len(op.Changes) != 1 || op.Changes[0].Field != "shape" ||
		op.Changes[0].Old != "small" || op.Changes[0].New != "large" {
		t.Fatalf("changes = %+v, want shape small->large", op.Changes)
	}
}

func TestBuild_RefChangeIsReplace(t *testing.T) {
	base := []model.Live{
		liveOf(vpc("a", "10/8", "east", false), "id-a", false),
		liveOf(vpc("b", "172.16/12", "west", false), "id-b", false),
		liveOf(subnet("web", "10.1/16", "z1", "a"), "id-sub", false),
	}
	desired := []model.Desired{
		vpc("a", "10/8", "east", false), vpc("b", "172.16/12", "west", false),
		subnet("web", "10.1/16", "z1", "b"), // network_ref moved a->b
	}
	p, err := Build(Input{Spec: mustParse(t, desired), Observed: model.Observation{Resources: base}})
	if err != nil {
		t.Fatal(err)
	}
	var hasReplaceCreate, hasDeleteOld bool
	for _, op := range p.Operations {
		if op.Key.Name == "web" && op.Type == OpDelete && op.ExistingID == "id-sub" {
			hasDeleteOld = true
		}
		if op.Key.Name == "web" && op.Type == OpCreate {
			hasReplaceCreate = true
		}
	}
	if !hasDeleteOld || !hasReplaceCreate {
		t.Fatalf("ref change must replace subnet/web: %+v", p.Operations)
	}
}

func TestBuild_GuardedDeleteRequiresExplicitRelease(t *testing.T) {
	old := vpc("main", "10/8", "east", true) // protected live resource
	p, err := Build(Input{
		Spec:     mustParse(t, []model.Desired{}),
		Observed: model.Observation{Resources: []model.Live{liveOf(old, "id-1", true)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Guarded) != 1 || p.Guarded[0] != old.Key() {
		t.Fatalf("guarded = %v, want [vpc/main]", p.Guarded)
	}

	// With an explicit release the key is no longer guarded.
	p2, err := Build(Input{
		Spec:           mustParse(t, []model.Desired{}),
		Observed:       model.Observation{Resources: []model.Live{liveOf(old, "id-1", true)}},
		ReleasedGuards: map[model.Key]bool{old.Key(): true},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(p2.Guarded) != 0 {
		t.Fatalf("after release guarded = %v, want empty", p2.Guarded)
	}
	if len(p2.Operations) != 1 || p2.Operations[0].Type != OpDelete {
		t.Fatalf("after release ops = %+v, want one delete", p2.Operations)
	}
}

func TestBuild_InSyncProducesNoopsAndEmptyPlan(t *testing.T) {
	d := []model.Desired{
		vpc("main", "10/8", "east", false),
		bucket("artifacts"),
	}
	live := []model.Live{
		liveOf(d[0], "id-vpc", false),
		liveOf(d[1], "id-bkt", false),
	}
	p, err := Build(Input{Spec: mustParse(t, d), Observed: model.Observation{Resources: live}})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 0 {
		t.Fatalf("expected no ops for in-sync world, got %+v", p.Operations)
	}
}

func TestFingerprint_StableAndDriftSensitive(t *testing.T) {
	obs := model.Observation{Resources: []model.Live{
		liveOf(vpc("main", "10/8", "east", false), "id-1", false),
		liveOf(bucket("b"), "id-2", false),
	}}
	// order independence
	obsReordered := model.Observation{Resources: []model.Live{obs.Resources[1], obs.Resources[0]}}
	if Fingerprint(obs) != Fingerprint(obsReordered) {
		t.Fatal("fingerprint must be order independent")
	}
	drifted := obs
	drifted.Resources = []model.Live{
		{Key: obs.Resources[0].Key, ID: "id-1",
			Attrs: map[string]string{"cidr": "10/8", "region": "west"}, Refs: map[string]model.Ref{}},
		obs.Resources[1],
	}
	if Fingerprint(obs) == Fingerprint(drifted) {
		t.Fatal("changed attribute must change fingerprint")
	}
}

func TestBuild_ReplaceWaveRespectsDependents(t *testing.T) {
	// Replace the VPC (immutable cidr). Subnet + instance must be destroyed
	// first (instance, subnet, vpc) and recreated last (vpc, subnet,
	// instance). Everything is one dependency chain.
	base := []model.Desired{
		vpc("main", "10/8", "east", false),
		subnet("web", "10.1/16", "z1", "main"),
		instance("api", "img", "small", "web"),
	}
	live := []model.Live{
		liveOf(base[0], "id-vpc", false),
		liveOf(base[1], "id-sub", false),
		liveOf(base[2], "id-inst", false),
	}
	target := []model.Desired{
		vpc("main", "172.16/12", "east", false),
		subnet("web", "10.1/16", "z1", "main"),
		instance("api", "img", "small", "web"),
	}
	p, err := Build(Input{Spec: mustParse(t, target), Observed: model.Observation{Resources: live}})
	if err != nil {
		t.Fatal(err)
	}
	// 3 destroys + 3 creates
	if len(p.Operations) != 6 {
		t.Fatalf("ops = %d, want 6: %+v", len(p.Operations), p.Operations)
	}
	destroySeq := []string{"api", "web", "main"}
	createSeq := []string{"main", "web", "api"}
	for i := 0; i < 3; i++ {
		if p.Operations[i].Type != OpDelete || p.Operations[i].Key.Name != destroySeq[i] {
			t.Fatalf("destroy[%d]=%+v want delete %s", i, p.Operations[i], destroySeq[i])
		}
		if p.Operations[i+3].Type != OpCreate || p.Operations[i+3].Key.Name != createSeq[i] {
			t.Fatalf("create[%d]=%+v want create %s", i, p.Operations[i+3], createSeq[i])
		}
	}
}
