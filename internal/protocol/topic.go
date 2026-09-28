// Package protocol implements the topic routing wire protocol: topic/filter
// syntax, validation rules, the canonical error taxonomy and the DTOs shared
// by the HTTP layer and the replay interface.
//
// Topic semantics (fixed):
//
//   - A topic is a '/'-separated sequence of levels. Leading, trailing and
//     repeated '/' are significant: they produce EMPTY levels.
//     "a/b"  -> ["a","b"]
//     "/a/"  -> ["", "a", ""]
//     "a//b" -> ["a", "", "b"]
//     "/"    -> ["", ""]
//     The only rejected form is the completely empty string "".
//
//   - A subscription filter is a topic with optional wildcards:
//     '+' is a single-level wildcard: it matches EXACTLY ONE level, and
//     DOES match an empty level. It must occupy a whole level on its own.
//     '#' is a multi-level wildcard: it is only legal as the FINAL level,
//     on its own, and matches ZERO OR MORE remaining levels (including
//     empty levels).
//
//   - '+' or '#' appearing inside a non-wildcard level (e.g. "a+b", "a#b",
//     "+a", "a/#/b", "a/#b") is a syntax error, never a literal. Wildcards are
//     only accepted in the prescribed positions.
//
//   - A topic used for PUBLISHING must contain no wildcards at all; any whole
//     '+' or '#' level is rejected.
package protocol

import (
	"errors"
	"fmt"
	"strings"
)

// Fixed protocol limits. These are part of the wire contract.
const (
	MaxTopicBytes   = 4096
	MaxFilterBytes  = 4096
	MaxLevels       = 128
	MaxLevelBytes   = 1024
	MaxFilterCount  = 64
	MaxSubscriberID = 128
	MaxPayloadBytes = 256 * 1024
)

// SingleLevel and MultiLevel are the only wildcard tokens.
const (
	SingleLevel = "+"
	MultiLevel  = "#"
	Separator   = "/"
)

// ErrorCode is the machine-readable failure category returned in diagnostics.
type ErrorCode string

const (
	ErrMalformed       ErrorCode = "MALFORMED"          // unparseable request body / bad content type
	ErrValidation      ErrorCode = "VALIDATION_FAILED"  // semantically invalid field
	ErrWildcardSyntax  ErrorCode = "WILDCARD_SYNTAX"    // wildcard in illegal position
	ErrWildcardInTopic ErrorCode = "WILDCARD_IN_TOPIC"  // publish topic contains + or #
	ErrEmptyTopic      ErrorCode = "EMPTY_TOPIC"        // topic/filter is the empty string
	ErrTooLarge        ErrorCode = "TOO_LARGE"          // protocol limit exceeded
	ErrVersionConflict ErrorCode = "VERSION_CONFLICT"   // optimistic snapshot lock
	ErrNotFound        ErrorCode = "NOT_FOUND"          // subscriber / version / message unknown
	ErrDeleted         ErrorCode = "SUBSCRIBER_DELETED" // update targets a deleted subscriber
	ErrInternal        ErrorCode = "INTERNAL"           // storage/unknown failure
)

// ProtocolError carries a fixed category and a human explanation.
type ProtocolError struct {
	Code   ErrorCode
	Reason string
}

func (e *ProtocolError) Error() string { return string(e.Code) + ": " + e.Reason }

// NewError builds a categorized protocol error.
func NewError(code ErrorCode, format string, args ...any) *ProtocolError {
	return &ProtocolError{Code: code, Reason: sprintf(format, args...)}
}

// AsProtocolError extracts a *ProtocolError from any error, mapping nil to nil.
func AsProtocolError(err error) *ProtocolError {
	var pe *ProtocolError
	if errors.As(err, &pe) {
		return pe
	}
	return nil
}

// Topic is a validated sequence of levels used for matching.
type Topic struct {
	levels []string
	raw    string
}

// ParseTopic parses and validates a PUBLISH topic: wildcards are forbidden.
func ParseTopic(raw string) (Topic, error) {
	t, err := parseLevels(raw, false)
	if err != nil {
		return Topic{}, err
	}
	return t, nil
}

// ParseFilter parses and validates a SUBSCRIPTION filter: wildcards allowed in
// their prescribed positions.
func ParseFilter(raw string) (Topic, error) {
	return parseLevels(raw, true)
}

