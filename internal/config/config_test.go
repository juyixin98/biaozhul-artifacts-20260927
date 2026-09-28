package config

import (
	"strings"
	"testing"
)

func TestParseErrorCategories(t *testing.T) {
	cases := []struct {
		name string
		json string
		want string
	}{
		{"unknown protocol", `{"rules":[{"id":"r","action":"allow","protocol":"foo"}]}`, ErrUnknownProtocol},
		{"invalid cidr", `{"rules":[{"id":"r","action":"allow","protocol":"tcp","source":"nope"}]}`, ErrInvalidCIDR},
		{"inverted ports", `{"rules":[{"id":"r","action":"allow","protocol":"tcp","destination_ports":"9-1"}]}`, ErrInvalidPort},
		{"mixed families", `{"rules":[{"id":"r","action":"allow","protocol":"tcp","source":"10.0.0.0/30","destination":"2001:db8::/126"}]}`, ErrMixedAddressFamily},
		{"icmp family", `{"rules":[{"id":"r","action":"allow","protocol":"icmp","source":"2001:db8::/126"}]}`, ErrProtocolFamilyMismatch},
		{"bad action", `{"rules":[{"id":"r","action":"permit","protocol":"tcp"}]}`, ErrInvalidAction},
		{"duplicate id", `{"rules":[{"id":"r","action":"allow","protocol":"tcp"},{"id":"r","action":"deny","protocol":"udp"}]}`, ErrDuplicateRuleID},
		{"malformed json", `{bad`, ErrInvalidConfig},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, errs := Parse(strings.NewReader(tc.json))
			found := false
			for _, e := range errs {
				if e.Category == tc.want {
					found = true
				}
			}
			if !found {
				t.Fatalf("want category %s, got %+v", tc.want, errs)
			}
		})
	}
}

func TestUnknownProtocolIsNoteNotError(t *testing.T) {
	rs, errs := Parse(strings.NewReader(`{"rules":[{"id":"u","action":"allow","protocol":"99"}]}`))
	if len(errs) != 0 {
		t.Fatalf("numeric unknown protocol should be accepted with a note, got %+v", errs)
	}
	if len(rs.Rules[0].Notes) == 0 {
		t.Fatalf("expected uncertainty note")
	}
}

func TestDefaultActionValidation(t *testing.T) {
	if _, errs := Parse(strings.NewReader(`{"default_action":"maybe"}`)); len(errs) == 0 {
		t.Fatalf("invalid default action must error")
	}
	rs, errs := Parse(strings.NewReader(`{}`))
	if len(errs) != 0 {
		t.Fatalf("empty config should default to deny: %+v", errs)
	}
	if rs.DefaultAction != "deny" {
		t.Fatalf("default action should default to deny, got %q", rs.DefaultAction)
	}
}
