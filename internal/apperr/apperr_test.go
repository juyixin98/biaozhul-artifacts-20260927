package apperr

import (
	"errors"
	"fmt"
	"testing"
)

func TestCategoryAndAs(t *testing.T) {
	base := New(Conflict, "ownership_conflict", "field %s", "x")
	if base.Category != Conflict || base.Code != "ownership_conflict" {
		t.Fatalf("wrong fields: %+v", base)
	}
	if got := base.Error(); got != "conflict/ownership_conflict: field x" {
		t.Fatalf("error text = %q", got)
	}
	wrapped := fmt.Errorf("load: %w", base)
	got, ok := As(wrapped)
	if !ok || got != base {
		t.Fatal("As must unwrap fmt-wrapped apperr")
	}
	if _, ok := As(errors.New("plain")); ok {
		t.Fatal("plain error must not classify")
	}
}
