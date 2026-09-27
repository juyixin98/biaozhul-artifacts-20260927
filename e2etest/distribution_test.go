// Package e2etest holds the end-to-end acceptance tests that drive the real
// modules together: fixed 20k synthetic 5-tuple fixture, distribution,
// membership-change migration against a full re-modulo baseline, zero
// weights, all-down failover and concurrent reads.
package e2etest

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"
	"time"

	"flexhash/internal/fherr"
	"flexhash/internal/flow"
	"flexhash/internal/hashring"
	"flexhash/internal/oracle"
	"flexhash/internal/store"
	"flexhash/internal/testlog"
)

const fixturePath = "../testdata/flows/flows.json"

type fixtureFile struct {
	Seed  uint64 `json:"seed"`
	Count int    `json:"count"`
	Flows []struct {
		SrcIP    string `json:"src_ip"`
		SrcPort  uint16 `json:"src_port"`
		DstIP    string `json:"dst_ip"`
		DstPort  uint16 `json:"dst_port"`
		Protocol string `json:"protocol"`
	} `json:"flows"`
}

// loadedFlows caches the parsed fixture.
var (
	loadOnce sync.Once
	keys     []string
	loadErr  error
)

func loadKeys(t *testing.T) []string {
	t.Helper()
	loadOnce.Do(func() {
		b, err := os.ReadFile(fixturePath)
		if err != nil {
			loadErr = err
			return
		}
		var fx fixtureFile
		if err := json.Unmarshal(b, &fx); err != nil {
			loadErr = err
			return
		}
		if fx.Count != 20000 || len(fx.Flows) != 20000 {
			loadErr = &countMismatch{fx.Count}
			return
		}
		keys = make([]string, len(fx.Flows))
		for i, rf := range fx.Flows {
			tpl := flow.FiveTuple{
				SrcIP: rf.SrcIP, SrcPort: rf.SrcPort,
				DstIP: rf.DstIP, DstPort: rf.DstPort, Protocol: rf.Protocol,
			}
			k, err := tpl.Key()
			if err != nil {
				loadErr = err
				return
			}
			keys[i] = k
		}
	})
	if loadErr != nil {
		t.Fatalf("load fixture: %v", loadErr)
	}
	return keys
}

type countMismatch struct{ n int }

func (e *countMismatch) Error() string { return "fixture flow count mismatch" }

// flowOwners resolves every flow against a ring (all members assumed up;
// Chosen == structural owner then).
func flowOwners(ring *hashring.Ring, ks []string) []string {
	out := make([]string, len(ks))
	for i, k := range ks {
		out[i] = ring.Owner(ring.BucketOf(k))
	}
	return out
}

func countBy(members []string) map[string]int {
	m := map[string]int{}
	for _, x := range members {
		m[x]++
	}
	return m
}

func sortedIDs(members []hashring.Member) []string {
	out := make([]string, 0, len(members))
	for _, m := range members {
		out = append(out, m.ID)
	}
	sort.Strings(out)
	return out
}

// weightedSlots is the naive weighted re-modulo baseline: member i occupies
// weight[i] contiguous slots of [0,totalWeight); owner = slots[hash%total].
func weightedSlots(members []hashring.Member) []string {
	slots := make([]string, 0)
	for _, m := range members { // members passed sorted by ID
		for j := 0; j < m.Weight; j++ {
			slots = append(slots, m.ID)
		}
	}
	return slots
}

func modWeightedOwner(key string, slots []string) string {
	return slots[oracle.FlowBucket(key, len(slots))]
}

