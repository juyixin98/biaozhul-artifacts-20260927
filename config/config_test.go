package config_test

import (
	"strings"
	"testing"

	"pvsim/config"
	"pvsim/model"
)

func mustParse(t *testing.T, raw string) *config.Scenario {
	t.Helper()
	sc, err := config.Parse([]byte(raw))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	return sc
}

func expectErr(t *testing.T, raw string, wantKind model.Kind, codeFragment string) {
	t.Helper()
	_, err := config.Parse([]byte(raw))
	if err == nil {
		t.Fatalf("Parse unexpectedly succeeded; want %s/%s", wantKind, codeFragment)
	}
	me, ok := model.AsError(err)
	if !ok {
		t.Fatalf("error %v is not a typed model.Error", err)
	}
	if me.Kind != wantKind {
		t.Fatalf("kind = %s, want %s (err=%v)", me.Kind, wantKind, err)
	}
	if codeFragment != "" && !strings.Contains(me.Code, codeFragment) {
		t.Fatalf("code = %s, want fragment %q (err=%v)", me.Code, codeFragment, err)
	}
}

const validBase = `{
  "name": "t", "max_steps": 100,
  "routers": [{"name":"r1","asn":65001},{"name":"r2","asn":65002}],
  "sessions": [{"id":"s1","a":"r1","b":"r2","type":"ebgp"}],
  "events": [{"seq":1,"router":"r1","peer":"r2","kind":"update","prefix":"P",
              "attrs":{"as_path":[65002],"origin":"igp"}}]
}`

func TestParseHappyPath(t *testing.T) {
	sc := mustParse(t, validBase)
	if len(sc.Routers) != 2 || len(sc.Sessions) != 1 || len(sc.Events) != 1 {
		t.Fatalf("unexpected parsed scenario: %+v", sc)
	}
	if _, cost, ok := sc.SessionBetween("r1", "r2"); !ok || cost != 0 {
		t.Fatalf("SessionBetween r1->r2 = (%d,%v)", cost, ok)
	}
}

func TestSyntaxError(t *testing.T) {
	expectErr(t, `{not json`, model.KindInput, "PAYLOAD_SYNTAX")
}

func TestUnknownFieldRejected(t *testing.T) {
	expectErr(t, `{
	  "name":"t","bogus":1,
	  "routers": [{"name":"r1","asn":65001}],
	  "sessions": [],
	  "events": []
	}`, model.KindInput, "PAYLOAD_SYNTAX")
}

func TestUnknownRouterReference(t *testing.T) {
	bad := strings.Replace(validBase, `"a":"r1"`, `"a":"rx"`, 1)
	expectErr(t, bad, model.KindInput, "UNREFERENCED_ENTITY")
}

func TestIbgpRequiresSameASN(t *testing.T) {
	bad := strings.Replace(validBase, `"type":"ebgp"`, `"type":"ibgp"`, 1)
	expectErr(t, bad, model.KindInput, "INVALID_CONFIG")
}

func TestEbgpRequiresDifferentASN(t *testing.T) {
	bad := strings.Replace(validBase, `"asn":65002`, `"asn":65001`, 1)
	expectErr(t, bad, model.KindInput, "INVALID_CONFIG")
}

func TestLocalPrefOnEbgpEventRejected(t *testing.T) {
	bad := strings.Replace(validBase, `"as_path":[65002]`,
		`"as_path":[65002],"local_pref":500`, 1)
	expectErr(t, bad, model.KindInput, "INVALID_CONFIG")
}

func TestEventLoopInFixtureRejected(t *testing.T) {
	// AS_PATH of an event delivered to r1 must not already carry AS65001.
	bad := strings.Replace(validBase, `"as_path":[65002]`, `"as_path":[65001,65002]`, 1)
	expectErr(t, bad, model.KindInput, "LOOP_IN_EVENT")
}

func TestEventBetweenNonAdjacentRouters(t *testing.T) {
	raw := `{
	  "routers": [{"name":"r1","asn":65001},{"name":"r2","asn":65002},{"name":"r3","asn":65003}],
	  "sessions": [{"id":"s1","a":"r1","b":"r2","type":"ebgp"}],
	  "events": [{"seq":1,"router":"r1","peer":"r3","kind":"withdraw","prefix":"P"}]
	}`
	expectErr(t, raw, model.KindInput, "UNREFERENCED_ENTITY")
}

func TestDuplicateEventSeq(t *testing.T) {
	raw := strings.Replace(validBase, `"events": [`, `"events": [
	  {"seq":1,"router":"r2","peer":"r1","kind":"withdraw","prefix":"Q"},`, 1)
	expectErr(t, raw, model.KindInput, "INVALID_CONFIG")
}

func TestDenyRuleWithActionsRejected(t *testing.T) {
	raw := strings.Replace(validBase,
		`"sessions": [{"id":"s1","a":"r1","b":"r2","type":"ebgp"}]`,
		`"sessions": [{"id":"s1","a":"r1","b":"r2","type":"ebgp",
		  "import_a":[{"name":"x","deny":true,
		    "actions":[{"type":"set_med","set_med":1}]}]}]`, 1)
	expectErr(t, raw, model.KindInput, "INVALID_CONFIG")
}

func TestPolicyFirstMatch(t *testing.T) {
	sc := mustParse(t, `{
	  "routers": [{"name":"r1","asn":65001},{"name":"r2","asn":65002}],
	  "sessions": [{"id":"s1","a":"r1","b":"r2","type":"ebgp",
	    "import_a":[
	      {"name":"deny-prefix","deny":true,"match":{"prefix":"DENIED"}},
	      {"name":"boost","actions":[{"type":"set_local_pref","set_local_pref":300}]},
	      {"name":"unreached","deny":true}
	    ]}],
	  "events": [{"seq":1,"router":"r1","peer":"r2","kind":"update","prefix":"P",
	              "attrs":{"as_path":[65002],"origin":"igp"}}]
	}`)
	// Deny match.
	got := config.EvalPolicy(sc.Sessions[0].ImportA,
		config.PolicyInput{Prefix: "DENIED", Attrs: model.Attrs{ASPath: []uint32{65002}}, FromRouter: "r2"})
	if got.Permitted || got.RuleName != "deny-prefix" {
		t.Fatalf("deny rule: %+v", got)
	}
	// First matching permit wins and later deny must not be reached.
	got = config.EvalPolicy(sc.Sessions[0].ImportA,
		config.PolicyInput{Prefix: "P", Attrs: model.Attrs{ASPath: []uint32{65002}}, FromRouter: "r2"})
	if !got.Permitted || got.RuleName != "boost" || got.Attrs.LocalPrefOr(0) != 300 {
		t.Fatalf("permit rule: %+v", got)
	}
}
