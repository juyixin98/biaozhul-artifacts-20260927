package engine_test

import (
	"testing"

	"pvsim/config"
	"pvsim/engine"
	"pvsim/model"
)

// runScn parses and executes a scenario JSON document with a collector.
func runScn(t *testing.T, raw string) (*engine.Result, *engine.Collector, *config.Scenario) {
	t.Helper()
	sc, err := config.Parse([]byte(raw))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	col := engine.NewCollector()
	res, err := engine.Run(sc, engine.Options{}, col)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	return res, col, sc
}

func bestPeer(res *engine.Result, router, prefix string) (string, bool) {
	for _, v := range res.Best[router] {
		if v.Prefix == prefix {
			return v.Peer, true
		}
	}
	return "", false
}

func countCategory(col *engine.Collector, category string) int {
	n := 0
	for _, tr := range col.Traces {
		if tr.Category == category {
			n++
		}
	}
	return n
}

func decisionsFor(col *engine.Collector, router, prefix string) []engine.Decision {
	var out []engine.Decision
	for _, d := range col.Decisions {
		if d.Router == router && d.Prefix == prefix {
			out = append(out, d)
		}
	}
	return out
}

// Inbound AS_PATH loop rejection: when an export policy forces the
// advertised AS_PATH to already contain the *receiving* AS (e.g. AS
// prepending the neighbor), the receiving speaker must reject the UPDATE
// rather than install it; the rejection must not disturb its best route.
func TestInboundLoopRejection(t *testing.T) {
	raw := `{
	  "name": "inbound-loop", "max_steps": 100,
	  "routers": [
	    {"name":"r9","asn":65009},
	    {"name":"r1","asn":65001},
	    {"name":"r2","asn":65002}
	  ],
	  "sessions": [
	    {"id":"s91","a":"r9","b":"r1","type":"ebgp"},
	    {"id":"s12","a":"r1","b":"r2","type":"ebgp",
	      "export_a":[
	        {"name":"force-neighbor-as","match":{"prefix":"P"},
	         "actions":[{"type":"prepend_as","prepend_as":[65002]}]}
	      ]}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}}
	  ]
	}`
	res, col, _ := runScn(t, raw)

	if peer, ok := bestPeer(res, "r1", "P"); !ok || peer != "r9" {
		t.Fatalf("r1 best = %q,%v; want r9", peer, ok)
	}
	if _, ok := bestPeer(res, "r2", "P"); ok {
		t.Fatalf("r2 must not install a route whose AS_PATH contains AS65002")
	}
	var found bool
	for _, tr := range col.Traces {
		if tr.Category == engine.TraceLoopRejected && tr.Router == "r2" && tr.Peer == "r1" {
			found = true
		}
		if tr.Category == engine.TraceCandidateUpdate && tr.Router == "r2" && tr.Prefix == "P" {
			t.Fatalf("looped route was installed at r2: %+v", tr)
		}
	}
	if !found {
		t.Fatalf("no loop_rejected trace at r2; r2 must drop path containing its own AS")
	}
}

// Outbound loop guard: in a plain eBGP ring the message that would carry
// the target AS is suppressed before it leaves the sending router, so the
// ring settles on shortest paths instead of looping forever.
func TestOutboundLoopGuardSettlesRing(t *testing.T) {
	raw := `{
	  "name": "ring-settles", "max_steps": 200,
	  "routers": [
	    {"name":"r0","asn":65000},
	    {"name":"a","asn":65001},
	    {"name":"b","asn":65002},
	    {"name":"c","asn":65003}
	  ],
	  "sessions": [
	    {"id":"s0a","a":"r0","b":"a","type":"ebgp"},
	    {"id":"sab","a":"a","b":"b","type":"ebgp"},
	    {"id":"sbc","a":"b","b":"c","type":"ebgp"},
	    {"id":"sca","a":"c","b":"a","type":"ebgp"}
	  ],
	  "events": [
	    {"seq":1,"router":"a","peer":"r0","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65000],"origin":"igp"}}
	  ]
	}`
	res, col, _ := runScn(t, raw)
	if !res.Converged {
		t.Fatalf("ring did not converge: %s", res.NonConvergentCode)
	}
	if peer, ok := bestPeer(res, "c", "P"); !ok || peer != "a" {
		t.Fatalf("c best = %q; want shortest path via a", peer)
	}
	// The long path b->...->a must reach a and be rejected inbound: its
	// AS_PATH carries AS65001.
	if countCategory(col, engine.TraceLoopRejected) < 1 {
		t.Fatalf("expected inbound loop rejection when the long path returns to a")
	}
}

