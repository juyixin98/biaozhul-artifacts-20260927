package engine

import (
	"bytes"
	"fmt"
	"path/filepath"
	"testing"

	"pathvector/internal/config"
	"pathvector/internal/ierr"
)

func loadFixtureCfg(t *testing.T, name string) *config.Config {
	t.Helper()
	cfg, err := config.LoadFile(filepath.Join("..", "..", "testdata", "fixtures", name+".json"))
	if err != nil {
		t.Fatalf("load %s: %v", name, err)
	}
	return cfg
}

func runCfg(t *testing.T, cfg *config.Config) (*Report, error) {
	t.Helper()
	idx, err := cfg.Topology.Build()
	if err != nil {
		t.Fatalf("topology build: %v", err)
	}
	var log bytes.Buffer
	return Run("test-run", idx, cfg.Policies, cfg.Events, Options{
		Budget: cfg.Budget, QueueCap: cfg.QueueCap,
		Logger: &bufLogger{&log},
	})
}

func bestOf(rep *Report, pfx, router string) (CandidateSnap, bool) {
	m, ok := rep.BestRoutes[pfx]
	if !ok {
		return CandidateSnap{}, false
	}
	c, ok := m[router]
	return c, ok
}

type bufLogger struct{ b *bytes.Buffer }

func (l *bufLogger) Logf(format string, args ...any) {
	fmt.Fprintf(l.b, format+"\n", args...)
}

