package oracle_test

import (
	"testing"

	"pvsim/config"
	"pvsim/internal/oracle"
)

func TestOracleConvergesSimpleChain(t *testing.T) {
	sc, err := config.Parse([]byte(`{
	  "routers": [
	    {"name":"r9","asn":65009},
	    {"name":"r1","asn":65001},
	    {"name":"r2","asn":65002}
	  ],
	  "sessions": [
	    {"id":"s91","a":"r9","b":"r1","type":"ebgp"},
	    {"id":"s12","a":"r1","b":"r2","type":"ebgp"}
	  ],
	  "events": [
	    {"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65009],"origin":"igp"}}
	  ]
	}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	oc := oracle.Run(sc, 100)
	if !oc.Converged {
		t.Fatalf("oracle did not converge simple chain (cycle %d..%d)", oc.CycleFrom, oc.CycleTo)
	}
	if e := oc.Best["r2"]["P"]; e.Peer != "r1" || len(e.ASPath) != 2 {
		t.Fatalf("r2 best = %+v, want via r1 path len 2", e)
	}
	if e := oc.Best["r1"]["P"]; e.Peer != "r9" {
		t.Fatalf("r1 best = %+v, want r9", e)
	}
}

func TestOracleDetectsBadGadget(t *testing.T) {
	sc, err := config.LoadFile("../../fixtures/03_oscillation.json")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	oc := oracle.Run(sc, 500)
	if oc.Converged {
		t.Fatalf("oracle unexpectedly converged an oscillating bad gadget")
	}
	if oc.CycleTo <= oc.CycleFrom || oc.CycleFrom == 0 {
		t.Fatalf("nonsensical cycle indices from=%d to=%d", oc.CycleFrom, oc.CycleTo)
	}
}

// The independent solver must honor an import local_pref policy just like
// the documented order says (higher local_pref wins over shorter path).
func TestOracleImportLocalPrefReversesPathLength(t *testing.T) {
	sc, err := config.Parse([]byte(`{
	  "routers": [
	    {"name":"s1","asn":65100},
	    {"name":"s2","asn":65200},
	    {"name":"r","asn":65000}
	  ],
	  "sessions": [
	    {"id":"x","a":"s1","b":"r","type":"ebgp",
	      "import_b":[{"name":"boost-s1","match":{"from_router":"s1"},
	        "actions":[{"type":"set_local_pref","set_local_pref":200}]}]},
	    {"id":"y","a":"s2","b":"r","type":"ebgp"}
	  ],
	  "events": [
	    {"seq":1,"router":"r","peer":"s1","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65100,1,2],"origin":"igp"}},
	    {"seq":2,"router":"r","peer":"s2","kind":"update","prefix":"P",
	     "attrs":{"as_path":[65200],"origin":"igp"}}
	  ]
	}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	oc := oracle.Run(sc, 100)
	if !oc.Converged {
		t.Fatalf("oracle failed to converge")
	}
	if e := oc.Best["r"]["P"]; e.Peer != "s1" || e.LocalPref != 200 {
		t.Fatalf("r best = %+v, want s1 with local_pref 200", e)
	}
}
