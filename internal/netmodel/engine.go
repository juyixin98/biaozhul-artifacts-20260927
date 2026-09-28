package netmodel

import (
	"fmt"
	"time"
)

// Step records one named processing stage for explainability.
type Step struct {
	Name       string `json:"name"`
	Detail     string `json:"detail"`
	DurationUS int64  `json:"duration_us"`
}

// ComputeRequest is the family-independent input to the engine.
type ComputeRequest struct {
	Family  string   `json:"family"` // "ipv4" | "ipv6"; inferred when empty
	Allow   []string `json:"allow"`
	Exclude []string `json:"exclude"`
	// Strict rejects CIDRs carrying host bits; when false they are masked and
	// reported as warnings. The boundary policy never changes either way:
	// network and directed-broadcast addresses are always included.
	Strict bool `json:"strict"`
}

// ComputeResult is the fully explained outcome.
type ComputeResult struct {
	Family      string   `json:"family"`
	Width       int      `json:"width"`
	AllowNorm   []string `json:"allow_normalized"`
	ExcludeNorm []string `json:"exclude_normalized"`
	Prefixes    []string `json:"prefixes"`
	EmptyResult bool     `json:"empty_result"`
	Proof       Proof    `json:"proof"`
	Warnings    []string `json:"warnings"`
	Steps       []Step   `json:"steps"`
	blocks      []Block
	target      []Range
}

// Compute parses, validates, subtracts, covers and independently verifies.
func Compute(req ComputeRequest) (*ComputeResult, error) {
	res := &ComputeResult{Prefixes: []string{}, Warnings: []string{}, Steps: []Step{}}

	step := func(name string, fn func() (string, error)) error {
		t0 := time.Now()
		detail, err := fn()
		res.Steps = append(res.Steps, Step{Name: name, Detail: detail, DurationUS: time.Since(t0).Microseconds()})
		return err
	}

	var width int
	if err := step("resolve_family", func() (string, error) {
		w, fam, err := resolveFamily(req.Family, req.Allow, req.Exclude)
		if err != nil {
			return "", err
		}
		width = w
		res.Width = w
		res.Family = fam
		return fmt.Sprintf("family=%s width=%d", fam, w), nil
	}); err != nil {
		return nil, err
	}

	allowP := make([]Prefix, 0, len(req.Allow))
	if err := step("parse_allow", func() (string, error) {
		p, n, w, err := parseList(req.Allow, req.Strict)
		if err != nil {
			return "", err
		}
		allowP = p
		res.AllowNorm = n
		res.Warnings = append(res.Warnings, w...)
		return fmt.Sprintf("parsed=%d prefixes", len(p)), nil
	}); err != nil {
		return nil, err
	}

	excludeP := make([]Prefix, 0, len(req.Exclude))
	if err := step("parse_exclude", func() (string, error) {
		p, n, w, err := parseList(req.Exclude, req.Strict)
		if err != nil {
			return "", err
		}
		excludeP = p
		res.ExcludeNorm = n
		res.Warnings = append(res.Warnings, w...)
		return fmt.Sprintf("parsed=%d prefixes", len(p)), nil
	}); err != nil {
		return nil, err
	}

	var diff []Range
	if err := step("union_and_subtract", func() (string, error) {
		ar := make([]Range, len(allowP))
		for i, p := range allowP {
			ar[i] = PrefixRange(p)
		}
		er := make([]Range, len(excludeP))
		for i, p := range excludeP {
			er[i] = PrefixRange(p)
		}
		diff = Subtract(ar, er, width)
		return fmt.Sprintf("target_ranges=%d target_addresses=%s", len(diff), Size(diff)), nil
	}); err != nil {
		return nil, err
	}
	res.target = diff

	var blocks []Block
	if err := step("greedy_cover", func() (string, error) {
		blocks = Cover(diff, width)
		return fmt.Sprintf("emitted=%d prefixes", len(blocks)), nil
	}); err != nil {
		return nil, err
	}
	res.blocks = blocks

	if err := step("convert_and_format", func() (string, error) {
		for _, b := range blocks {
			p, err := ToPrefix(b, width)
			if err != nil {
				return "", err
			}
			res.Prefixes = append(res.Prefixes, p.String())
		}
		return fmt.Sprintf("formatted=%d cidrs", len(res.Prefixes)), nil
	}); err != nil {
		return nil, err
	}
	res.EmptyResult = len(blocks) == 0

	if err := step("independent_verification", func() (string, error) {
		violations, proof := VerifyCover(blocks, diff, width)
		res.Proof = proof
		if len(violations) > 0 {
			// Verification failure is an internal uncertainty: list every reason.
			return "", &VerificationFailure{Violations: violations}
		}
		return fmt.Sprintf("equivalent=%t blocks=%d mergeable_siblings=%d",
			proof.Equivalent, proof.BlockCount, len(proof.SiblingMerges)), nil
	}); err != nil {
		return nil, err
	}

	return res, nil
}

