package model

import (
	"errors"
	"fmt"
)

// Error kinds. They double as HTTP error codes (camelCase strings) and
// as condition reasons. Keeping a closed set prevents callers from
// treating unknown/exceptional states as success.
const (
	ErrKindNotFound          = "NotFound"
	ErrKindAlreadyExists     = "AlreadyExists"
	ErrKindConflict          = "Conflict"
	ErrKindInvalid           = "Invalid"
	ErrKindUIDMismatch       = "OwnerUIDMismatch"
	ErrKindOwnerNotFound     = "OwnerNotFound"
	ErrKindRefAlreadyExists  = "OwnerRefAlreadyExists"
	ErrKindOwnershipCycle    = "OwnershipCycle"
	ErrKindOwnerDeleting     = "OwnerDeleting"
	ErrKindUnknownFinalizer  = "UnknownFinalizer"
	ErrKindFinalizerFailed   = "FinalizerFailed"
	ErrKindInvariantBroken   = "InvariantBroken"
	ErrKindInvalidPolicy     = "InvalidDeletionPolicy"
	ErrKindStorage           = "StorageError"
	ErrKindInternal          = "Internal"
)

// Error is the canonical domain error. Storage and controller always
// wrap failures in this type (or return errors.Is-compatible values) so
// the HTTP adapter can map the concrete category to a status code and
// tests can assert the failure category.
type Error struct {
	Kind    string `json:"kind"`
	Message string `json:"message"`
	// Subject identifies the resource the error refers to (ns/name),
	// kept out of Message so structured logs stay parseable.
	Subject string `json:"subject,omitempty"`
	Detail  string `json:"detail,omitempty"`
}

func (e *Error) Error() string {
	if e.Subject != "" {
		return e.Kind + ": " + e.Subject + ": " + e.Message
	}
	return e.Kind + ": " + e.Message
}

// NewError constructs a categorized error.
func NewError(kind, msg string) *Error {
	return &Error{Kind: kind, Message: msg}
}

// Errorf is NewError with fmt.Sprintf semantics.
func Errorf(kind, format string, args ...any) *Error {
	return &Error{Kind: kind, Message: fmt.Sprintf(format, args...)}
}

// ErrorSubject annotates an error with a resource subject.
func ErrorSubject(err *Error, subject, detail string) *Error {
	err.Subject = subject
	err.Detail = detail
	return err
}

// AsError extracts a *model.Error from any error chain.
func AsError(err error) (*Error, bool) {
	if err == nil {
		return nil, false
	}
	var me *Error
	if errors.As(err, &me) {
		return me, true
	}
	return nil, false
}

// IsKind reports whether err (unwrapped) is a model.Error of the given kind.
func IsKind(err error, kind string) bool {
	if me, ok := AsError(err); ok {
		return me.Kind == kind
	}
	return false
}
