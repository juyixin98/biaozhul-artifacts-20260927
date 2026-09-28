package config

import (
	"strings"
	"testing"
)

func TestLoadValid(t *testing.T) {
	doc := `{
      "server": {"listen": "127.0.0.1:9090"},
      "storage": {"driver": "memory"},
      "resolution": {"max_depth": 4},
      "routes": [
        {"id":"a","prefix":"10.0.0.0/8","admin_distance":5,"next_hop":{"interface":"eth0"}}
      ]
    }`
	cfg, err := Load(strings.NewReader(doc))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Resolution.MaxDepth != 4 || cfg.Server.Listen != "127.0.0.1:9090" {
		t.Fatalf("parsed values wrong: %+v", cfg)
	}
	if len(cfg.Routes) != 1 || cfg.Routes[0].ID != "a" {
		t.Fatal("bootstrap route missing")
	}
}

func TestDefaultsApply(t *testing.T) {
	cfg, err := Load(strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Storage.Driver != "memory" || cfg.Resolution.MaxDepth != 8 || cfg.Server.Listen == "" {
		t.Fatalf("defaults not applied: %+v", cfg)
	}
}

func TestRejectCases(t *testing.T) {
	bad := []struct {
		name string
		doc  string
		frag string
	}{
		{"unknown key", `{"unknown":1}`, "unknown"},
		{"bad listen", `{"server":{"listen":"nope"}}`, "listen"},
		{"bad driver", `{"storage":{"driver":"redis"}}`, "driver"},
		{"sqlite without dsn", `{"storage":{"driver":"sqlite"}}`, "dsn"},
		{"depth too big", `{"resolution":{"max_depth":99}}`, "max_depth"},
		{"duplicate ids", `{"routes":[
			{"id":"a","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{"interface":"e"}},
			{"id":"a","prefix":"10.1.0.0/16","admin_distance":1,"next_hop":{"interface":"e"}}]}`, "duplicates"},
		{"invalid route", `{"routes":[{"id":"a","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{}}]}`, "routes[0]"},
		{"cross family", `{"routes":[{"id":"a","prefix":"10.0.0.0/8","admin_distance":1,"next_hop":{"addr":"2001:db8::1"}}]}`, "routes[0]"},
		{"host bits kept in canonical", `{"routes":[{"id":"a","prefix":"10.0.0.5/8","admin_distance":1,"next_hop":{"interface":"e"}}]}`, ""},
	}
	for _, b := range bad {
		t.Run(b.name, func(t *testing.T) {
			_, err := Load(strings.NewReader(b.doc))
			if b.frag == "" {
				// The last case is actually accepted but canonicalized.
				if err != nil {
					t.Fatalf("expected normalization, got error %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), b.frag) {
				t.Fatalf("want error containing %q, got %v", b.frag, err)
			}
		})
	}
}
