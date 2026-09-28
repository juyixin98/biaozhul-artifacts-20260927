package topic

import (
	"errors"
	"strings"
	"testing"
)

func TestSplitSubject_FixedEmptyLayerSemantics(t *testing.T) {
	cases := []struct {
		in   string
		want []string
	}{
		{"/", []string{"", ""}},
		{"//", []string{"", "", ""}},
		{"/a", []string{"", "a"}},
		{"a/", []string{"a", ""}},
		{"a//b", []string{"a", "", "b"}},
		{"sport//tennis/", []string{"sport", "", "tennis", ""}},
		{"a/b/c", []string{"a", "b", "c"}},
	}
	for _, c := range cases {
		got := SplitSubject(c.in)
		if len(got) != len(c.want) {
			t.Errorf("SplitSubject(%q) = %v, want %v", c.in, got, c.want)
			continue
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("SplitSubject(%q)[%d] = %q, want %q (full=%v)", c.in, i, got[i], c.want[i], got)
			}
		}
	}
	if got := SplitSubject(""); got != nil {
		t.Errorf("SplitSubject(\"\") = %v, want nil", got)
	}
}

func TestValidateTopic(t *testing.T) {
	valid := []string{
		"a", "/", "a/b", "a//b", "/a", "a/", "sport/+", "sport/#",
		"weird+layer", "hash#embedded", "plus+and#hash",
	}
	for _, v := range valid {
		if err := ValidateTopic(v); err != nil {
			t.Errorf("ValidateTopic(%q) unexpected error: %v", v, err)
		}
	}

	// Pinned semantics: '+' and '#' inside a *topic* are ordinary literal
	// characters; only filters treat them as wildcards.
	if err := ValidateTopic("sport/+"); err != nil {
		t.Errorf("literal '+' in topic must be accepted, got %v", err)
	}

	invalid := []struct {
		in    string
		class ErrorClass
	}{
		{"", ErrEmpty},
		{strings.Repeat("x", MaxTopicLen+1), ErrTooLong},
		{"a/" + strings.Repeat("x", MaxLayerLen+1), ErrLayerTooLong},
		{strings.Repeat("a/", MaxLayers) + "a", ErrTooManyLayers},
	}
	for _, c := range invalid {
		err := ValidateTopic(c.in)
		if err == nil {
			t.Errorf("ValidateTopic(len=%d) expected %s", len(c.in), c.class)
			continue
		}
		var pe *ProtocolError
		if !errors.As(err, &pe) || pe.Class != c.class {
			t.Errorf("ValidateTopic(%q) class = %v, want %s", truncate(c.in), err, c.class)
		}
	}
}

func TestValidateFilter_WildcardPlacement(t *testing.T) {
	valid := []string{
		"#", "+", "a/#", "a/+", "sport/+/results/#",
		"a/+/b/+", "/#", "/+", "+/#", "a//+", "a//",
		"sport//", "x/+/y",
	}
	for _, v := range valid {
		if err := ValidateFilter(v); err != nil {
			t.Errorf("ValidateFilter(%q) unexpected rejection: %v", v, err)
		}
	}

	invalid := []struct {
		in    string
		class ErrorClass
	}{
		{"", ErrEmpty},
		{"a/#/b", ErrWildcardPosition},
		{"#/a", ErrWildcardPosition},
		{"a/foo#", ErrWildcardEmbedding},
		{"a/#b", ErrWildcardEmbedding},
		{"a/foo+", ErrWildcardEmbedding},
		{"a/+b", ErrWildcardEmbedding},
		{"a/+/c+", ErrWildcardEmbedding},
		{strings.Repeat("f/", MaxLayers) + "f", ErrTooManyLayers},
		{strings.Repeat("g", MaxFilterLen+1), ErrTooLong},
	}
	for _, c := range invalid {
		err := ValidateFilter(c.in)
		if err == nil {
			t.Errorf("ValidateFilter(%q) expected %s, got acceptance", c.in, c.class)
			continue
		}
		var pe *ProtocolError
		if !errors.As(err, &pe) || pe.Class != c.class {
			t.Errorf("ValidateFilter(%q) = %v, want class %s", c.in, err, c.class)
		}
	}
}

func truncate(s string) string {
	if len(s) <= 20 {
		return s
	}
	return s[:20] + "...(len=" + itoa(len(s)) + ")"
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
