package protocol

import "testing"

func TestParseTopic_FixedEmptyLevelSemantics(t *testing.T) {
	cases := []struct {
		raw    string
		levels []string
	}{
		{"a/b", []string{"a", "b"}},
		{"/a/", []string{"", "a", ""}},
		{"a//b", []string{"a", "", "b"}},
		{"/", []string{"", ""}},
		{"//", []string{"", "", ""}},
		{"a/", []string{"a", ""}},
		{"/a", []string{"", "a"}},
	}
	for _, tc := range cases {
		got, err := ParseTopic(tc.raw)
		if err != nil {
			t.Fatalf("ParseTopic(%q) unexpected error: %v", tc.raw, err)
		}
		if len(got.Levels()) != len(tc.levels) {
			t.Fatalf("ParseTopic(%q) levels = %#v, want %#v", tc.raw, got.Levels(), tc.levels)
		}
		for i := range tc.levels {
			if got.Levels()[i] != tc.levels[i] {
				t.Fatalf("ParseTopic(%q) level %d = %q, want %q (full=%#v)",
					tc.raw, i, got.Levels()[i], tc.levels[i], got.Levels())
			}
		}
		if got.Raw() != tc.raw {
			t.Fatalf("Raw() = %q, want %q", got.Raw(), tc.raw)
		}
	}
}

func TestParseTopic_EmptyStringRejected(t *testing.T) {
	_, err := ParseTopic("")
	assertCode(t, err, ErrEmptyTopic)
}

func TestParseTopic_WildcardsRejectedByCategory(t *testing.T) {
	bad := []struct {
		raw  string
		want ErrorCode
	}{
		{"a/+/c", ErrWildcardInTopic},
		{"a/#", ErrWildcardInTopic},
		{"+", ErrWildcardInTopic},
		{"#", ErrWildcardInTopic},
		{"a+b", ErrWildcardInTopic},
		{"a#b", ErrWildcardInTopic},
	}
	for _, tc := range bad {
		_, err := ParseTopic(tc.raw)
		assertCode(t, err, tc.want)
	}
}

func TestParseFilter_WildcardPositions(t *testing.T) {
	// Legal placements.
	legal := []string{"+", "#", "+/b", "a/+", "+/+", "a/#", "a/+/c/#", "/+/"}
	for _, raw := range legal {
		if _, err := ParseFilter(raw); err != nil {
			t.Fatalf("ParseFilter(%q) unexpected error: %v", raw, err)
		}
	}

	// Illegal placements with the specific category: '#' anywhere but the
	// final level, or wildcard chars embedded in a literal level.
	illegal := []string{"#/x", "a/#/b", "#/+", "a/#/c", "+/#/x", "a+b", "a#b", "+a", "a+", "a#", "#b"}
	for _, raw := range illegal {
		_, err := ParseFilter(raw)
		assertCode(t, err, ErrWildcardSyntax)
	}
}

func TestParseFilter_MultiMatchesZeroOrMoreFlag(t *testing.T) {
	f, err := ParseFilter("a/#")
	if err != nil {
		t.Fatalf("ParseFilter: %v", err)
	}
	if !AsFilter(f).IsMulti() {
		t.Fatalf("a/# should be multi")
	}
	f2, err := ParseFilter("a/+")
	if err != nil {
		t.Fatalf("ParseFilter: %v", err)
	}
	if AsFilter(f2).IsMulti() {
		t.Fatalf("a/+ should not be multi")
	}
}

func TestParse_LimitsAndCategories(t *testing.T) {
	long := make([]byte, MaxTopicBytes+1)
	for i := range long {
		long[i] = 'a'
	}
	if _, err := ParseTopic(string(long)); err == nil || AsProtocolError(err).Code != ErrTooLarge {
		t.Fatalf("oversized topic: got %v, want TOO_LARGE", err)
	}

	manyLevels := ""
	for i := 0; i <= MaxLevels; i++ {
		if i > 0 {
			manyLevels += "/"
		}
		manyLevels += "a"
	}
	if _, err := ParseFilter(manyLevels); err == nil || AsProtocolError(err).Code != ErrTooLarge {
		t.Fatalf("too many levels: got %v, want TOO_LARGE", err)
	}
}

func TestValidateFilters_DuplicatesAndEmpty(t *testing.T) {
	if _, err := ValidateFilters(nil); err == nil || AsProtocolError(err).Code != ErrValidation {
		t.Fatalf("empty snapshot: got %v, want VALIDATION_FAILED", err)
	}
	if _, err := ValidateFilters([]string{"a", "a"}); err == nil || AsProtocolError(err).Code != ErrValidation {
		t.Fatalf("duplicate filter: got %v, want VALIDATION_FAILED", err)
	}
	fs, err := ValidateFilters([]string{"a/+", "b/#"})
	if err != nil {
		t.Fatalf("valid snapshot rejected: %v", err)
	}
	if len(fs) != 2 {
		t.Fatalf("filters len = %d, want 2", len(fs))
	}
}

func TestValidateSubscriberID(t *testing.T) {
	for _, bad := range []string{"", " space", "trail ", string(make([]byte, MaxSubscriberID+1))} {
		if err := ValidateSubscriberID(bad); err == nil {
			t.Fatalf("subscriber id %q accepted", bad)
		}
	}
	if err := ValidateSubscriberID("ok-id_1"); err != nil {
		t.Fatalf("valid id rejected: %v", err)
	}
}

func assertCode(t *testing.T, err error, want ErrorCode) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error %s, got nil", want)
	}
	pe := AsProtocolError(err)
	if pe == nil {
		t.Fatalf("error %v is not a *ProtocolError", err)
	}
	if pe.Code != want {
		t.Fatalf("error code = %s, want %s (reason: %s)", pe.Code, want, pe.Reason)
	}
}
