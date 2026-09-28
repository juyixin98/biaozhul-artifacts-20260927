package cidrsvc_test

import (
	"encoding/json"
	"os"
	"testing"

	"cidrsvc/internal/netmodel"
)

// FixtureDTO mirrors testdata/fixtures/golden_cases.json.
type fixtureFile struct {
	BoundaryPolicy string `json:"boundary_policy"`
	Cases          []struct {
		Name    string `json:"name"`
		Request struct {
			Family  string   `json:"family"`
			Allow   []string `json:"allow"`
			Exclude []string `json:"exclude"`
			Strict  bool     `json:"strict"`
		} `json:"request"`
		ExpectedPrefixes     []string `json:"expected_prefixes"`
		ExpectedAddressCount string   `json:"expected_address_count"`
	} `json:"cases"`
	ErrorCases []struct {
		Name    string `json:"name"`
		Request struct {
			Family  string   `json:"family"`
			Allow   []string `json:"allow"`
			Exclude []string `json:"exclude"`
			Strict  bool     `json:"strict"`
		} `json:"request"`
		ExpectedErrorCode string `json:"expected_error_code"`
	} `json:"error_cases"`
}

func loadFixtures(t *testing.T) fixtureFile {
	t.Helper()
	raw, err := os.ReadFile("testdata/fixtures/golden_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var fx fixtureFile
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	return fx
}

// TestGoldenFixtures asserts the engine reproduces the hand-derived golden
// answers exactly (prefix strings, address count), and independently verifies
// each answer's structural correctness.
func TestGoldenFixtures(t *testing.T) {
	fx := loadFixtures(t)
	if len(fx.Cases) < 10 {
		t.Fatalf("fixture file looks truncated: %d success cases", len(fx.Cases))
	}
	for _, c := range fx.Cases {
		t.Run(c.Name, func(t *testing.T) {
			res, err := netmodel.Compute(netmodel.ComputeRequest{
				Family: c.Request.Family, Allow: c.Request.Allow,
				Exclude: c.Request.Exclude, Strict: c.Request.Strict,
			})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if len(res.Prefixes) != len(c.ExpectedPrefixes) {
				t.Fatalf("prefix count: got %d want %d\ngot=%v\nwant=%v",
					len(res.Prefixes), len(c.ExpectedPrefixes),
					res.Prefixes, c.ExpectedPrefixes)
			}
			for i := range c.ExpectedPrefixes {
				if res.Prefixes[i] != c.ExpectedPrefixes[i] {
					t.Fatalf("index %d got=%s want=%s\nfull=%v",
						i, res.Prefixes[i], c.ExpectedPrefixes[i], res.Prefixes)
				}
			}
			if res.Proof.CoverAddressCount != c.ExpectedAddressCount {
				t.Fatalf("address count got=%s want=%s",
					res.Proof.CoverAddressCount, c.ExpectedAddressCount)
			}
			if !res.Proof.Equivalent {
				t.Fatal("golden answer not exactly equivalent")
			}
			if len(res.Proof.SiblingMerges) != 0 {
				t.Fatalf("golden answer has mergeable siblings: %v", res.Proof.SiblingMerges)
			}
		})
	}
}

// TestGoldenErrorFixtures asserts the exact error code for each documented
// failure category.
func TestGoldenErrorFixtures(t *testing.T) {
	fx := loadFixtures(t)
	for _, c := range fx.ErrorCases {
		t.Run(c.Name, func(t *testing.T) {
			_, err := netmodel.Compute(netmodel.ComputeRequest{
				Family: c.Request.Family, Allow: c.Request.Allow,
				Exclude: c.Request.Exclude, Strict: c.Request.Strict,
			})
			if err == nil {
				t.Fatal("expected error, got success")
			}
			pe, ok := err.(*netmodel.PrefixError)
			if !ok {
				t.Fatalf("error type %T is not *PrefixError: %v", err, err)
			}
			if pe.Kind != c.ExpectedErrorCode {
				t.Fatalf("error code got=%s want=%s", pe.Kind, c.ExpectedErrorCode)
			}
		})
	}
}

// TestBoundaryPolicyDocumented guards the fixture header so the inclusive
// network+broadcast policy cannot be silently changed without updating docs.
func TestBoundaryPolicyDocumented(t *testing.T) {
	fx := loadFixtures(t)
	if fx.BoundaryPolicy == "" {
		t.Fatal("fixture boundary_policy must be documented")
	}
}
