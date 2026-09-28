// Package engine orchestrates the CIDR-cover computation over parsed address
// sets: it splits entries by family, computes allow-minus-exclude per family,
// runs an independent post-computation verification, and emits an explainable
// trace (key steps, versions, code location, separated failures/advisories).
package engine

import (
	"fmt"
	"math/big"
	"runtime"

	"cidrcov/internal/ipparse"
	"cidrcov/internal/netmodel"
)

// AlgorithmVersion is bumped whenever the cover algorithm changes; it is
// echoed in every result and persisted with every request.
const AlgorithmVersion = "cover-greedy-v1"

// ServiceVersion is the overall service revision.
const ServiceVersion = "1.0.0"

// Failure codes produced by the engine.
const (
	CodeInvalidEntry      = "invalid_entry"
	CodeInternalVerify    = "internal_verification_failed"
	CodeEntryLimitReached = "entry_limit_reached"
)

// Failure is one fatal problem with a stable code and a 1-based entry index.
type Failure struct {
	Code     string `json:"code"`
	List     string `json:"list"`            // "allow" | "exclude" | "ipv4" | "ipv6"
	Index    int    `json:"index,omitempty"` // 1-based position in the list
	Input    string `json:"input,omitempty"`
	Reason   string `json:"reason"`
	Location string `json:"location,omitempty"` // producer package.func
}

// Advisory re-exports parse advisories plus engine-level uncertainty notes.
type Advisory = ipparse.Advisory

// Engine-level advisory codes (parse-level codes live in ipparse).
const (
	// AdvExcludeNotInAllow flags exclusions that remove nothing.
	AdvExcludeNotInAllow = "exclude_outside_allow"
	// AdvDuplicateEntry flags repeated identical canonical entries.
	AdvDuplicateEntry = "duplicate_entry"
)

// PrefixResult is one output prefix with family metadata.
type PrefixResult struct {
	Family string `json:"family"`
	CIDR   string `json:"cidr"`
	Base   string `json:"base_int"` // decimal big integer
	Len    int    `json:"prefix_len"`
	Size   string `json:"block_size"` // decimal 2^(width-len)
}

// TraceStep is one explainable processing step.
type TraceStep struct {
	Step     string `json:"step"`
	Location string `json:"location"`
	Detail   string `json:"detail"`
}

// Result is the fully explained computation outcome.
type Result struct {
	Status            string         `json:"status"` // "ok" | "empty" | "error"
	AlgorithmVersion  string         `json:"algorithm_version"`
	ServiceVersion    string         `json:"service_version"`
	GoVersion         string         `json:"go_version"`
	Prefixes          []PrefixResult `json:"prefixes"`
	Advisories        []Advisory     `json:"advisories"`
	Failures          []Failure      `json:"failures,omitempty"`
	Trace             []TraceStep    `json:"trace"`
	TotalCovered      string         `json:"total_covered_addresses"`
	V4PrefixCount     int            `json:"ipv4_prefix_count"`
	V6PrefixCount     int            `json:"ipv6_prefix_count"`
	InputAllowCount   int            `json:"input_allow_count"`
	InputExcludeCount int            `json:"input_exclude_count"`
}

// Options bounds request size.
type Options struct {
	MaxEntriesPerList int // 0 => default 10_000
}

func (o Options) maxEntries() int {
	if o.MaxEntriesPerList <= 0 {
		return 10_000
	}
	return o.MaxEntriesPerList
}

type familyInput struct {
	width   int
	kind    string
	allow   []netmodel.Interval
	exclude []netmodel.Interval
}

