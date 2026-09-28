package kernel

import (
	"reflect"
	"sort"
	"testing"

	"topicrouter/internal/topic"
)

// insert is a test helper mirroring validated insertion.
func insert(b *Builder, filter, sub string) {
	if err := topic.ValidateFilter(filter); err != nil {
		panic("invalid filter in test: " + filter)
	}
	b.Insert(topic.SplitSubject(filter), sub)
}

func build(t *testing.T, version int64, pairs ...string) *Snapshot {
	t.Helper()
	if len(pairs)%2 != 0 {
		t.Fatalf("pairs must be filter,sub alternating")
	}
	b := NewBuilder(nil)
	for i := 0; i < len(pairs); i += 2 {
		insert(b, pairs[i], pairs[i+1])
	}
	return b.Build(version)
}

func match(s *Snapshot, topicText string) MatchResult {
	if err := topic.ValidateTopic(topicText); err != nil {
		panic("invalid topic in test: " + topicText)
	}
	return s.Match(topic.SplitSubject(topicText))
}

func TestMatch_GoldenTable(t *testing.T) {
	// Overlapping wildcards: every topic is covered by several filters at once
	// ("#", "sport/#", "sport/+", exact), which is where naive multi-index
	// designs double-deliver.
	s := build(t, 1,
		"#", "s-hash",
		"sport/#", "s-sport-hash",
		"sport/+", "s-sport-plus",
		"sport/tennis", "s-tennis",
		"sport/tennis/+", "s-tennis-player",
		"sport/tennis/#", "s-tennis-hash",
		"sport/+/results", "s-sport-any-results",
		"sport/tennis/results", "s-tennis-results",
		"a/+/c/+", "s-cross",
		"/#", "s-leading-empty-hash",
		"/a/+", "s-leading-empty-a-plus",
		"a//+", "s-double-slash-plus",
		"x/+", "s-x-plus",
		"x/#", "s-x-hash",
	)

	type tc struct {
		topic string
		want  []string
	}
	cases := []tc{
		// "#" matches zero remaining layers, so "sport/#" matches "sport".
		{"sport", []string{"s-hash", "s-sport-hash"}},
		{"/", []string{"s-hash", "s-leading-empty-hash"}},
		{"sport/tennis", []string{
			"s-hash", "s-sport-hash", "s-sport-plus", "s-tennis", "s-tennis-hash",
		}},
		{"sport/tennis/player1", []string{
			"s-hash", "s-sport-hash", "s-tennis-hash", "s-tennis-player",
		}},
		{"sport/tennis/player1/rank", []string{
			"s-hash", "s-sport-hash", "s-tennis-hash",
		}},
		{"sport/golf/results", []string{
			"s-hash", "s-sport-any-results", "s-sport-hash",
		}},
		{"sport/tennis/results", []string{
			"s-hash", "s-sport-any-results", "s-sport-hash", "s-tennis-hash",
			"s-tennis-player", "s-tennis-results",
		}},
		// "+" matches an empty layer.
		{"a//c/", []string{"s-cross", "s-hash"}},
		{"a/b/c/d", []string{"s-cross", "s-hash"}},
		// Leading empty layer: "/#" filter attaches to node "" and matches
		// anything with a leading empty layer.
		{"/a/b", []string{"s-hash", "s-leading-empty-a-plus", "s-leading-empty-hash"}},
		{"/a/x", []string{"s-hash", "s-leading-empty-a-plus", "s-leading-empty-hash"}},
		// Empty layer after "a/": "a//+".
		{"a//x", []string{"s-double-slash-plus", "s-hash"}},
		// x/+ and x/# overlap on exactly one layer.
		{"x/y", []string{"s-hash", "s-x-hash", "s-x-plus"}},
		{"x/y/z", []string{"s-hash", "s-x-hash"}},
		// Non-matching literal "+" chars in topic are just literals: no
		// subscription here matches topic layer "+" literally.
		{"x/+", []string{"s-hash", "s-x-hash", "s-x-plus"}},
	}
	for _, c := range cases {
		got := match(s, c.topic)
		if !reflect.DeepEqual(got.Subscribers, c.want) {
			t.Errorf("topic %q:\n  got  %v\n  want %v", c.topic, got.Subscribers, c.want)
		}
		if got.Stats.DedupHits != 0 {
			t.Errorf("topic %q: DedupHits=%d, structural duplicate path?", c.topic, got.Stats.DedupHits)
		}
	}
}

func TestMatch_EmptyLayerExhaustive(t *testing.T) {
	s := build(t, 1,
		"#", "root",
		"+", "plus1",
		"+/+", "plus2",
			"+/#", "plus1hash",
		"//", "twoempties",
	)
	cases := map[string][]string{
		// "/" layers ["",""] (2 layers): matches "+/+", "+/#" and "#". The
		// exact filter "//" has 3 layers and must NOT match.
		"/":   {"plus1hash", "plus2", "root"},
		"//":  {"plus1hash", "root", "twoempties"},
		"/a":  {"plus1hash", "plus2", "root"},
		"a/":  {"plus1hash", "plus2", "root"},
		"a/b": {"plus1hash", "plus2", "root"},
		"a":   {"plus1", "plus1hash", "root"},
		"///": {"plus1hash", "root"},
	}
	for topicText, want := range cases {
		got := match(s, topicText)
		if !reflect.DeepEqual(got.Subscribers, want) {
			t.Errorf("topic %q got %v, want %v", topicText, got.Subscribers, want)
		}
	}
}