// Withdrawal must remove only the withdrawing neighbor's candidate:
// withdrawing a non-best source leaves best untouched; withdrawing the
// best source falls back to the retained alternative.
func TestWithdrawalIsolationAndFallback(t *testing.T) {
	raw := `{
	  "name": "withdraw", "max_steps": 200,
	  "routers": [
	    {"name":"r9","asn":65009},
	    {"name":"r8","asn":65008},
	    {"name":"r1","asn":65001}
	  ],
	  "sessions": [
	    {"id":"s91","a":"r9","b":"r1","type":"ebgp"},
	    {"id":"s81","a":"r8","b":"r1","type":"ebgp"}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}},
	    {"seq":2,"router":"r1","peer":"r8","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65008],"origin":"igp"}},
	    {"seq":3,"router":"r1","peer":"r8","kind":"withdraw","prefix":"P"},
	    {"seq":4,"router":"r1","peer":"r9","kind":"withdraw","prefix":"P"}
	  ]
	}`
	res, col, _ := runScn(t, raw)

	// Event 2: r8 path length 1 vs r9 path length 1 -> router-id: r9
	// (ordinal 0) beats r8 (ordinal 1).
	if peer, _ := bestPeer(res, "r1", "P"); peer != "" {
		t.Fatalf("after both withdrawals r1 still has route via %q", peer)
	}
	ds := decisionsFor(col, "r1", "P")
	// Expect: select r9; then event 3 (r8 withdraw, not best) -> NO decision;
	// event 4 (r9 withdraw, only/best) -> lose route.
	if len(ds) != 2 {
		t.Fatalf("r1 decisions = %d (%+v), want 2 (non-best withdrawal must not reselect)", len(ds), ds)
	}
	if ds[0].ChosenPeer != "r9" {
		t.Fatalf("first decision chosen = %q, want r9", ds[0].ChosenPeer)
	}
	if ds[1].ChosenPeer != "" || ds[1].PreviousPeer != "r9" {
		t.Fatalf("second decision = %+v, want route lost from r9", ds[1])
	}
	removedAtR1 := 0
	for _, tr := range col.Traces {
		if tr.Category == engine.TraceCandidateRemoved && tr.Router == "r1" {
			removedAtR1++
		}
	}
	if removedAtR1 != 2 {
		t.Fatalf("r1 candidate_removed = %d, want exactly 2 (the propagated withdrawal at stub r8 is separate)",
			removedAtR1)
	}
	// The propagated withdrawal must actually arrive at the r8 stub.
	if n := countCategory(col, engine.TracePropagateWithdraw); n < 1 {
		t.Fatalf("propagate_withdraw traces = 0, want >= 1")
	}
}

// Withdrawing the best while a second source remains switches best to the
// retained source; the other source's candidate survives.
func TestWithdrawBestFallsBack(t *testing.T) {
	raw := `{
	  "name": "fallback", "max_steps": 200,
	  "routers": [
	    {"name":"r9","asn":65009},
	    {"name":"r8","asn":65008},
	    {"name":"r1","asn":65001}
	  ],
	  "sessions": [
	    {"id":"s91","a":"r9","b":"r1","type":"ebgp"},
	    {"id":"s81","a":"r8","b":"r1","type":"ebgp"}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}},
	    {"seq":2,"router":"r1","peer":"r8","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65008],"origin":"igp"}},
	    {"seq":3,"router":"r1","peer":"r9","kind":"withdraw","prefix":"P"}
	  ]
	}`
	res, col, _ := runScn(t, raw)
	if peer, ok := bestPeer(res, "r1", "P"); !ok || peer != "r8" {
		t.Fatalf("r1 best after withdrawing r9 = %q,%v; want fallback to r8", peer, ok)
	}
	var last engine.Decision
	for _, d := range decisionsFor(col, "r1", "P") {
		last = d
	}
	if last.PreviousPeer != "r9" || last.ChosenPeer != "r8" {
		t.Fatalf("fallback decision = %+v, want r9 -> r8", last)
	}
	// r8 is the sole remaining source after r9 leaves: the recorded reason
	// must describe selection among remaining candidates, not a phantom
	// comparison against the withdrawn route.
	if last.Reason != "only_candidate" {
		t.Fatalf("fallback reason = %q, want only_candidate", last.Reason)
	}
}

