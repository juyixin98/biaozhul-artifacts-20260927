// Package difftest contains the differential property tests that compare the
// indexed kernel against an independent per-subscription reference matcher and
// against hand-written golden expectations. It lives in its own package so the
// oracle and the kernel cannot share implementation details.
package difftest

import (
	"bufio"
	"encoding/json"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

	"topicrouter/internal/kernel"
	"topicrouter/internal/testutil/reference"
	"topicrouter/internal/topic"
)

var jsonUnmarshal = json.Unmarshal

// findGolden walks up to the repo root (tests run from the package directory).
func findGolden(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 6; i++ {
		p := filepath.Join(dir, "testdata", "golden.jsonl")
		if _, err := os.Stat(p); err == nil {
			return p
		}
		dir = filepath.Dir(dir)
	}
	t.Fatal("testdata/golden.jsonl not found")
	return ""
}

type goldenCase struct {
	Case           string              `json:"case"`
	Subscriptions  map[string]string   `json:"subscriptions"`
	Expectations   map[string][]string `json:"expectations"`
	InvalidFilters []string            `json:"invalid_filters"`
}

func buildKernel(subs map[string]string, version int64) *kernel.Snapshot {
	b := kernel.NewBuilder(nil)
	ids := make([]string, 0, len(subs))
	for id := range subs {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		f := subs[id]
		if err := topic.ValidateFilter(f); err != nil {
			panic("invalid golden filter " + f + ": " + err.Error())
		}
		b.Insert(topic.SplitSubject(f), id)
	}
	return b.Build(version)
}

func TestGolden_HandWrittenExpectations(t *testing.T) {
	f, err := os.Open(findGolden(t))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	var n int
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		n++
		var gc goldenCase
		if err := jsonUnmarshal([]byte(line), &gc); err != nil {
			t.Fatalf("golden line %d: %v", n, err)
		}
		t.Run(gc.Case, func(t *testing.T) {
			for _, bad := range gc.InvalidFilters {
				err := topic.ValidateFilter(bad)
				if err == nil {
					t.Errorf("filter %q must be rejected by protocol", bad)
				} else if pe, ok := topic.AsProtocolError(err); !ok ||
					(pe.Class != topic.ErrWildcardEmbedding && pe.Class != topic.ErrWildcardPosition) {
					t.Errorf("filter %q rejected with %v, want wildcard category", bad, err)
				}
			}
			snap := buildKernel(gc.Subscriptions, 1)
			for topicText, want := range gc.Expectations {
				if topicText == "" {
					// Protocol rejects empty topics; the golden entry exists
					// to document the behavior, skip kernel evaluation.
					continue
				}
				if err := topic.ValidateTopic(topicText); err != nil {
					t.Fatalf("golden topic %q invalid: %v", topicText, err)
				}
				got := snap.Match(topic.SplitSubject(topicText)).Subscribers
				w := want
				if w == nil {
					w = []string{}
				}
				if !reflect.DeepEqual(got, w) {
					t.Errorf("topic %q:\n  kernel got %v\n  golden want %v",
						topicText, got, w)
				}
			}
		})
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	if n < 15 {
		t.Fatalf("expected >=15 golden cases, read %d", n)
	}
}

func TestGolden_ReferenceAgreesWithGolden(t *testing.T) {
	// The hand-written golden answer must also equal the independent oracle.
	// This guards both directions at once: a typo in the golden file cannot
	// silently agree with the kernel.
	f, _ := os.Open(findGolden(t))
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	n := 0
	for sc.Scan() {
		var gc goldenCase
		_ = jsonUnmarshal(sc.Bytes(), &gc)
		for topicText, want := range gc.Expectations {
			if topicText == "" {
				continue
			}
			got, err := reference.RouteAll(gc.Subscriptions, topicText)
			if err != nil {
				t.Errorf("[%s] topic %q: oracle error: %v", gc.Case, topicText, err)
				continue
			}
			if !equalSets(got, want) {
				t.Errorf("[%s] topic %q:\n  oracle got %v\n  golden want %v",
					gc.Case, topicText, got, want)
			}
		}
		n++
	}
}

// layerAlphabet for generated filters/topics: includes "" (empty), literals
// and both wildcards for filters.
var (
	topicLayers   = []string{"", "a", "b", "sport", "tennis", "x", "+"}
	literalLayers = []string{"", "a", "b", "sport", "tennis", "x"}
)

func genFilter(rng *rand.Rand) string {
	depth := 1 + rng.Intn(5)
	ls := make([]string, depth)
	for i := 0; i < depth; i++ {
		last := i == depth-1
		switch rng.Intn(10) {
		case 0, 1:
			ls[i] = "+"
		case 2:
			if last {
				ls[i] = "#"
			} else {
				ls[i] = literalLayers[rng.Intn(len(literalLayers))]
			}
		default:
			ls[i] = literalLayers[rng.Intn(len(literalLayers))]
		}
	}
	// A single empty layer serializes to the empty string, which is not a
	// representable filter; pick a non-empty literal for that case.
	if depth == 1 && ls[0] == "" {
		ls[0] = "a"
	}
	f := strings.Join(ls, "/")
	if err := topic.ValidateFilter(f); err != nil {
		// Generator invariant: every produced filter is protocol-valid.
		panic("generator produced invalid filter " + strconv.Quote(f) + ": " + err.Error())
	}
	return f
}