// TestDistributionAndMigration is the main acceptance test. It records a
// replayable run log with run id, every intermediate quota/flow-share state
// and the reason behind each assertion.
func TestDistributionAndMigration(t *testing.T) {
	ks := loadKeys(t)
	const B = 1024
	lg := testlog.New(t, "distribution-and-migration")
	defer func() {
		persistRun(t, lg)
		lg.Finish(!t.Failed())
	}()

	mgr := hashring.NewManager(B)

	// ---- v1: 1:1:1 ----
	v1 := []hashring.Member{
		{ID: "a", Address: "10.0.0.1:1", Weight: 1, Healthy: true},
		{ID: "b", Address: "10.0.0.1:2", Weight: 1, Healthy: true},
		{ID: "c", Address: "10.0.0.1:3", Weight: 1, Healthy: true},
	}
	r1, _, err := mgr.Bootstrap(1, v1, 0)
	if err != nil {
		lg.Failf(fherr.KindComputationFailed.String(), "bootstrap v1", "%v", err)
	}
	q1 := r1.Quota()
	owners1 := flowOwners(r1, ks)
	shares1 := shares(owners1, sortedIDs(v1), len(ks))
	lg.Info("v1 a:b:c=1:1:1", map[string]any{
		"bucket_quota": q1, "flow_shares": shares1,
	})
	if q1["a"] != 342 || q1["b"] != 341 || q1["c"] != 341 {
		lg.Failf(fherr.KindComputationFailed.String(), "v1 exact quota",
			"got %v want a=342 b=341 c=341 (Hamilton, tie->a)", q1)
	}
	assertShareNear(t, lg, "v1", shares1, q1, B, 0.03)

	// ---- v2: add d (1:1:1:1): compare with BOTH baselines ----
	v2 := append(append([]hashring.Member{}, v1...),
		hashring.Member{ID: "d", Address: "10.0.0.1:4", Weight: 1, Healthy: true})
	r2, moved2, err := mgr.ApplyConfig(2, v2)
	if err != nil {
		lg.Failf(fherr.KindComputationFailed.String(), "apply v2", "%v", err)
	}
	ringMoved2 := countMoves(owners1, flowOwners(r2, ks))
	modMoved2 := 0
	slots1, slots2 := weightedSlots(sortedMembers(v1)), weightedSlots(sortedMembers(v2))
	ids1, ids2 := sortedIDs(v1), sortedIDs(v2)
	for _, k := range ks {
		if oracle.ModN(k, ids1) != oracle.ModN(k, ids2) {
			modMoved2++
		}
	}
	weightedMoved2 := 0
	for _, k := range ks {
		if modWeightedOwner(k, slots1) != modWeightedOwner(k, slots2) {
			weightedMoved2++
		}
	}
	lg.Info("v2 add d", map[string]any{
		"buckets_moved":            len(moved2),
		"flows_moved_ring":         ringMoved2,
		"flows_moved_modN":         modMoved2,
		"flows_moved_weighted_mod": weightedMoved2,
		"flow_fraction_ring":       float64(ringMoved2) / float64(len(ks)),
		"flow_fraction_modN":       float64(modMoved2) / float64(len(ks)),
	})
	// Bucket-locality: a flow moves iff its bucket moved (no scattered
	// rehash); and every moving flow lands on the added member d.
	movedSet := map[int]bool{}
	for _, b := range moved2 {
		movedSet[b] = true
	}
	for i, k := range ks {
		b := r2.BucketOf(k)
		changed := owners1[i] != r2.Owner(b)
		if changed != movedSet[b] {
			lg.Failf(fherr.KindComputationFailed.String(), "v2 bucket locality",
				"flow %d (bucket %d): changed=%v but bucket moved=%v", i, b, changed, movedSet[b])
		}
		if changed && r2.Owner(b) != "d" {
			lg.Failf(fherr.KindComputationFailed.String(), "v2 add locality",
				"flow moved to %q, only new member d should receive traffic", r2.Owner(b))
		}
	}
	lg.Pass("v2 bucket locality", "flows move exactly with their bucket and only toward added member d", nil)
	// Exactly 256 buckets (25%) moved — the same theoretical fraction as
	// mod-N here, but ring keeps whole buckets sticky.
	if len(moved2) != 256 {
		lg.Failf(fherr.KindComputationFailed.String(), "v2 moved bucket count",
			"got %d want 256", len(moved2))
	}
	q2 := r2.Quota()
	for _, id := range ids2 {
		if q2[id] != 256 {
			lg.Failf(fherr.KindComputationFailed.String(), "v2 equal quota",
				"%s=%d want 256", id, q2[id])
		}
	}

	// ---- v3: remove c; only c's flows relocate ----
	v3 := []hashring.Member{v1[0], v1[1], v2[3]} // a,b,d
	r3, moved3, err := mgr.ApplyConfig(3, v3)
	if err != nil {
		lg.Failf(fherr.KindComputationFailed.String(), "apply v3", "%v", err)
	}
	owners3 := flowOwners(r3, ks)
	// Compare against the immediately preceding state r2 for exact locality.
	owners2 := flowOwners(r2, ks)
	ringMoved3 := 0
	for i := range ks {
		before, after := owners2[i], owners3[i]
		if before != after {
			ringMoved3++
			if before != "c" {
				lg.Failf(fherr.KindComputationFailed.String(), "v3 only c buckets",
					"flow %d moved although its member %q survives", i, before)
			}
		}
	}
	modMoved3 := 0
	ids3 := sortedIDs(v3)
	for _, k := range ks {
		if oracle.ModN(k, ids2) != oracle.ModN(k, ids3) {
			modMoved3++
		}
	}
	// Under mod-N, flows that stay under the same name are only 3/4; the
	// moved set is scattered across the surviving members arbitrarily.
	// Under the ring, 100% of moved flows came from c. Verify all of c's
	// flows moved (they had nowhere to stay) and no others.
	cFlows := 0
	for _, o := range owners2 {
		if o == "c" {
			cFlows++
		}
	}
	lg.Info("v3 remove c", map[string]any{
		"buckets_moved":         len(moved3),
		"c_flows_before":        cFlows,
		"flows_moved_ring":      ringMoved3,
		"flows_moved_modN":      modMoved3,
		"ring_moved_all_from_c": true,
	})
	if ringMoved3 != cFlows || len(moved3) != q2["c"] {
		lg.Failf(fherr.KindComputationFailed.String(), "v3 moved exactly c",
			"ring moved flows %d vs c flows %d; buckets moved %d vs c quota %d",
			ringMoved3, cFlows, len(moved3), q2["c"])
	}
	if float64(ringMoved3)/float64(len(ks)) > float64(modMoved3)/float64(len(ks))+0.01 {
		lg.Failf(fherr.KindComputationFailed.String(), "v3 vs modN fraction",
			"ring moved %.3f of flows but modN %.3f",
			float64(ringMoved3)/float64(len(ks)), float64(modMoved3)/float64(len(ks)))
	}
	lg.Pass("v3 minimal removal", "only c-owned buckets relocated; all survivors kept theirs", nil)

	// ---- v4: reweight a:b:d = 3:1:1 ----
	v4 := []hashring.Member{
		{ID: "a", Address: "10.0.0.1:1", Weight: 3, Healthy: true},
		{ID: "b", Address: "10.0.0.1:2", Weight: 1, Healthy: true},
		{ID: "d", Address: "10.0.0.1:4", Weight: 1, Healthy: true},
	}
	r4, moved4, err := mgr.ApplyConfig(4, v4)
	if err != nil {
		lg.Failf(fherr.KindComputationFailed.String(), "apply v4", "%v", err)
	}
	q4 := r4.Quota()
	if q4["a"] != 614 || q4["b"] != 205 || q4["d"] != 205 {
		lg.Failf(fherr.KindComputationFailed.String(), "v4 exact quota",
			"got %v want a=614 b=205 d=205", q4)
	}
	owners4 := flowOwners(r4, ks)
	ringMoved4 := countMoves(owners3, owners4)
	slots3, slots4 := weightedSlots(sortedMembers(v3)), weightedSlots(sortedMembers(v4))
	weightedMoved4 := 0
	for _, k := range ks {
		if modWeightedOwner(k, slots3) != modWeightedOwner(k, slots4) {
			weightedMoved4++
		}
	}
	// Plain mod-N ignores weights: it would move ZERO flows while completely
	// violating the configured 3:1:1 target.
	modZero := 0
	for _, k := range ks {
		if oracle.ModN(k, ids3) != oracle.ModN(k, ids3) {
			modZero++
		}
	}
	shares4 := shares(owners4, ids3, len(ks))
	lg.Info("v4 reweight 3:1:1", map[string]any{
		"bucket_quota":                q4,
		"flow_shares":                 shares4,
		"buckets_moved_ring":          len(moved4),
		"flows_moved_ring":            ringMoved4,
		"flows_moved_weighted_mod":    weightedMoved4,
		"flows_moved_plain_modN":      modZero,
		"plain_modN_violates_weights": true,
		"ring_vs_weighted_mod_ratio":  float64(ringMoved4) / float64(max1(weightedMoved4)),
	})
	if len(moved4) != 272 {
		lg.Failf(fherr.KindComputationFailed.String(), "v4 minimal movement",
			"buckets moved %d want exactly 272 (b loses 136, d loses 136)", len(moved4))
	}
	if ringMoved4 >= weightedMoved4 {
		lg.Failf(fherr.KindComputationFailed.String(), "v4 beats weighted remod",
			"ring moved %d flows, naive weighted re-mod moved %d; ring must move strictly fewer",
			ringMoved4, weightedMoved4)
	}
	assertShareNear(t, lg, "v4", shares4, q4, B, 0.03)
	// And the realized shares must actually reflect 3:1:1 (what plain mod-N
	// cannot do): a must carry roughly 60%.
	if shares4["a"] < 0.55 {
		lg.Failf(fherr.KindComputationFailed.String(), "v4 weight honored",
			"a flow share %.3f want ~0.6", shares4["a"])
	}
	lg.Pass("v4 weighted with minimal churn",
		"3:1:1 honored with 272/1024 bucket moves vs a near-total weighted re-mod", nil)
}