// Compute parses both lists, computes the minimal cover, verifies it and
// returns an explained Result. User-input problems never surface as Go errors:
// they are encoded as Result.Failures with stable codes.
func Compute(allowIn, excludeIn []string, opts Options) *Result {
	res := &Result{
		Status:           "ok",
		AlgorithmVersion: AlgorithmVersion,
		ServiceVersion:   ServiceVersion,
		GoVersion:        runtime.Version(),
		Prefixes:         []PrefixResult{},
		Advisories:       []Advisory{},
		Trace:            []TraceStep{},
	}
	res.InputAllowCount = len(allowIn)
	res.InputExcludeCount = len(excludeIn)
	maxN := opts.maxEntries()

	trace := func(step, detail string) {
		res.Trace = append(res.Trace, TraceStep{Step: step, Location: "engine.Compute", Detail: detail})
	}
	trace("start", fmt.Sprintf("parsing %d allow and %d exclude entries (limit %d/list)",
		len(allowIn), len(excludeIn), maxN))

	if len(allowIn) > maxN || len(excludeIn) > maxN {
		res.Status = "error"
		which, n := "allow", len(allowIn)
		if len(excludeIn) > maxN {
			which, n = "exclude", len(excludeIn)
		}
		res.Failures = append(res.Failures, Failure{
			Code: CodeEntryLimitReached, List: which, Location: "engine.Compute",
			Reason: fmt.Sprintf("%d entries exceeds limit %d per list", n, maxN),
		})
		trace("abort", "entry limit reached before parsing")
		return res
	}

	v4 := &familyInput{width: 32, kind: ipparse.KindV4}
	v6 := &familyInput{width: 128, kind: ipparse.KindV6}
	families := map[string]*familyInput{ipparse.KindV4: v4, ipparse.KindV6: v6}
	// Duplicate detection is scoped PER LIST: the same canonical prefix
	// legitimately appears in both allow and exclude (that is how you cover a
	// set then punch it).
	seen := map[string]map[string]bool{
		"allow":   {},
		"exclude": {},
	}

	parseOne := func(listName string, i int, raw string) bool {
		entry, advs, err := ipparse.Parse(raw)
		res.Advisories = append(res.Advisories, advs...)
		if err != nil {
			pe, _ := err.(*ipparse.ParseError)
			reason := err.Error()
			if pe != nil {
				reason = fmt.Sprintf("%s: %s", pe.Class, pe.Detail)
			}
			res.Failures = append(res.Failures, Failure{
				Code: CodeInvalidEntry, List: listName, Index: i + 1, Input: raw,
				Reason: reason, Location: "ipparse.Parse",
			})
			return false
		}
		key := entry.Kind + "|" + entry.CanonicalText
		if seen[listName][key] {
			res.Advisories = append(res.Advisories, Advisory{
				Code: AdvDuplicateEntry, Input: raw,
				Message:   fmt.Sprintf("duplicate canonical entry %s in %s list; collapsed", entry.CanonicalText, listName),
				Effective: entry.CanonicalText,
			})
		}
		seen[listName][key] = true
		f := families[entry.Kind]
		if listName == "allow" {
			f.allow = append(f.allow, entry.Interval)
		} else {
			f.exclude = append(f.exclude, entry.Interval)
		}
		return true
	}
	for i, raw := range allowIn {
		parseOne("allow", i, raw)
	}
	for i, raw := range excludeIn {
		parseOne("exclude", i, raw)
	}
	if len(res.Failures) > 0 {
		res.Status = "error"
		trace("abort", fmt.Sprintf("%d invalid entr(ies); no cover computed", len(res.Failures)))
		return res
	}
	trace("parse", "all entries parsed, canonicalized and split by address family")

	total := new(big.Int)
	for _, f := range []*familyInput{v4, v6} {
		prefs, steps, fail := coverFamily(f, res)
		res.Trace = append(res.Trace, steps...)
		if fail != nil {
			res.Status = "error"
			res.Failures = append(res.Failures, *fail)
			trace("abort", "verification failed for "+f.kind)
			return res
		}
		for _, p := range prefs {
			cidr := ipparse.Format(f.kind, p)
			size := new(big.Int).Lsh(big.NewInt(1), uint(f.width-p.Len))
			total.Add(total, size)
			res.Prefixes = append(res.Prefixes, PrefixResult{
				Family: f.kind, CIDR: cidr, Base: p.Base.String(),
				Len: p.Len, Size: size.String(),
			})
			if f.kind == ipparse.KindV4 {
				res.V4PrefixCount++
			} else {
				res.V6PrefixCount++
			}
		}
	}
	// Prefixes leave decomposition already ascending per family; merge the two
	// family slices (v4 first) without resorting by value.
	// (No numeric sort needed — IntervalToPrefixes emits in ascending order.)
	res.TotalCovered = total.String()
	if len(res.Prefixes) == 0 {
		res.Status = "empty"
		trace("finish", "set difference is empty (allow set empty, or every allowed address excluded)")
	} else {
		trace("finish", fmt.Sprintf("emitted %d prefixes covering %s addresses; verified non-overlapping and sibling-merge free",
			len(res.Prefixes), total.String()))
	}
	return res
}

func coverFamily(f *familyInput, res *Result) ([]netmodel.Prefix, []TraceStep, *Failure) {
	var steps []TraceStep
	allowed := netmodel.Union(f.allow)
	if len(allowed) == 0 {
		steps = append(steps, TraceStep{Step: f.kind + ":skip", Location: "engine.coverFamily",
			Detail: "no allow entries in this family"})
		return nil, steps, nil
	}
	excluded := netmodel.Union(f.exclude)

	// Uncertainty, listed separately from failures: an exclusion disjoint
	// from the allow set removes no address and usually signals a caller mistake.
	for _, ex := range f.exclude {
		hit := false
		for _, al := range allowed {
			if al.Start.Cmp(ex.End) <= 0 && ex.Start.Cmp(al.End) <= 0 {
				hit = true
				break
			}
		}
		if !hit {
			res.Advisories = append(res.Advisories, Advisory{
				Code:    AdvExcludeNotInAllow,
				Input:   fmt.Sprintf("%s:[%s..%s]", f.kind, ex.Start.String(), ex.End.String()),
				Message: "exclude entry does not intersect the allow set; it changes nothing",
			})
		}
	}

	diff := netmodel.Subtract(allowed, excluded)
	var prefs []netmodel.Prefix
	for _, iv := range diff {
		prefs = append(prefs, netmodel.IntervalToPrefixes(f.width, iv.Start, iv.End)...)
	}
	steps = append(steps,
		TraceStep{Step: f.kind + ":union", Location: "netmodel.Union",
			Detail: fmt.Sprintf("normalized to %d allow / %d exclude disjoint interval(s)", len(allowed), len(excluded))},
		TraceStep{Step: f.kind + ":subtract", Location: "netmodel.Subtract",
			Detail: fmt.Sprintf("allow\\exclude leaves %d contiguous interval run(s)", len(diff))},
		TraceStep{Step: f.kind + ":decompose", Location: "netmodel.IntervalToPrefixes",
			Detail: fmt.Sprintf("greedy largest-aligned-block decomposition produced %d prefix(es)", len(prefs))},
	)
	if verr := verify(f.width, allowed, excluded, prefs); verr != nil {
		return nil, steps, &Failure{Code: CodeInternalVerify, List: f.kind,
			Location: "engine.verify", Reason: verr.Error()}
	}
	steps = append(steps, TraceStep{Step: f.kind + ":verify", Location: "engine.verify",
		Detail: "rebuilt cover equals allow\\exclude exactly; no overlap; no mergeable sibling pair"})
	return prefs, steps, nil
}
