package model_test

import (
	"testing"

	"pvsim/model"
)

func lp(v int) *int  { return &v }
func med(v int) *int { return &v }

func TestCompareOrder(t *testing.T) {
	// Each case documents one step of the selection order: the pair is
	// constructed so earlier steps are tied and exactly one step decides.
	cases := []struct {
		name   string
		a, b   model.Attrs
		ai, bi model.CompareInput
		want   int // -1: a better, +1: b better
		reason model.Reason
	}{
		{
			name: "1-local-pref-higher-wins",
			a:    model.Attrs{LocalPref: lp(150), ASPath: []uint32{2}, Med: med(9), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{2}, Med: med(9), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ibgp"},
			bi:   model.CompareInput{SessionType: "ibgp"},
			want: -1, reason: model.ReasonLocalPref,
		},
		{
			name: "2-shorter-as-path-wins-despite-worse-med",
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{7}, Med: med(999), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{8, 9}, Med: med(1), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ebgp"},
			bi:   model.CompareInput{SessionType: "ebgp"},
			want: -1, reason: model.ReasonASPath,
		},
		{
			name: "3-med-compared-only-same-neighbor-AS",
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{65100, 10}, Med: med(50), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{65100, 10}, Med: med(20), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ebgp"},
			bi:   model.CompareInput{SessionType: "ebgp"},
			want: 1, reason: model.ReasonMED, // b's lower MED wins
		},
		{
			name: "3b-med-ignored-when-neighbor-AS-differs-origin-decides",
			// Same path length; first AS differs so MED step is skipped;
			// a is IGP and beats b's incomplete.
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{65100, 10}, Med: med(999), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{65200, 10}, Med: med(1), Origin: model.OriginIncomplete},
			ai:   model.CompareInput{SessionType: "ebgp"},
			bi:   model.CompareInput{SessionType: "ebgp"},
			want: -1, reason: model.ReasonOrigin,
		},
		{
			name: "4-origin-igp-beats-egp",
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginEGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ebgp"},
			bi:   model.CompareInput{SessionType: "ebgp"},
			want: 1, reason: model.ReasonOrigin,
		},
		{
			name: "5-ebgp-beats-ibgp",
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ebgp"},
			bi:   model.CompareInput{SessionType: "ibgp"},
			want: -1, reason: model.ReasonEbgpOverIbgp,
		},
		{
			name: "6-igp-cost-lower-wins",
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ibgp", IGPCost: 30},
			bi:   model.CompareInput{SessionType: "ibgp", IGPCost: 10},
			want: 1, reason: model.ReasonIGPCost,
		},
		{
			name: "7-router-id-ordinal-lower-wins",
			a:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			b:    model.Attrs{LocalPref: lp(100), ASPath: []uint32{5}, Med: med(10), Origin: model.OriginIGP},
			ai:   model.CompareInput{SessionType: "ebgp", PeerOrdinal: 3},
			bi:   model.CompareInput{SessionType: "ebgp", PeerOrdinal: 2},
			want: 1, reason: model.ReasonRouterID,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := model.Compare(tc.a, tc.b, tc.ai, tc.bi)
			if got != tc.want || reason != tc.reason {
				t.Fatalf("Compare = (%d,%s), want (%d,%s)", got, reason, tc.want, tc.reason)
			}
			// Comparator must be anti-symmetric.
			back, _ := model.Compare(tc.b, tc.a, tc.bi, tc.ai)
			if back != -got {
				t.Fatalf("Compare not anti-symmetric: forward=%d backward=%d", got, back)
			}
		})
	}
}

func TestContainsAS(t *testing.T) {
	a := model.Attrs{ASPath: []uint32{65003, 65001, 65000}}
	for _, asn := range []uint32{65003, 65001, 65000} {
		if !a.ContainsAS(asn) {
			t.Errorf("ContainsAS(%d) = false, want true", asn)
		}
	}
	if a.ContainsAS(65099) {
		t.Errorf("ContainsAS(65099) = true, want false")
	}
}

func TestParseOrigin(t *testing.T) {
	if _, ok := model.ParseOrigin("bogus"); ok {
		t.Fatal("ParseOrigin(bogus) unexpectedly ok")
	}
	o, ok := model.ParseOrigin("egp")
	if !ok || o != model.OriginEGP {
		t.Fatalf("ParseOrigin(egp) = %v,%v", o, ok)
	}
}