// TestBucketShareVsTrafficShare proves the two share notions differ when a
// weighted member is down: structural bucket ownership stays fixed while
// realized traffic shifts via weighted failover.
func TestBucketShareVsTrafficShare(t *testing.T) {
	ks := loadKeys(t)
	const B = 1024
	lg := testlog.New(t, "bucket-vs-traffic-share")
	defer func() { persistRun(t, lg); lg.Finish(!t.Failed()) }()

	mgr := hashring.NewManager(B)
	members := []hashring.Member{
		{ID: "a", Address: "h1", Weight: 3, Healthy: true},
		{ID: "b", Address: "h2", Weight: 1, Healthy: true},
		{ID: "d", Address: "h4", Weight: 1, Healthy: true},
	}
	ring, _, _ := mgr.Bootstrap(1, members, 0)
	bucketShare := ring.Quota()

	up := map[string]int{}
	for _, k := range ks {
		d, err := mgr.Resolve(k)
		if err != nil || d.Chosen == "" {
			t.Fatalf("resolve: %v", err)
		}
		up[d.Chosen]++
	}
	rev, err := mgr.SetHealth("a", false)
	if err != nil {
		t.Fatal(err)
	}
	downRing, rev, _ := mgr.Snapshot()
	downTraffic := map[string]int{}
	failoverTo := map[string]int{}
	for _, k := range ks {
		d := hashring.ResolveOn(downRing, rev, k)
		if d.Chosen == "" {
			t.Fatal("unavailable while b,d up")
		}
		downTraffic[d.Chosen]++
		if d.Failover {
			failoverTo[d.Chosen]++
		}
	}
	// Structural ownership is untouched by health.
	postQuota := downRing.Quota()
	lg.Info("a down: structural vs realized", map[string]any{
		"bucket_quota_unchanged": postQuota,
		"traffic_when_up":        up,
		"traffic_when_a_down":    downTraffic,
		"a_buckets_failed_over":  failoverTo,
	})
	if postQuota["a"] != bucketShare["a"] || postQuota["b"] != bucketShare["b"] {
		lg.Failf(fherr.KindComputationFailed.String(), "structural frozen",
			"quota changed with health: before %v after %v", bucketShare, postQuota)
	}
	if downTraffic["a"] != 0 {
		lg.Failf(fherr.KindUnavailable.String(), "down member excluded",
			"a still received %d flows while marked down", downTraffic["a"])
	}
	// a's traffic must be split between b and d proportional to equal
	// weights 1:1 — within ±4% absolute of an even split.
	fa, fb := float64(failoverTo["b"]), float64(failoverTo["d"])
	if math.Abs(fa-fb)/(fa+fb) > 0.08 {
		lg.Failf(fherr.KindComputationFailed.String(), "weighted failover",
			"failover split b=%d d=%d not ~1:1", failoverTo["b"], failoverTo["d"])
	}
	// b's and d's own flows must remain pinned (no secondary movement).
	bOwnKept := 0
	for _, k := range ks {
		b := ring.BucketOf(k)
		if ring.Owner(b) == "b" {
			if hashring.ResolveOn(downRing, rev, k).Chosen == "b" {
				bOwnKept++
			}
		}
	}
	if bOwnKept != up["b"] {
		lg.Failf(fherr.KindComputationFailed.String(), "healthy owners pinned",
			"b kept %d of its %d flows", bOwnKept, up["b"])
	}
	lg.Pass("shares distinguished",
		"bucket ownership frozen while realized traffic fails over 1:1", nil)
}

