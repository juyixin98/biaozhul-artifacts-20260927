package engine

import (
	"os"
	"path/filepath"
	"testing"

	"igmpv2timer/internal/config"
	"igmpv2timer/internal/model"
	"igmpv2timer/internal/store"
)

func minimalScript() *Script {
	return &Script{
		Name: "unit", Iface: "eth0", Group: "239.9.9.9", UntilMs: 1000,
		Timing: config.Timing{
			QueryInterval: 1_000_000, QueryResponseInterval: 200,
			GroupMembershipInterval: 300,
			LastMemberQueryInterval: 100, LastMemberQueryCount: 2,
		},
		Members: []MemberSpec{{Name: "a", Addr: "192.0.2.1", GeneralDelayMs: 10}},
		Events: []ScriptEvent{
			{At: 10, Kind: "report", Member: "a"},
			{At: 20, Kind: "leave", Member: "a"},
			{At: 400, Kind: "checkpoint"},
		},
	}
}

func TestScriptValidation(t *testing.T) {
	cases := []func(s *Script){
		func(s *Script) { s.Name = "" },
		func(s *Script) { s.Iface = "" },
		func(s *Script) { s.Group = "" },
		func(s *Script) { s.UntilMs = 0 },
		func(s *Script) { s.Events = append(s.Events, ScriptEvent{Kind: "bogus"}) },
		func(s *Script) { s.Members = append(s.Members, MemberSpec{Name: "a", Addr: "1.1.1.1"}) },
		func(s *Script) { s.Assertions = append(s.Assertions, Assertion{Check: "weird"}) },
	}
	for i, mutate := range cases {
		s := minimalScript()
		mutate(s)
		if err := s.validate(); err == nil {
			t.Errorf("case %d: expected validation error", i)
		}
	}
}

func TestUnknownInterfaceRejected(t *testing.T) {
	s := minimalScript()
	s.Iface = "no-such"
	_, err := NewRunner(config.Default(), s, nil)
	if err == nil {
		t.Fatal("unknown scenario interface must fail")
	}
}

func TestSimpleJoinLeaveTimeout(t *testing.T) {
	cfg := config.Default()
	st, _ := store.Open(":memory:")
	defer st.Close()
	r, err := NewRunner(cfg, minimalScript(), st)
	if err != nil {
		t.Fatal(err)
	}
	rep, err := r.Run()
	if err != nil {
		t.Fatal(err)
	}
	// join at 10 creates; leave at 20 is the last member -> the FIRST GSQ is
	// sent immediately at 20 (RFC 3376), the second at 120, and deletion is
	// confirmed at leave + LMQC*LMQI = 220 if no report cancels it.
	var timeout *model.Diag
	for i := range rep.Diags {
		if rep.Diags[i].Verdict == model.VTimeout {
			timeout = &rep.Diags[i]
		}
	}
	if timeout == nil {
		t.Fatalf("expected a timeout diag: %+v", rep.Diags)
	}
	if timeout.Reason != model.ReasonLastMemberConfirmed {
		t.Errorf("timeout reason=%s", timeout.Reason)
	}
	if int64(timeout.At) != 220 {
		t.Errorf("last-member confirmation at %d, want 220 (leave 20 + LMQC*LMQI)", timeout.At)
	}
	// the two GSQs must be emitted at 20 and 120
	var gsqAt []int64
	for _, p := range rep.Emitted {
		if p.Packet == model.PktQueryGroup {
			gsqAt = append(gsqAt, int64(p.At))
		}
	}
	if len(gsqAt) != 2 || gsqAt[0] != 20 || gsqAt[1] != 120 {
		t.Errorf("GSQ times=%v, want [20 120]", gsqAt)
	}
}

func TestLoadScriptFromFile(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "s.json")
	body := `{"name":"f","iface":"eth0","group":"239.1.1.1","until_ms":500,
"members":[{"name":"a","addr":"192.0.2.1"}],"events":[]}`
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	s, err := LoadScript(p)
	if err != nil {
		t.Fatal(err)
	}
	if s.Name != "f" || s.Until() != 500 {
		t.Errorf("loaded script wrong: %+v", s)
	}
}