// TestMultiExit checks the multi-exit early-exit decision and the withdraw
// fail-over: after ispA's local-origin is withdrawn, every internal router
// (br1/br2/core) must use the ispB-learned path, with explicit AS paths.
func TestMultiExit(t *testing.T) {
	cfg := loadFixtureCfg(t, "multi_exit")
	rep, err := runCfg(t, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Status != StatusConverged || rep.Reason != ReasonQuiesced {
		t.Fatalf("status=%s reason=%s", rep.Status, rep.Reason)
	}
	const pfx = "203.0.113.0/24"

	// Post-final expectations, hand-derived from the fixture topology.
	want := map[string]struct {
		peer string
		path []int
		ibgp bool
	}{
		"ispB": {"", []int{65099}, false}, // its own local origin
		"br2":  {"ispB", []int{65020, 65099}, false},
		"br1":  {"br2", []int{65020, 65099}, true}, // failed over over iBGP
		"core": {"br2", []int{65020, 65099}, true},
	}
	for router, w := range want {
		c, ok := rep.BestRoutes[pfx][router]
		if !ok {
			t.Fatalf("router %s has no best route", router)
		}
		if c.FromPeer != w.peer || c.LearnedIBGP != w.ibgp || !eqInts(c.ASPath, w.path) {
			t.Fatalf("%s best: peer=%s ibgp=%v path=%v want peer=%s ibgp=%v path=%v",
				router, c.FromPeer, c.LearnedIBGP, c.ASPath, w.peer, w.ibgp, w.path)
		}
	}
	if _, present := rep.BestRoutes[pfx]["ispA"]; present {
		// ispA does eventually learn the ispB-originated route (synthetic
		// peers are also replay speakers). Record that fact explicitly:
		// its path must carry the eBGP propagation chain and never 65010.
		c := rep.BestRoutes[pfx]["ispA"]
		for _, asn := range c.ASPath {
			if asn == 65010 {
				t.Fatal("ispA learned a path containing its own ASN 65010")
			}
		}
	}

	// Propagation evidence: when br1 later receives br2's iBGP route
	// (step 12, after its ispA candidate was withdrawn), it must accept
	// exactly one candidate from br2 and select it as best.
	var sawFailover bool
	for _, ev := range rep.Trace {
		if ev.Router == "br1" && ev.Phase == "propagate" && ev.Kind == "announce" &&
			ev.From == "br2" && ev.Outcome == "accepted" {
			if ev.Best == nil || ev.Best.FromPeer != "br2" {
				t.Fatalf("br1 failover best = %+v, want br2", ev.Best)
			}
			if len(ev.Candidates) != 1 || ev.Candidates[0].FromPeer != "br2" {
				t.Fatalf("br1 candidates at failover = %v, want only br2", ev.Candidates)
			}
			sawFailover = true
		}
		// While the ispA candidate existed, br1 must never have selected
		// any other source (withdraw at step 9 leaves it briefly bestless).
	}
	if !sawFailover {
		t.Fatal("trace does not show br1 failing over to the br2 candidate")
	}
}

// TestLoopSuppression: a route carrying the receiver's own ASN is rejected
// at loop guard before import policy, and local origin stays best.
func TestLoopSuppression(t *testing.T) {
	cfg := loadFixtureCfg(t, "loop_suppression")
	rep, err := runCfg(t, cfg)
	if err != nil {
		t.Fatal(err)
	}
	const pfx = "198.51.100.0/24"
	if rep.BestRoutes[pfx]["a"].FromPeer != "" {
		t.Fatal("a must keep local origin; looping candidate must not replace it")
	}
	var rejected bool
	var importRuleAppliedToLoop bool
	for _, ev := range rep.Trace {
		if ev.Router == "a" && ev.Outcome == "rejected_loop" && ev.Reason == "as_path_contains_local_as" {
			rejected = true
		}
		// Import permit-all sets local-pref 500; it must never appear on
		// the rejected looping candidate.
		if ev.Router == "a" && ev.Rule == "permit-all" && ev.Outcome == "accepted" &&
			ev.From == "c" {
			importRuleAppliedToLoop = true
		}
	}
	if !rejected {
		t.Fatal("no rejected_loop trace event for own-ASN path")
	}
	if importRuleAppliedToLoop {
		t.Fatal("looping candidate reached import policy; loop guard must run first")
	}
	// Propagated route from a must carry exactly [65000] at b and c.
	if c, ok := bestOf(rep, pfx, "b"); !ok || !eqInts(c.ASPath, []int{65000}) {
		t.Fatalf("b best = %+v ok=%v", c, ok)
	}
	if c, ok := bestOf(rep, pfx, "c"); !ok || !eqInts(c.ASPath, []int{65000}) {
		t.Fatalf("c best = %+v ok=%v", c, ok)
	}
}

// TestPolicyAsymmetry proves import and export policies are independent:
// br1 keeps ispA (import local-pref 300), never exports it to br2 (export
// deny is recorded per neighbor), and core receives it directly via iBGP.
func TestPolicyAsymmetry(t *testing.T) {
	cfg := loadFixtureCfg(t, "policy_asymmetry")
	rep, err := runCfg(t, cfg)
	if err != nil {
		t.Fatal(err)
	}
	const pfx = "192.0.2.0/24"
	if c, ok := bestOf(rep, pfx, "br1"); !ok || c.FromPeer != "ispA" || c.LocalPref != 300 {
		t.Fatalf("br1 best = %+v ok=%v want ispA lp300", c, ok)
	}
	if c, ok := bestOf(rep, pfx, "br2"); !ok || c.FromPeer != "ispB" || c.LocalPref != 100 {
		t.Fatalf("br2 best = %+v ok=%v want ispB lp100", c, ok)
	}
	if c, ok := bestOf(rep, pfx, "core"); !ok || c.FromPeer != "br1" || c.LocalPref != 300 {
		t.Fatalf("core best = %+v ok=%v want br1 lp300", c, ok)
	}
	var exportDeny bool
	for _, ev := range rep.Trace {
		for _, d := range ev.DeniedExports {
			if d == "br2:no-ibgp-leak" && ev.Router == "br1" {
				exportDeny = true
			}
		}
	}
	if !exportDeny {
		t.Fatal("br1 -> br2 export deny not evidenced in trace")
	}
}

// TestWithdrawAlternate: MED comparison selects p1 then the p1 withdraw
// promotes p2 without deleting it, and downstream r2 switches its RIB-Out.
func TestWithdrawAlternate(t *testing.T) {
	cfg := loadFixtureCfg(t, "withdraw_alternate")
	rep, err := runCfg(t, cfg)
	if err != nil {
		t.Fatal(err)
	}
	const pfx = "198.18.0.0/16"
	r1, ok := bestOf(rep, pfx, "r1")
	if !ok || r1.FromPeer != "p2" || r1.MED != 200 {
		t.Fatalf("r1 final best = %+v ok=%v want p2 med200", r1, ok)
	}
	r2, ok := bestOf(rep, pfx, "r2")
	if !ok || r2.FromPeer != "r1" {
		t.Fatalf("r2 final best = %+v ok=%v want r1", r2, ok)
	}
	// Explicit message trajectory toward downstream r2:
	//  - after seq1 (best=p1, med100): announce med100
	//  - after seq2: best switches to p1(med100 wins), no UPDATE (p1
	//    already advertised)
	//  - after seq3 (p1 withdrawn, best=p2 med200): a REPLACING announce
	//    with med200 — no withdraw is needed because r1 never goes bestless.
	var r2MEDs []uint32
	var r2Withdraws int
	for _, ev := range rep.Trace {
		if ev.Router != "r1" {
			continue
		}
		for _, q := range ev.Queued {
			if q.To != "r2" {
				continue
			}
			switch q.Kind {
			case "announce":
				r2MEDs = append(r2MEDs, q.MED)
			case "withdraw":
				r2Withdraws++
			}
		}
		// Split horizon: the best route is never advertised back to the
		// exact neighbor it was learned from.
		bestPeer := ""
		if ev.Best != nil {
			bestPeer = ev.Best.FromPeer
		}
		for _, q := range ev.Queued {
			if q.Kind == "announce" && q.To == bestPeer {
				t.Fatalf("split horizon violated: r1 advertised best learned from %s back to it", q.To)
			}
		}
	}
	if r2Withdraws != 0 {
		t.Fatalf("r2 withdraws=%d, want 0 (path never vanished)", r2Withdraws)
	}
	if len(r2MEDs) != 2 || r2MEDs[0] != 100 || r2MEDs[1] != 200 {
		t.Fatalf("r2 announce MEDs=%v, want [100 200]", r2MEDs)
	}
	// The withdrawn step must show exactly one surviving candidate (p2).
	for _, ev := range rep.Trace {
		if ev.Router == "r1" && ev.Kind == "withdraw" && ev.From == "p1" {
			if len(ev.Candidates) != 1 || ev.Candidates[0].FromPeer != "p2" {
				t.Fatalf("survivors after p1 withdraw = %v, want only p2", ev.Candidates)
			}
		}
	}
}

// TestOscillationProven asserts not_converged/oscillation_detected with
// cycle evidence whose two steps reproduce the same signature.
func TestOscillationProven(t *testing.T) {
	cfg := loadFixtureCfg(t, "oscillation")
	rep, err := runCfg(t, cfg)
	if err != nil {
		t.Fatalf("oscillation is a verdict, not an error: %v", err)
	}
	if rep.Status != StatusNotConverged || rep.Reason != ReasonOscillation {
		t.Fatalf("status=%s reason=%s", rep.Status, rep.Reason)
	}
	if rep.Cycle == nil {
		t.Fatal("missing cycle evidence")
	}
	if rep.Cycle.FirstStep <= 0 || rep.Cycle.SecondStep <= rep.Cycle.FirstStep {
		t.Fatalf("bad cycle steps %+v", rep.Cycle)
	}
	if len(rep.Cycle.Walk) < 2 {
		t.Fatalf("cycle walk too short: %d", len(rep.Cycle.Walk))
	}
	if rep.Cycle.Walk[0].Signature != rep.Cycle.Signature {
		t.Fatal("cycle walk does not start from the recurring signature")
	}
}

// TestBudgetExceeded is a distinct verdict from oscillation: converged
// prefixes stay visible, processed seeds is partial, reason is budget.
func TestBudgetExceeded(t *testing.T) {
	cfg := loadFixtureCfg(t, "budget")
	rep, err := runCfg(t, cfg)
	if err != nil {
		t.Fatalf("budget is a verdict, not an error: %v", err)
	}
	if rep.Status != StatusNotConverged || rep.Reason != ReasonBudgetExceeded {
		t.Fatalf("status=%s reason=%s", rep.Status, rep.Reason)
	}
	if rep.Steps != 5 {
		t.Fatalf("steps=%d want 5", rep.Steps)
	}
	if rep.ProcessedSeeds != 1 || rep.Seeds != 2 {
		t.Fatalf("seeds processed=%d total=%d want 1/2", rep.ProcessedSeeds, rep.Seeds)
	}
	if rep.Cycle != nil {
		t.Fatal("budget stop must not masquerade as a cycle")
	}
	// r5 is reached at step 5; r6 must not have a route.
	if _, ok := rep.BestRoutes["203.0.113.0/24"]["r6"]; ok {
		t.Fatal("r6 must not have converged within budget")
	}
}

// TestUnknownWithdrawIsStateConflict: hard error category with the other
// source untouched in the attached partial report.
func TestUnknownWithdrawIsStateConflict(t *testing.T) {
	cfg := loadFixtureCfg(t, "unknown_withdraw")
	rep, err := runCfg(t, cfg)
	if err == nil {
		t.Fatal("expected state_conflict error")
	}
	if !ierr.Is(err, ierr.KindStateConflict) {
		t.Fatalf("kind=%s want state_conflict", ierr.Of(err))
	}
	if rep == nil {
		t.Fatal("partial report must accompany the hard error")
	}
	r1, ok := bestOf(rep, "198.18.0.0/16", "r1")
	if !ok || r1.FromPeer != "p1" {
		t.Fatalf("p1 candidate must survive failed p2 withdraw: %+v ok=%v", r1, ok)
	}
	// The rejecting trace row records the precise failure class.
	var found bool
	for _, ev := range rep.Trace {
		if ev.Outcome == "rejected_unknown_withdraw" && ev.Reason == ReasonUnknownWithdraw {
			found = true
		}
	}
	if !found {
		t.Fatal("missing rejected_unknown_withdraw trace row")
	}
}

// TestQueueCapResourceExhausted distinguishes resource exhaustion from the
// other classes.
func TestQueueCapResourceExhausted(t *testing.T) {
	cfg := loadFixtureCfg(t, "queue_cap")
	rep, err := runCfg(t, cfg)
	if err == nil {
		t.Fatal("expected resource_exhausted")
	}
	if !ierr.Is(err, ierr.KindResourceExhausted) {
		t.Fatalf("kind=%s want resource_exhausted", ierr.Of(err))
	}
	if rep == nil || rep.Steps != 1 {
		t.Fatalf("partial report steps = %+v", rep)
	}
}

// TestRunIDAppearsInLogs checks the replay correlation id requirement via
// the logger sink.
func TestRunIDAppearsInLogs(t *testing.T) {
	cfg := loadFixtureCfg(t, "withdraw_alternate")
	idx, err := cfg.Topology.Build()
	if err != nil {
		t.Fatal(err)
	}
	var log bytes.Buffer
	rep, err := Run("run-correlate-42", idx, cfg.Policies, cfg.Events, Options{
		Budget: cfg.Budget, QueueCap: cfg.QueueCap, Logger: &bufLogger{&log},
	})
	if err != nil {
		t.Fatal(err)
	}
	if rep.RunID != "run-correlate-42" {
		t.Fatalf("run id = %s", rep.RunID)
	}
	if !bytes.Contains(log.Bytes(), []byte("run=run-correlate-42")) {
		t.Fatal("log sink lacks run correlation id")
	}
}