func genTopic(rng *rand.Rand) string {
	depth := 1 + rng.Intn(6)
	ls := make([]string, depth)
	for i := range ls {
		ls[i] = topicLayers[rng.Intn(len(topicLayers))]
	}
	tp := strings.Join(ls, "/")
	if tp == "" {
		return "a" // empty string is rejected at the protocol layer
	}
	return tp
}

func TestDifferential_RandomFiltersVsReference(t *testing.T) {
	rng := rand.New(rand.NewSource(20260928))
	for iter := 0; iter < 200; iter++ {
		nSubs := 1 + rng.Intn(40)
		subs := make(map[string]string, nSubs)
		for i := 0; i < nSubs; i++ {
			subs["s"+itoa(i)] = genFilter(rng)
		}
		snap := buildKernel(subs, 1)

		for q := 0; q < 30; q++ {
			tp := genTopic(rng)
			if err := topic.ValidateTopic(tp); err != nil {
				t.Fatalf("generated invalid topic %q: %v", tp, err)
			}
			got := snap.Match(topic.SplitSubject(tp)).Subscribers
			want, err := reference.RouteAll(subs, tp)
			if err != nil {
				t.Fatalf("oracle rejected its own fixture %q: %v", tp, err)
			}
			if !equalSets(got, want) {
				t.Fatalf("iter %d query %q:\n  kernel %v\n  oracle %v\n  subs=%v",
					iter, tp, got, want, subs)
			}
		}
	}
}

func TestDifferential_OverlappingWildcardsStress(t *testing.T) {
	// Adversarially overlapping patterns maximize conceptual path multiplicity.
	pats := []string{"#", "+/#", "+/+/#", "a/#", "a/+", "a/+/b/#", "a/+/+",
		"+/b", "+/b/#", "a/b/#", "/#", "/+", "a//+", "a//b/#", "//"}
	subs := make(map[string]string, len(pats))
	for i, p := range pats {
		subs["f"+itoa(i)] = p
	}
	snap := buildKernel(subs, 1)

	rng := rand.New(rand.NewSource(99))
	for i := 0; i < 5000; i++ {
		tp := genTopic(rng)
		if err := topic.ValidateTopic(tp); err != nil {
			continue
		}
		m := snap.Match(topic.SplitSubject(tp))
		want, _ := reference.RouteAll(subs, tp)
		if !reflect.DeepEqual(m.Subscribers, want) {
			t.Fatalf("topic %q:\n  kernel %v\n  oracle %v", tp, m.Subscribers, want)
		}
		if m.Stats.DedupHits != 0 {
			t.Fatalf("topic %q: dedup guard fired %d times", tp, m.Stats.DedupHits)
		}
	}
}

// TestIndexAccess_vs_FullScan measures the actual index access counts against
// the reference scan cost and asserts a large, structural gap rather than a
// constant-factor microbenchmark.
func TestIndexAccess_vs_FullScan(t *testing.T) {
	const n = 5000
	subs := make(map[string]string, n)
	b := kernel.NewBuilder(nil)
	for i := 0; i < n; i++ {
		f := "root/branch/" + pad(i%500) + "/device/" + pad(i) + "/events/+"
		id := "s" + pad(i)
		subs[id] = f
		b.Insert(topic.SplitSubject(f), id)
	}
	b.Insert(topic.SplitSubject("#"), "all")
	subs["all"] = "#"
	snap := b.Build(1)

	tp := "root/branch/000123/device/004321/events/temp"
	m := snap.Match(topic.SplitSubject(tp))

	// Reference cost: every one of the 5001 filters examined.
	refMatched, err := reference.RouteAll(subs, tp)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(m.Subscribers, refMatched) {
		t.Fatalf("kernel %v != reference %v", m.Subscribers, refMatched)
	}
	scanCost := len(subs)
	if m.Stats.NodeVisits >= scanCost/100 {
		t.Errorf("index NodeVisits=%d is not <1%% of scan cost %d", m.Stats.NodeVisits, scanCost)
	}
	t.Logf("index node_visits=%d edge_lookups=%d terminals=%d vs full-scan filters=%d (%.3f%%)",
		m.Stats.NodeVisits, m.Stats.EdgeLookups, m.Stats.TerminalsCollected,
		scanCost, 100*float64(m.Stats.NodeVisits)/float64(scanCost))
}

func equalSets(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	x := append([]string(nil), a...)
	y := append([]string(nil), b...)
	sort.Strings(x)
	sort.Strings(y)
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}

func pad(i int) string {
	s := itoa(i)
	for len(s) < 6 {
		s = "0" + s
	}
	return s
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