func TestMatch_DedupAcrossMultiplePaths(t *testing.T) {
	// One subscriber on the single structural identity: even if it were
	// inserted twice (idempotent insert), it must be delivered once.
	b := NewBuilder(nil)
	insert(b, "a/#", "same")
	insert(b, "a/#", "same")
	insert(b, "a/+", "same") // same id, different conceptual filter
	s := b.Build(1)
	r := match(s, "a/x")
	want := []string{"same"}
	if !reflect.DeepEqual(r.Subscribers, want) {
		t.Errorf("got %v, want single delivery %v (stats=%+v)", r.Subscribers, want, r.Stats)
	}
}

func TestSnapshot_VersionIsolationAndDelete(t *testing.T) {
	v1 := build(t, 1, "a/+", "s1", "b/#", "s2")
	r1 := match(v1, "a/x")
	if !reflect.DeepEqual(r1.Subscribers, []string{"s1"}) {
		t.Fatalf("v1 match = %v", r1.Subscribers)
	}

	// v2 adds and deletes on a COW child; v1 must be untouched.
	b := NewBuilder(v1)
	b.Delete(topic.SplitSubject("a/+"), "s1")
	insert(b, "a/y", "s3")
	v2 := b.Build(2)

	if got := match(v1, "a/x").Subscribers; !reflect.DeepEqual(got, []string{"s1"}) {
		t.Errorf("historical v1 changed after v2 edit: %v", got)
	}
	if got := match(v2, "a/x").Subscribers; len(got) != 0 {
		t.Errorf("v2 still routes deleted s1: %v", got)
	}
	if got := match(v2, "a/y").Subscribers; !reflect.DeepEqual(got, []string{"s3"}) {
		t.Errorf("v2 new route = %v", got)
	}
	if got := match(v2, "b/anything").Subscribers; !reflect.DeepEqual(got, []string{"s2"}) {
		t.Errorf("shared subtree lost across COW versions: %v", got)
	}
}

func TestDelete_PrunesBranches(t *testing.T) {
	b := NewBuilder(nil)
	insert(b, "a/b/c", "s1")
	insert(b, "a/b/d", "s2")
	s0 := b.Build(1)

	b2 := NewBuilder(s0)
	b2.Delete(topic.SplitSubject("a/b/c"), "s1")
	s1 := b2.Build(2)
	if got := match(s1, "a/b/c").Subscribers; len(got) != 0 {
		t.Fatalf("deleted sub still matched: %v", got)
	}
	if got := match(s1, "a/b/d").Subscribers; !reflect.DeepEqual(got, []string{"s2"}) {
		t.Fatalf("sibling deleted by prune: %v", got)
	}

	b3 := NewBuilder(s1)
	b3.Delete(topic.SplitSubject("a/b/d"), "s2")
	s2 := b3.Build(3)
	if got := match(s2, "a/b/d").Subscribers; len(got) != 0 {
		t.Fatalf("s2 still present: %v", got)
	}
	// Only the empty root should remain: matching anything yields nothing.
	if got := match(s2, "a").Subscribers; len(got) != 0 {
		t.Fatalf("expected fully pruned trie, got %v", got)
	}
}

func TestMatch_IndexAccessIsBoundedNotFullScan(t *testing.T) {
	// 2000 subscriptions sharing a long common prefix; only a handful can
	// possibly match a target topic. A full scan inspects all 2000 filters;
	// the trie must inspect a small multiple of topic depth.
	const n = 2000
	b := NewBuilder(nil)
	for i := 0; i < n; i++ {
		insert(b, "tenant/acme/events/2026/september/day28/device/"+padID(i)+"/stream/+",
			"s"+itoaK(i))
	}
	// Plus broad wildcards elsewhere — they must not drag the walk through
	// the 2000-device subtree.
	insert(b, "#", "root")
	insert(b, "tenant/#", "tenant-hash")
	s := b.Build(1)

	r := match(s, "tenant/acme/events/2026/september/day28/device/000424/stream/temp")
	if len(r.Subscribers) != 3 {
		t.Fatalf("expected exactly 3 matches (exact + 2 hash), got %d: %v", len(r.Subscribers), r.Subscribers)
	}
	// Depth is ~9 layers; at each layer at most 3 edges are probed, so node
	// visits should be O(depth * branches), nowhere near n.
	if r.Stats.NodeVisits > 100 {
		t.Errorf("NodeVisits=%d looks like a scan of %d subscriptions", r.Stats.NodeVisits, n)
	}
	if r.Stats.EdgeLookups > 200 {
		t.Errorf("EdgeLookups=%d too high for a depth-bounded walk", r.Stats.EdgeLookups)
	}
	t.Logf("bounded walk: node_visits=%d edge_lookups=%d terminals=%d (subscriptions=%d)",
		r.Stats.NodeVisits, r.Stats.EdgeLookups, r.Stats.TerminalsCollected, n)
}

func TestMatch_ManyCommonPrefixes_DistinctBranches(t *testing.T) {
	// A fan of "#"-free literal subscriptions; a query into one branch must
	// not visit the sibling subtrees.
	const branches = 500
	b := NewBuilder(nil)
	ids := make([]string, 0, branches)
	for i := 0; i < branches; i++ {
		id := "br" + padID(i)
		insert(b, "root/"+id+"/leaf", id)
		ids = append(ids, id)
	}
	s := b.Build(1)
	r := match(s, "root/br000007/leaf")
	want := []string{"br000007"}
	if !reflect.DeepEqual(r.Subscribers, want) {
		t.Fatalf("got %v, want %v", r.Subscribers, want)
	}
	if r.Stats.NodeVisits > 10 {
		t.Errorf("walk crossed into sibling branches: NodeVisits=%d", r.Stats.NodeVisits)
	}
}

func padID(i int) string {
	s := itoaK(i)
	for len(s) < 6 {
		s = "0" + s
	}
	return s
}

func itoaK(n int) string {
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

func init() {
	// Defensive: keep sort imported available for helper slices if extended.
	_ = sort.Strings
}
