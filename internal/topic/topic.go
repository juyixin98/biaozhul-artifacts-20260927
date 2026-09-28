// Package topic defines the wire-level topic/filter protocol: layer splitting,
// wildcard placement rules, literal semantics of special characters, and the
// fixed length limits that decide whether the routing service accepts a
// request, rejects it, or cannot decide how to route it.
//
// These rules are the single source of truth shared by the in-memory kernel,
// the PostgreSQL store (which mirrors them via CHECK constraints) and the
// independent reference matcher used by the tests.
package topic

import (
	"errors"
	"strings"
)

const (
	// Separator delimits topic/filter layers. It is the only structural
	// character in the protocol.
	Separator = "/"
	// SingleLevel matches exactly one layer, including an empty one.
	SingleLevel = "+"
	// MultiLevel matches zero or more remaining layers. It is only legal as
	// the final layer of a filter.
	MultiLevel = "#"

	// MaxTopicLen bounds a published topic in bytes.
	MaxTopicLen = 4096
	// MaxFilterLen bounds a subscription filter in bytes.
	MaxFilterLen = 4096
	// MaxLayers bounds the number of layers (including empty ones).
	MaxLayers = 128
	// MaxLayerLen bounds a single layer in bytes.
	MaxLayerLen = 255
)

// ErrorClass is a stable, machine-readable failure category. Diagnostics, HTTP
// responses and test assertions all use these constants, never free-form
// strings, so clients can branch on them.
type ErrorClass string

const (
	ErrEmpty             ErrorClass = "EMPTY_SUBJECT"        // topic or filter was the empty string
	ErrTooLong           ErrorClass = "SUBJECT_TOO_LONG"     // exceeded MaxTopicLen/MaxFilterLen
	ErrTooManyLayers     ErrorClass = "TOO_MANY_LAYERS"      // exceeded MaxLayers
	ErrLayerTooLong      ErrorClass = "LAYER_TOO_LONG"       // a layer exceeded MaxLayerLen
	ErrWildcardPosition  ErrorClass = "WILDCARD_POSITION"    // '#' appeared outside the final layer
	ErrWildcardEmbedding ErrorClass = "WILDCARD_EMBEDDING"   // '+'/'#' embedded inside a layer
	ErrEmptyFilter       ErrorClass = "EMPTY_FILTER"         // reserved for explicit filter rejection
)

// ProtocolError carries the fixed category plus the offset/layer that triggered
// rejection, which the diagnostic layer logs alongside the request id.
type ProtocolError struct {
	Class  ErrorClass
	Detail string
}

func (e *ProtocolError) Error() string {
	if e.Detail == "" {
		return string(e.Class)
	}
	return string(e.Class) + ": " + e.Detail
}

// AsProtocolError extracts a *ProtocolError if err wraps one.
func AsProtocolError(err error) (*ProtocolError, bool) {
	var pe *ProtocolError
	if errors.As(err, &pe) {
		return pe, true
	}
	return nil, false
}

// SplitSubject splits a subject on the separator without collapsing runs and
// without stripping leading/trailing separators. Consequences, which are part
// of the fixed semantics:
//
//	""            -> nil               (the empty subject is invalid)
//	"/"           -> ["", ""]
//	"a/"          -> ["a", ""]
//	"/a"          -> ["", "a"]
//	"a//b"        -> ["a", "", "b"]
//	"a/b"         -> ["a", "b"]
//
// Empty layers are first-class layers: "+" matches them.
func SplitSubject(s string) []string {
	if s == "" {
		return nil
	}
	return strings.Split(s, Separator)
}

// ValidateTopic validates a published topic. Topics contain no wildcards: a
// "+" or "#" byte in a topic is an ordinary literal character (a subscription
// "sport/+" does not match topic "sport/+" unless an exact literal layer
// happens to coincide — it does not, because "+" is always the wildcard edge).
func ValidateTopic(t string) error {
	if t == "" {
		return &ProtocolError{Class: ErrEmpty, Detail: "topic must not be empty"}
	}
	if len(t) > MaxTopicLen {
		return &ProtocolError{Class: ErrTooLong, Detail: "topic length exceeds limit"}
	}
	layers := SplitSubject(t)
	if len(layers) > MaxLayers {
		return &ProtocolError{Class: ErrTooManyLayers, Detail: "topic layer count exceeds limit"}
	}
	for _, l := range layers {
		if len(l) > MaxLayerLen {
			return &ProtocolError{Class: ErrLayerTooLong, Detail: "topic layer exceeds length limit"}
		}
	}
	return nil
}

// ValidateFilter validates a subscription filter. Placement rules:
//
//   - "+" is a wildcard only as a whole layer ("a/+/b" valid, "a/foo+/b" not).
//   - "#" is a wildcard only as a whole layer and only as the final layer
//     ("a/#" valid, "a/#/b" and "a/foo#" rejected).
//
// Empty filters and filters with empty layers follow the general subject rules;
// empty layers are permitted ("/#", "a//+" are valid filters with an empty
// literal layer).
func ValidateFilter(f string) error {
	if f == "" {
		return &ProtocolError{Class: ErrEmpty, Detail: "filter must not be empty"}
	}
	if len(f) > MaxFilterLen {
		return &ProtocolError{Class: ErrTooLong, Detail: "filter length exceeds limit"}
	}
	layers := SplitSubject(f)
	if len(layers) > MaxLayers {
		return &ProtocolError{Class: ErrTooManyLayers, Detail: "filter layer count exceeds limit"}
	}
	for i, l := range layers {
		if len(l) > MaxLayerLen {
			return &ProtocolError{Class: ErrLayerTooLong, Detail: "filter layer exceeds length limit"}
		}
		switch {
		case l == MultiLevel && i != len(layers)-1:
			return &ProtocolError{Class: ErrWildcardPosition, Detail: "'#' is only valid as the final layer"}
		case l != MultiLevel && strings.Contains(l, MultiLevel):
			return &ProtocolError{Class: ErrWildcardEmbedding, Detail: "'#' is only valid as a standalone layer"}
		case l != SingleLevel && strings.Contains(l, SingleLevel):
			return &ProtocolError{Class: ErrWildcardEmbedding, Detail: "'+' is only valid as a standalone layer"}
		}
	}
	return nil
}