func parseLevels(raw string, allowWildcards bool) (Topic, error) {
	if raw == "" {
		return Topic{}, NewError(ErrEmptyTopic, "topic must not be the empty string")
	}
	limit := MaxTopicBytes
	if allowWildcards {
		limit = MaxFilterBytes
	}
	if len(raw) > limit {
		return Topic{}, NewError(ErrTooLarge, "topic length %d exceeds limit %d bytes", len(raw), limit)
	}
	if strings.IndexByte(raw, 0x00) >= 0 {
		return Topic{}, NewError(ErrValidation, "topic must not contain NUL bytes")
	}

	levels := strings.Split(raw, "/")
	if len(levels) > MaxLevels {
		return Topic{}, NewError(ErrTooLarge, "topic has %d levels, limit is %d", len(levels), MaxLevels)
	}
	for i, lvl := range levels {
		if len(lvl) > MaxLevelBytes {
			return Topic{}, NewError(ErrTooLarge, "level %d length %d exceeds limit %d", i, len(lvl), MaxLevelBytes)
		}
		if err := validateLevel(lvl, i, len(levels), allowWildcards); err != nil {
			return Topic{}, err
		}
	}
	return Topic{levels: levels, raw: raw}, nil
}

func validateLevel(lvl string, idx, total int, allowWildcards bool) error {
	switch {
	case lvl == SingleLevel:
		if !allowWildcards {
			return NewError(ErrWildcardInTopic, "publish topic must not contain the '+' wildcard at level %d", idx)
		}
		return nil // '+' is legal anywhere as a whole level.
	case lvl == MultiLevel:
		if !allowWildcards {
			return NewError(ErrWildcardInTopic, "publish topic must not contain the '#' wildcard at level %d", idx)
		}
		if idx != total-1 {
			return NewError(ErrWildcardSyntax, "'#' is only legal as the final level (found at level %d of %d)", idx, total)
		}
		return nil
	case strings.ContainsAny(lvl, SingleLevel+MultiLevel):
		// '+'/'#' embedded inside an otherwise literal level are never literals.
		if allowWildcards {
			return NewError(ErrWildcardSyntax, "level %d %q mixes wildcard characters into a non-wildcard level; '+' and '#' must occupy a whole level", idx, lvl)
		}
		return NewError(ErrWildcardInTopic, "level %d %q contains a wildcard character", idx, lvl)
	}
	return nil
}

// Levels returns the parsed levels. Empty strings are real (empty) levels.
func (t Topic) Levels() []string { return t.levels }

// Len returns the number of levels.
func (t Topic) Len() int { return len(t.levels) }

// Raw returns the original topic string.
func (t Topic) Raw() string { return t.raw }

// IsMulti reports whether the final (validated) level is '#'.
func (t Topic) IsMulti() bool {
	n := len(t.levels)
	return n > 0 && levelsLast(t.levels) == MultiLevel
}

func levelsLast(s []string) string { return s[len(s)-1] }

// Filter is a parsed subscription filter with its kind precomputed.
type Filter struct {
	Topic
	multi bool
}

// AsFilter converts a validated filter topic. The value must have been parsed
// with ParseFilter.
func AsFilter(t Topic) Filter {
	return Filter{Topic: t, multi: t.IsMulti()}
}

// IsMulti reports whether this filter terminates with '#'.
func (f Filter) IsMulti() bool { return f.multi }

// ValidateSubscriberID applies the subscriber identifier rules.
func ValidateSubscriberID(id string) error {
	if id == "" {
		return NewError(ErrValidation, "subscriber_id must not be empty")
	}
	if len(id) > MaxSubscriberID {
		return NewError(ErrTooLarge, "subscriber_id length %d exceeds limit %d", len(id), MaxSubscriberID)
	}
	if strings.TrimSpace(id) != id {
		return NewError(ErrValidation, "subscriber_id must not have leading/trailing whitespace")
	}
	return nil
}

// ValidateFilters validates a snapshot filter set: bounded size, no duplicates.
func ValidateFilters(raw []string) ([]Filter, error) {
	if len(raw) == 0 {
		return nil, NewError(ErrValidation, "filters snapshot must contain at least one filter")
	}
	if len(raw) > MaxFilterCount {
		return nil, NewError(ErrTooLarge, "filters snapshot has %d entries, limit is %d", len(raw), MaxFilterCount)
	}
	seen := make(map[string]struct{}, len(raw))
	out := make([]Filter, 0, len(raw))
	for i, r := range raw {
		t, err := ParseFilter(r)
		if err != nil {
			return nil, err
		}
		if _, dup := seen[r]; dup {
			return nil, NewError(ErrValidation, "duplicate filter %q at index %d", r, i)
		}
		seen[r] = struct{}{}
		out = append(out, AsFilter(t))
	}
	return out, nil
}

func sprintf(format string, args ...any) string {
	return fmt.Sprintf(format, args...)
}