// TestZeroWeightAndAllDown covers the two boundary configurations.
func TestZeroWeightAndAllDown(t *testing.T) {
	ks := loadKeys(t)
	lg := testlog.New(t, "zero-weight-and-all-down")
	defer func() { persistRun(t, lg); lg.Finish(!t.Failed()) }()

	const B = 256
	mgr := hashring.NewManager(B)
	members := []hashring.Member{
		{ID: "a", Address: "h", Weight: 2, Healthy: true},
		{ID: "b", Address: "h", Weight: 1, Healthy: true},
		{ID: "z", Address: "h", Weight: 0, Healthy: true},
	}
	ring, _, err := mgr.Bootstrap(1, members, 0)
	if err != nil {
		lg.Failf(fherr.KindComputationFailed.String(), "bootstrap with zero weight", "%v", err)
	}
	q := ring.Quota()
	if q["z"] != 0 || q["a"] != 171 || q["b"] != 85 {
		// 256*2/3=170.67 -> 171; 256/3=85.33 -> 85.
		lg.Failf(fherr.KindComputationFailed.String(), "zero-weight quota",
			"got %v want a=171 b=85 z=0", q)
	}
	for _, k := range ks {
		d, _ := mgr.Resolve(k)
		if d.Chosen == "z" {
			lg.Failf(fherr.KindComputationFailed.String(), "zero weight never chosen",
				"flow routed to z")
		}
	}
	lg.Pass("zero weight member holds no bucket/traffic", "quota z=0", q)

	// Marking the sole weighted members down -> unavailable decisions.
	if _, err := mgr.SetHealth("a", false); err != nil {
		t.Fatal(err)
	}
	if _, err := mgr.SetHealth("b", false); err != nil {
		t.Fatal(err)
	}
	ring2, rev, _ := mgr.Snapshot()
	unavail := 0
	for _, k := range ks {
		d := hashring.ResolveOn(ring2, rev, k)
		if d.Chosen == "" {
			unavail++
		}
	}
	lg.Info("all weighted members down", map[string]any{
		"unavailable_flows": unavail, "total": len(ks),
	})
	if unavail != len(ks) {
		lg.Failf(fherr.KindUnavailable.String(), "all down unavailable",
			"%d/%d flows still resolved", len(ks)-unavail, len(ks))
	}
}

