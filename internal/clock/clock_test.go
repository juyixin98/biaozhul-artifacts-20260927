package clock

import "testing"

func TestMonotonic(t *testing.T) {
	c := New()
	if c.Now() != 0 {
		t.Fatalf("new clock at %d", c.Now())
	}
	if err := c.Advance(10); err != nil {
		t.Fatal(err)
	}
	if err := c.Advance(10); err != nil {
		t.Fatal(err) // staying in place is allowed
	}
	if c.Now() != 10 {
		t.Fatalf("now=%d", c.Now())
	}
	if err := c.Advance(9); err == nil {
		t.Fatal("backward advance must be rejected")
	}
	if c.Now() != 10 {
		t.Fatal("rejected advance must not move clock")
	}
}
