package apperr_test

import (
	"errors"
	"fmt"
	"net/http"
	"testing"

	"flowrouter/internal/apperr"
)

func TestKindsAndCodes(t *testing.T) {
	cases := []struct {
		err    *apperr.Error
		kind   apperr.Kind
		code   string
		status int
	}{
		{apperr.Invalid("C", "m"), apperr.KindInvalidInput, "C", http.StatusBadRequest},
		{apperr.Conflict("C", "m"), apperr.KindStateConflict, "C", http.StatusConflict},
		{apperr.Exhausted("C", "m"), apperr.KindResourceExhausted, "C", http.StatusServiceUnavailable},
		{apperr.Compute("C", "m"), apperr.KindComputationFailure, "C", http.StatusUnprocessableEntity},
		{apperr.NoHealthy("C", "m"), apperr.KindNoHealthyMember, "C", http.StatusServiceUnavailable},
	}
	for _, tc := range cases {
		if tc.err.Kind != tc.kind || tc.err.Code != tc.code {
			t.Fatalf("kind/code mismatch: %+v", tc.err)
		}
		if apperr.HTTPStatus(tc.err.Kind) != tc.status {
			t.Errorf("status for %s = %d want %d", tc.kind, apperr.HTTPStatus(tc.err.Kind), tc.status)
		}
	}
}

func TestAsAndIs(t *testing.T) {
	base := apperr.Invalid("BAD_X", "detail")
	wrapped := fmt.Errorf("outer: %w", base)
	got, ok := apperr.As(wrapped)
	if !ok || got.Code != "BAD_X" {
		t.Fatalf("as through wrap: %v", wrapped)
	}
	// exact code match
	if !errors.Is(wrapped, apperr.Invalid("BAD_X", "")) {
		t.Fatal("errors.Is exact code should match")
	}
	// same kind, different code -> no match
	if errors.Is(wrapped, apperr.Invalid("OTHER", "")) {
		t.Fatal("errors.Is must not match a different code")
	}
	// unrelated error
	if _, ok := apperr.As(errors.New("plain")); ok {
		t.Fatal("plain error must not classify")
	}
}

func TestWithCause(t *testing.T) {
	root := errors.New("driver locked")
	e := apperr.Exhausted("STORE_BUSY", "db busy").WithCause(root)
	if !errors.Is(e, root) {
		t.Fatal("Unwrap must expose the cause")
	}
	if e.Error() == "" || e.Cause != root {
		t.Fatal("cause not retained")
	}
}