// Withdrawing the best while several sources remain: the replacement must
// be chosen by comparing the REMAINING candidates (shorter AS_PATH), and
// the recorded reason must be that comparison step.
func TestWithdrawBestReasonAmongRemaining(t *testing.T) {
	raw := `{
	  "name": "fallback-reason", "max_steps": 300,
	  "routers": [
	    {"name":"r7","asn":65007},
	    {"name":"r8","asn":65008},
	    {"name":"r9","asn":65009},
	    {"name":"r1","asn":65001}
	  ],
	  "sessions": [
	    {"id":"s71","a":"r7","b":"r1","type":"ebgp"},
	    {"id":"s81","a":"r8","b":"r1","type":"ebgp"},
	    {"id":"s91","a":"r9","b":"r1","type":"ebgp"}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}},
	    {"seq":2,"router":"r1","peer":"r8","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65008,77],"origin":"igp"}},
	    {"seq":3,"router":"r1","peer":"r7","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65007,78,79],"origin":"igp"}},
	    {"seq":4,"router":"r1","peer":"r9","kind":"withdraw","prefix":"P"}
	  ]
	}`
	res, col, _ := runScn(t, raw)
	if peer, ok := bestPeer(res, "r1", "P"); !ok || peer != "r8" {
		t.Fatalf("r1 best after r9 withdraw = %q,%v; want r8 (shorter of the two remaining)", peer, ok)
	}
	var last engine.Decision
	for _, d := range decisionsFor(col, "r1", "P") {
		if d.ChosenPeer == "r8" && d.PreviousPeer == "r9" {
			last = d
		}
	}
	if last.ChosenPeer == "" {
		t.Fatalf("no r9 -> r8 fallback decision; got %+v", decisionsFor(col, "r1", "P"))
	}
	if last.Reason != "as_path_length" {
		t.Fatalf("fallback reason = %q, want as_path_length (r8 len2 < r7 len3)", last.Reason)
	}
	if last.RunnerUpPeer != "r7" {
		t.Fatalf("runner-up = %q, want r7", last.RunnerUpPeer)
	}
}

// Import and export policies are independent: an import denial on one end
// must not affect what the other end exports, and an export denial must not
// remove the local candidate.
func TestImportExportIndependence(t *testing.T) {
	raw := `{
	  "name": "indep", "max_steps": 200,
	  "routers": [
	    {"name":"r9","asn":65009},
	    {"name":"r1","asn":65001},
	    {"name":"r2","asn":65002}
	  ],
	  "sessions": [
	    {"id":"s91","a":"r9","b":"r1","type":"ebgp"},
	    {"id":"s12","a":"r1","b":"r2","type":"ebgp",
	      "import_b":[{"name":"r2-refuses","deny":true,"match":{"prefix":"P"}}],
	      "export_a":[{"name":"r1-blocks-q","deny":true,"match":{"prefix":"Q"}}]}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}},
	    {"seq":2,"router":"r1","peer":"r9","kind":"update","prefix":"Q",
	     "attrs":{"as_path":[65009],"origin":"igp"}}
	  ]
	}`
	res, col, _ := runScn(t, raw)

	// r1 holds both prefixes regardless of r2's import policy.
	if peer, ok := bestPeer(res, "r1", "P"); !ok || peer != "r9" {
		t.Fatalf("r1 P = %q,%v; want r9", peer, ok)
	}
	if peer, ok := bestPeer(res, "r1", "Q"); !ok || peer != "r9" {
		t.Fatalf("r1 Q = %q,%v; want r9 (export denial must not remove local candidate)", peer, ok)
	}
	// r2 must not learn either: P by its own import denial, Q by r1's export.
	if _, ok := bestPeer(res, "r2", "P"); ok {
		t.Fatalf("r2 learned P despite import denial")
	}
	if _, ok := bestPeer(res, "r2", "Q"); ok {
		t.Fatalf("r2 learned Q despite export denial")
	}
	if countCategory(col, engine.TraceImportDenied) < 1 {
		t.Fatalf("no import_denied trace")
	}
	if countCategory(col, engine.TraceExportDenied) < 1 {
		t.Fatalf("no export_denied trace")
	}
	// P must have been exported once before being denied at import.
	sawProp := false
	for _, tr := range col.Traces {
		if tr.Category == engine.TracePropagateUpdate && tr.Router == "r1" &&
			tr.Peer == "r2" && tr.Prefix == "P" {
			sawProp = true
		}
	}
	if !sawProp {
		t.Fatalf("P never crossed r1->r2; export must run independently of r2 import")
	}
}

// A withdrawn-then-reannounced source must install cleanly again.
func TestReannounceAfterWithdraw(t *testing.T) {
	raw := `{
	  "name": "reannounce", "max_steps": 200,
	  "routers": [{"name":"r9","asn":65009},{"name":"r1","asn":65001}],
	  "sessions": [{"id":"s91","a":"r9","b":"r1","type":"ebgp"}],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}},
	    {"seq":2,"router":"r1","peer":"r9","kind":"withdraw","prefix":"P"},
	    {"seq":3,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009,65099],"med":30,"origin":"egp"}}
	  ]
	}`
	res, _, _ := runScn(t, raw)
	v, ok := lookupBest(res, "r1", "P")
	if !ok {
		t.Fatalf("r1 has no route after re-announcement")
	}
	if v.Peer != "r9" || len(v.Attrs.ASPath) != 2 || v.Attrs.Origin != model.OriginEGP || v.Attrs.MedOr() != 30 {
		t.Fatalf("re-announced route stale/wrong: %+v", v)
	}
}