// VerificationFailure signals that the produced cover failed its own
// independent post-check. It is treated as an internal error, never silently
// returned as a success.
type VerificationFailure struct{ Violations []Violation }

func (e *VerificationFailure) Error() string {
	s := "cover failed independent verification: "
	for i, v := range e.Violations {
		if i > 0 {
			s += "; "
		}
		s += v.String()
	}
	return s
}

// Blocks exposes the raw block decomposition (used by tests).
func (r *ComputeResult) Blocks() []Block { return r.blocks }

// Target exposes the independently computed target ranges (used by tests).
func (r *ComputeResult) Target() []Range { return r.target }

func resolveFamily(declared string, allow, exclude []string) (int, string, error) {
	declared = familyName(declared)
	seen := map[string]int{}
	probe := func(list []string, label string) error {
		for _, s := range list {
			p, err := ParsePrefix(s)
			if err != nil {
				// Parse failures are reported later with full kind info; for
				// family resolution ignore unparseable entries.
				continue
			}
			fam := "ipv6"
			w := IPv6Bits
			if p.FamilyBits() == IPv4Bits {
				fam, w = "ipv4", IPv4Bits
			}
			if prev, ok := seen[fam]; !ok {
				seen[fam] = w
			} else if prev != w {
				return &PrefixError{Kind: KindFamilyMismatch, Input: s, msg: label}
			}
		}
		return nil
	}
	if err := probe(allow, "allow"); err != nil {
		return 0, "", err
	}
	if err := probe(exclude, "exclude"); err != nil {
		return 0, "", err
	}
	if declared != "" {
		w, ok := map[string]int{"ipv4": IPv4Bits, "ipv6": IPv6Bits}[declared]
		if !ok {
			return 0, "", &PrefixError{Kind: KindMalformed, Input: declared, msg: `family must be "ipv4" or "ipv6"`}
		}
		for f := range seen {
			if f != declared {
				return 0, "", &PrefixError{
					Kind:  KindFamilyMismatch,
					Input: f,
					msg:   fmt.Sprintf("declared family %s but input contains %s prefixes", declared, f),
				}
			}
		}
		return w, declared, nil
	}
	switch len(seen) {
	case 0:
		// No inputs at all: default to IPv4 empty-set semantics.
		return IPv4Bits, "ipv4", nil
	case 1:
		for f, w := range seen {
			return w, f, nil
		}
	}
	return 0, "", &PrefixError{Kind: KindFamilyMismatch, Input: "", msg: "request mixes IPv4 and IPv6 prefixes; split into two requests"}
}

func familyName(s string) string {
	switch s {
	case "", "IPv4", "IPV4", "v4", "4":
		if s == "" {
			return ""
		}
		return "ipv4"
	case "IPv6", "IPV6", "v6", "6":
		return "ipv6"
	default:
		return s
	}
}

func parseList(in []string, strict bool) ([]Prefix, []string, []string, error) {
	out := make([]Prefix, 0, len(in))
	canon := make([]string, 0, len(in))
	var warnings []string
	for _, s := range in {
		if strict {
			p, err := ParsePrefix(s)
			if err != nil {
				return nil, nil, nil, err
			}
			out = append(out, p)
			canon = append(canon, p.String())
			continue
		}
		p, warn, err := ParsePrefixLenient(s)
		if err != nil {
			return nil, nil, nil, err
		}
		if warn != "" {
			warnings = append(warnings, warn)
		}
		out = append(out, p)
		canon = append(canon, p.String())
	}
	return out, canon, warnings, nil
}
