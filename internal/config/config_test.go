package config_test

import (
	"strings"
	"testing"

	"fwrule/internal/config"
)

func TestCompileErrors(t *testing.T) {
	cases := []struct {
		name string
		doc  string
		code string
	}{
		{
			"unknown protocol name",
			`{"name":"x","default_action":"deny","rules":[
			 {"id":"r","action":"allow","protocol":"tcp-x","src_cidr":"0.0.0.0/0","dst_cidr":"0.0.0.0/0"}]}`,
			"UNKNOWN_PROTOCOL",
		},
		{
			"port on non-port protocol",
			`{"name":"x","default_action":"deny","rules":[
			 {"id":"r","action":"allow","protocol":"icmp","src_cidr":"0.0.0.0/0","dst_cidr":"0.0.0.0/0","dst_port":"80"}]}`,
			"PORT_NOT_ALLOWED",
		},
		{
			"bad cidr",
			`{"name":"x","default_action":"deny","rules":[
			 {"id":"r","action":"allow","protocol":"tcp","src_cidr":"10/33","dst_cidr":"0.0.0.0/0"}]}`,
			"bad src_cidr",
		},
		{
			"bad action",
			`{"name":"x","default_action":"deny","rules":[
			 {"id":"r","action":"permit","protocol":"tcp","src_cidr":"0.0.0.0/0","dst_cidr":"0.0.0.0/0"}]}`,
			"invalid action",
		},
		{
			"missing default",
			`{"name":"x","rules":[
			 {"id":"r","action":"allow","protocol":"tcp","src_cidr":"0.0.0.0/0","dst_cidr":"0.0.0.0/0"}]}`,
			"default_action is required",
		},
		{
			"duplicate id",
			`{"name":"x","default_action":"deny","rules":[
			 {"id":"r","action":"allow","protocol":"tcp","src_cidr":"0.0.0.0/0","dst_cidr":"0.0.0.0/0"},
			 {"id":"r","action":"deny","protocol":"tcp","src_cidr":"0.0.0.0/0","dst_cidr":"0.0.0.0/0"}]}`,
			"duplicate rule id",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := config.Load([]byte(tc.doc), "case")
			if err == nil {
				t.Fatal("expected compile error")
			}
			if !strings.Contains(err.Error(), tc.code) {
				t.Fatalf("error %q does not contain %q", err.Error(), tc.code)
			}
		})
	}
}

func TestCrossFamilyIsNoteNotError(t *testing.T) {
	doc := `{"name":"x","default_action":{"ipv4":"deny","ipv6":"deny"},"rules":[
		{"id":"r","action":"allow","protocol":"tcp","src_cidr":"10.0.0.0/16","dst_cidr":"2001:db8::/64"}]}`
	pol, err := config.Load([]byte(doc), "case")
	if err != nil {
		t.Fatal(err)
	}
	if !pol.Rules[0].MatchEmpty {
		t.Fatal("cross-family rule must be flagged empty")
	}
	if len(pol.Notes) != 1 || pol.Notes[0].Code != "EMPTY_MATCH" {
		t.Fatalf("notes=%+v", pol.Notes)
	}
}