func lookupBest(res *engine.Result, router, prefix string) (engine.BestView, bool) {
	for _, v := range res.Best[router] {
		if v.Prefix == prefix {
			return v, true
		}
	}
	return engine.BestView{}, false
}

// max_steps above the hard cap is rejected as a typed RESOURCE_EXHAUSTED
// error (distinguishable from INPUT and from compute failure).
func TestMaxStepsHardCap(t *testing.T) {
	raw := `{
	  "routers": [{"name":"r9","asn":65009},{"name":"r1","asn":65001}],
	  "sessions": [{"id":"s","a":"r9","b":"r1","type":"ebgp"}],
	  "events": [{"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	              "attrs":{"as_path":[65009],"origin":"igp"}}]
	}`
	sc, err := config.Parse([]byte(raw))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	_, err = engine.Run(sc, engine.Options{MaxSteps: engine.HardMaxSteps + 1}, nil)
	if err == nil {
		t.Fatalf("expected error for max_steps over hard cap")
	}
	me, ok := model.AsError(err)
	if !ok || me.Kind != model.KindResourceExhausted || me.Code != "MAX_STEPS_CAP" {
		t.Fatalf("err = %v, want RESOURCE_EXHAUSTED/MAX_STEPS_CAP", err)
	}
}

// An oscillating gadget stopped by a very small budget without the engine
// having yet observed a repeated full-state signature returns the
// BUDGET_EXCEEDED_NO_CYCLE code, distinct from proven oscillation.
func TestSmallBudgetNoCycleCode(t *testing.T) {
	sc, err := config.LoadFile("../fixtures/03_oscillation.json")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	// One external event + a handful of propagation messages cannot yet
	// complete a full gadget cycle.
	res, err := engine.Run(sc, engine.Options{MaxSteps: 4}, engine.NewCollector())
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if res.Converged {
		t.Fatalf("unexpected convergence under a 4-step budget")
	}
	if res.NonConvergentCode != model.NonConvBudgetNoCycle {
		t.Fatalf("code = %q, want %s", res.NonConvergentCode, model.NonConvBudgetNoCycle)
	}
	if res.Cycle != nil {
		t.Fatalf("cycle evidence must be nil until a signature repeats")
	}
}

// Export policy actions (set_med, prepend_as) are applied before the
// protocol prepends the local AS; the receiver sees the rewritten path.
// MED's selection semantics are unit-tested directly in model_test.
func TestExportActionsRewritePropagatedRoute(t *testing.T) {
	raw := `{
	  "name": "export-actions", "max_steps": 200,
	  "routers": [
	    {"name":"x","asn":65009},
	    {"name":"r1","asn":65001},
	    {"name":"r2","asn":65002}
	  ],
	  "sessions": [
	    {"id":"sx1","a":"x","b":"r1","type":"ebgp"},
	    {"id":"s12","a":"r1","b":"r2","type":"ebgp",
	      "export_a":[
	        {"name":"prepend-and-med10","match":{"prefix":"P"},
	         "actions":[
	           {"type":"prepend_as","prepend_as":[65009]},
	           {"type":"set_med","set_med":10}]}
	      ]}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"x","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"med":100,"origin":"igp"}}
	  ]
	}`
	res, col, _ := runScn(t, raw)
	v, ok := lookupBest(res, "r2", "P")
	if !ok {
		t.Fatalf("r2 never learned P")
	}
	// Policy prepends 65009, protocol prepends local 65001.
	wantPath := []uint32{65001, 65009, 65009}
	if len(v.Attrs.ASPath) != len(wantPath) {
		t.Fatalf("r2 path = %v, want %v", v.Attrs.ASPath, wantPath)
	}
	for i := range wantPath {
		if v.Attrs.ASPath[i] != wantPath[i] {
			t.Fatalf("r2 path = %v, want %v", v.Attrs.ASPath, wantPath)
		}
	}
	if v.Attrs.MedOr() != 10 {
		t.Fatalf("r2 med = %d, want policy-set 10", v.Attrs.MedOr())
	}
	// The message ON THE WIRE must not carry local_pref; the receiver then
	// re-defaults it inside its RIB. Assert on the propagation trace.
	var wireLPSeen bool
	for _, tr := range col.Traces {
		if tr.Category == engine.TracePropagateUpdate && tr.Router == "r1" &&
			tr.Peer == "r2" && tr.Prefix == "P" && tr.AttrsAfter != nil {
			wireLPSeen = true
			if tr.AttrsAfter.LocalPref != nil {
				t.Fatalf("local_pref leaked across eBGP on the wire: %v", *tr.AttrsAfter.LocalPref)
			}
		}
	}
	if !wireLPSeen {
		t.Fatalf("no r1->r2 propagation trace for P")
	}
}
