package protocol

import (
	"errors"
	"fmt"
)

// Failure is the single error type returned by the kernel and storage layers.
// It always carries a concrete FailureClass so that:
//   - the HTTP layer maps it deterministically to a status code;
//   - tests assert the category rather than a substring;
//   - an unexpected/unknown state is reported explicitly (FailInternal with
//     full detail) instead of being silently treated as success.
type Failure struct {
	Class  FailureClass
	Op     string // operation that produced it, e.g. "ack"
	Detail string // human-readable, may include ids; safe to log
	Cause  error  // optional wrapped underlying error
}

func (e *Failure) Error() string {
	if e.Cause != nil {
		return fmt.Sprintf("%s: %s: %s: %v", e.Op, e.Class, e.Detail, e.Cause)
	}
	return fmt.Sprintf("%s: %s: %s", e.Op, e.Class, e.Detail)
}

func (e *Failure) Unwrap() error { return e.Cause }

// NewFailure builds a *Failure.
func NewFailure(op string, class FailureClass, detail string, cause error) *Failure {
	return &Failure{Class: class, Op: op, Detail: detail, Cause: cause}
}

// AsFailure extracts a *Failure from err, returning nil if the error is not
// one.
func AsFailure(err error) *Failure {
	var f *Failure
	if errors.As(err, &f) {
		return f
	}
	return nil
}
