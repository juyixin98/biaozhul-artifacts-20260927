package config

import (
	"errors"
	"io"
	"strings"
	"testing"

	"flexhash/internal/fherr"
)

func mustParse(t *testing.T, tcase, jsonText string) *Config {
	t.Helper()
	c, err := Parse(strings.NewReader(jsonText))
	if err != nil {
		t.Fatalf("%s: unexpected parse error: %v", tcase, err)
	}
	return c
}

func expectKind(t *testing.T, tcase string, err error, want fherr.Kind) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s: expected error kind %s, got nil", tcase, want)
	}
	if got := fherr.KindOf(err); got != want {
		t.Fatalf("%s: error kind = %s, want %s (err=%v)", tcase, got, want, err)
	}
}

func TestParseValid(t *testing.T) {
	c := mustParse(t, "valid", `{
		"listen_addr":"127.0.0.1:18080",
		"sqlite_path":"/tmp/x.db",
		"bucket_count":256,
		"members":[
			{"id":"a","address":"127.0.0.1:9001","weight":2,"healthy":true},
			{"id":"b","address":"127.0.0.1:9002","weight":0,"healthy":false}
		]}`)
	if c.BucketCount != 256 || len(c.Members) != 2 {
		t.Fatalf("unexpected config: %+v", c)
	}
	sorted := c.SortedMembers()
	if sorted[0].ID != "a" || sorted[1].ID != "b" {
		t.Fatalf("SortedMembers order wrong: %+v", sorted)
	}
}

func TestParseInputErrors(t *testing.T) {
	cases := []struct {
		name string
		json string
	}{
		{"malformed_json", `{not json`},
		{"unknown_field", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,"members":[{"id":"a","address":"127.0.0.1:1","weight":1}],"bogus":1}`},
		{"missing_listen", `{"listen_addr":"","sqlite_path":"x","bucket_count":4,"members":[{"id":"a","address":"127.0.0.1:1","weight":1}]}`},
		{"bad_listen_addr", `{"listen_addr":"::not an addr::","sqlite_path":"x","bucket_count":4,"members":[{"id":"a","address":"127.0.0.1:1","weight":1}]}`},
		{"missing_sqlite", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"","bucket_count":4,"members":[{"id":"a","address":"127.0.0.1:1","weight":1}]}`},
		{"zero_buckets", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":0,"members":[{"id":"a","address":"127.0.0.1:1","weight":1}]}`},
		{"huge_buckets", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":2000000,"members":[{"id":"a","address":"127.0.0.1:1","weight":1}]}`},
		{"no_members", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,"members":[]}`},
		{"empty_id", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,"members":[{"id":"","address":"127.0.0.1:1","weight":1}]}`},
		{"dup_id", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,"members":[{"id":"a","address":"127.0.0.1:1","weight":1},{"id":"a","address":"127.0.0.1:2","weight":1}]}`},
		{"bad_address", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,"members":[{"id":"a","address":"::::","weight":1}]}`},
		{"neg_weight", `{"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,"members":[{"id":"a","address":"127.0.0.1:1","weight":-1}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Parse(strings.NewReader(tc.json))
			expectKind(t, tc.name, err, fherr.KindInput)
		})
	}
}

func TestLoadFileMissingIsInput(t *testing.T) {
	_, err := LoadFile("/nonexistent/path/flexhash-xyz.json")
	expectKind(t, "missing file", err, fherr.KindInput)
	if !errors.Is(err, io.EOF) && err == nil {
		t.Fatal("expected error")
	}
}

func TestZeroWeightMemberAccepted(t *testing.T) {
	c := mustParse(t, "zero weight ok", `{
		"listen_addr":"127.0.0.1:8080","sqlite_path":"x","bucket_count":4,
		"members":[{"id":"a","address":"127.0.0.1:1","weight":0}]}`)
	if c.Members[0].Weight != 0 {
		t.Fatalf("weight 0 must be accepted, got %d", c.Members[0].Weight)
	}
}