// TestConcurrentReadsDuringConfigWaves runs under -race: many lock-free
// readers while a single writer advances versions. Readers must always see
// a self-consistent snapshot and never panic.
func TestConcurrentReadsDuringConfigWaves(t *testing.T) {
	ks := loadKeys(t)
	const B = 512
	lg := testlog.New(t, "concurrent-reads")
	defer func() { persistRun(t, lg); lg.Finish(!t.Failed()) }()

	mgr := hashring.NewManager(B)
	base := []hashring.Member{
		{ID: "a", Address: "h", Weight: 1, Healthy: true},
		{ID: "b", Address: "h", Weight: 1, Healthy: true},
	}
	if _, _, err := mgr.Bootstrap(1, base, 0); err != nil {
		t.Fatal(err)
	}

	stop := make(chan struct{})
	var wg sync.WaitGroup
	var readerErr sync.Map
	for r := 0; r < 16; r++ {
		wg.Add(1)
		go func(seed int) {
			defer wg.Done()
			i := 0
			for {
				select {
				case <-stop:
					return
				default:
				}
				ring, hrev, err := mgr.Snapshot()
				if err != nil {
					readerErr.Store("snapshot", err.Error())
					return
				}
				k := ks[(seed*7919+i)%len(ks)]
				d := hashring.ResolveOn(ring, hrev, k)
				if d.Bucket < 0 || d.Bucket >= B || d.Chosen == "" {
					readerErr.Store("decision", "invalid decision observed")
					return
				}
				if ring.Owner(d.Bucket) == "" {
					readerErr.Store("owner", "empty owner in published snapshot")
					return
				}
				i++
			}
		}(r + 1)
	}

	// Single writer advances versions add/remove alternately.
	const waves = 40
	writerDone := make(chan struct{})
	go func() {
		defer close(writerDone)
		for v := int64(2); v <= int64(waves)+1; v++ {
			var ms []hashring.Member
			if v%2 == 0 {
				ms = []hashring.Member{
					{ID: "a", Address: "h", Weight: 1, Healthy: true},
					{ID: "b", Address: "h", Weight: 1, Healthy: true},
					{ID: "c", Address: "h", Weight: 1, Healthy: true},
				}
			} else {
				ms = []hashring.Member{
					{ID: "a", Address: "h", Weight: 1, Healthy: true},
					{ID: "b", Address: "h", Weight: 1, Healthy: true},
				}
			}
			if _, _, err := mgr.ApplyConfig(v, ms); err != nil {
				readerErr.Store("writer", err.Error())
				return
			}
			time.Sleep(time.Millisecond)
		}
	}()

	<-writerDone
	close(stop)
	wg.Wait()
	readerErr.Range(func(k, v any) bool {
		t.Fatalf("reader/writer failure %s: %s", k, v)
		return false
	})
	ring, hrev, _ := mgr.Snapshot()
	lg.Info("after concurrent waves", map[string]any{
		"final_version": ring.Version, "health_rev": hrev, "readers": 16, "waves": waves,
	})
	if ring.Version != int64(waves)+1 {
		lg.Failf(fherr.KindStateConflict.String(), "final version",
			"got %d want %d", ring.Version, waves+1)
	}
	lg.Pass("concurrent reads consistent", "16 readers saw only complete snapshots across 40 waves", nil)
}

