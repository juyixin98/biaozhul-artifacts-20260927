package reference

import (
	"testing"

	"topicrouter/internal/protocol"
)

func f(t *testing.T, raw string) protocol.Filter {
	t.Helper()
	v, err := protocol.ParseFilter(raw)
	if err != nil {
		t.Fatalf("ParseFilter(%q): %v", raw, err)
	}
	return protocol.AsFilter(v)
}

func topic(t *testing.T, raw string) protocol.Topic {
	t.Helper()
	v, err := protocol.ParseTopic(raw)
	if err != nil {
		t.Fatalf("ParseTopic(%q): %v", raw, err)
	}
	return v
}

func TestFilterMatches_DirectSemantics(t *testing.T) {
	cases := []struct {
		filter string
		topic  string
		want   bool
		note   string
	}{
		{"a/b", "a/b", true, "exact"},
		{"a/b", "a/c", false, "literal mismatch"},
		{"a/b", "a", false, "topic shorter"},
		{"a", "a/b", false, "filter shorter, no hash"},
		{"+", "a", true, "plus matches one literal"},
		{"+", "a/b", false, "plus matches exactly one, not two"},
		{"a/+", "a/", true, "plus matches trailing empty level"},
		{"+/a", "/a", true, "plus matches leading empty level"},
		{"a//+", "a//b", true, "empty middle level fixed + empty match"},
		{"a//b", "a/x/b", false, "empty filter level only matches empty"},
		{"#", "a", true, "root hash matches one"},
		{"#", "a/b/c", true, "root hash matches many"},
		{"#", "/", true, "root hash matches empty levels"},
		{"a/#", "a", true, "hash matches ZERO remaining"},
		{"a/#", "a/b/c", true, "hash matches several remaining"},
		{"a/#", "b/c", false, "prefix must match"},
		{"a/#", "a/", true, "hash matches one empty remaining"},
		{"a/b/#", "a/b", true, "hash zero remaining at depth"},
		{"a/b/#", "a/b/c/d", true, "hash deep remaining"},
		{"a/b/#", "a/x/c/d", false, "prefix mismatch before hash"},
		{"/#", "/a", true, "leading empty + hash"},
		{"/#", "a", false, "leading empty required"},
		{"+/+", "/", true, "two plus match two empty levels"},
	}
	for _, tc := range cases {
		got := filterMatches(f(t, tc.filter).Levels(), topic(t, tc.topic).Levels())
		if got != tc.want {
			t.Errorf("%s: filter %q vs topic %q = %v, want %v",
				tc.note, tc.filter, tc.topic, got, tc.want)
		}
	}
}

func TestMatch_DedupAndWinner(t *testing.T) {
	frames := []protocol.Frame{
		{SubscriberID: "s1", Filters: []protocol.Filter{
			f(t, "a/+/c"), f(t, "a/b/c"), f(t, "#"),
		}},
		{SubscriberID: "s2", Filters: []protocol.Filter{f(t, "x/y")}},
	}
	hits := Match(topic(t, "a/b/c"), frames)
	if len(hits) != 1 {
		t.Fatalf("hits = %v, want exactly one subscriber", hits)
	}
	if hits[0].SubscriberID != "s1" || hits[0].Filter != "a/b/c" {
		t.Fatalf("winner = %+v, want s1/a/b/c (fewest wildcards)", hits[0])
	}
}

func TestScans_IsFullTableCount(t *testing.T) {
	frames := []protocol.Frame{
		{SubscriberID: "a", Filters: []protocol.Filter{f(t, "1"), f(t, "2")}},
		{SubscriberID: "b", Filters: []protocol.Filter{f(t, "3")}},
	}
	if got := Scans(frames); got != 3 {
		t.Fatalf("Scans = %d, want 3 (total filters)", got)
	}
}