// ---- helpers ----

func countMoves(a, b []string) int {
	n := 0
	for i := range a {
		if a[i] != b[i] {
			n++
		}
	}
	return n
}

func shares(owners []string, ids []string, total int) map[string]float64 {
	c := countBy(owners)
	out := make(map[string]float64, len(ids))
	for _, id := range ids {
		out[id] = float64(c[id]) / float64(total)
	}
	return out
}

func assertShareNear(t *testing.T, lg *testlog.Logger, phase string,
	flowShare map[string]float64, quota map[string]int, buckets int, tol float64) {
	t.Helper()
	for id, q := range quota {
		bucketShare := float64(q) / float64(buckets)
		if math.Abs(flowShare[id]-bucketShare) > tol {
			lg.Failf(fherr.KindComputationFailed.String(), phase+" share gap",
				"member %s flow share %.4f differs from bucket share %.4f by >%.2f",
				id, flowShare[id], bucketShare, tol)
		}
	}
	lg.Pass(phase+" flow share ~= bucket share",
		"per-member gap within tolerance of uniform 20k flow hashing",
		map[string]any{"flow_share": flowShare,
			"bucket_share": quotaToShare(quota, buckets), "tolerance": tol})
}

func quotaToShare(q map[string]int, b int) map[string]float64 {
	out := make(map[string]float64, len(q))
	for k, v := range q {
		out[k] = float64(v) / float64(b)
	}
	return out
}

func sortedMembers(ms []hashring.Member) []hashring.Member {
	out := append([]hashring.Member(nil), ms...)
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func max1(n int) int {
	if n < 1 {
		return 1
	}
	return n
}

// persistRun writes the run record into the retained testdata/runs/runlog.db
// so that run id is replayable after the test process exits (and can be
// inspected through GET /v1/runs when the server points at this database).
func persistRun(t *testing.T, lg *testlog.Logger) {
	t.Helper()
	dbPath := filepath.Join("..", "testdata", "runs", "runlog.db")
	st, err := store.Open(context.Background(), dbPath)
	if err != nil {
		t.Logf("persistRun open: %v", err)
		return
	}
	defer st.Close()
	detail := map[string]any{"steps": len(lg.Steps)}
	if err := st.SaveRunLog(context.Background(), lg.RunID, lg.TestName, lg.Result, detail); err != nil {
		t.Logf("persistRun save: %v", err)
	}
}
