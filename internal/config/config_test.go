package config_test

import (
	"encoding/json"
	"strings"
	"testing"

	"igmpq/internal/cats"
	"igmpq/internal/config"
)

// TestDefaults: RFC 2236 defaults — QI=125, QRI=10, RV=2, LMQI=1, LMQC=RV.
// GMI = 2*125+10 = 260s, LMQT = 1*2 = 2s.
func TestDefaults(t *testing.T) {
	cfg, err := config.Parse(json.RawMessage(`{"interfaces":["eth0"]}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if cfg.QueryIntervalSec != 125 || cfg.QueryResponseIntervalSec != 10 ||
		cfg.RobustnessVariable != 2 || cfg.LastMemberQueryIntervalSec != 1 ||
		cfg.LastMemberQueryCount != 2 {
		t.Fatalf("defaults = %+v", cfg)
	}
	d := cfg.Derived()
	if d.GroupMembershipIntervalMS != 260000 {
		t.Fatalf("GMI=%d, want 260000", d.GroupMembershipIntervalMS)
	}
	if d.LastMemberQueryTimeMS != 2000 {
		t.Fatalf("LMQT=%d, want 2000", d.LastMemberQueryTimeMS)
	}
}

// TestLMQCDefaultsToRobustnessVariable, per RFC 2236 §4.
func TestLMQCDefaultsToRobustnessVariable(t *testing.T) {
	cfg, err := config.Parse(json.RawMessage(`{"interfaces":["eth0"],"robustness_variable":4}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if cfg.LastMemberQueryCount != 4 {
		t.Fatalf("LMQC=%d, want 4 (=RV)", cfg.LastMemberQueryCount)
	}
}

// TestInvalidConfigs: every broken config must fail with category
// invalid_config and name the offending field.
func TestInvalidConfigs(t *testing.T) {
	cases := []struct {
		name  string
		json  string
		field string
	}{
		{"no interfaces", `{}`, "interfaces"},
		{"empty interface name", `{"interfaces":[""]}`, "interfaces"},
		{"duplicate interface", `{"interfaces":["eth0","eth0"]}`, "interfaces"},
		{"negative query interval", `{"interfaces":["e"],"query_interval_sec":-1}`, "query_interval_sec"},
		{"negative response interval", `{"interfaces":["e"],"query_response_interval_sec":-1}`, "query_response_interval_sec"},
		{"robustness too small", `{"interfaces":["e"],"robustness_variable":1}`, "robustness_variable"},
		{"negative lmq interval", `{"interfaces":["e"],"last_member_query_interval_sec":-2}`, "last_member_query_interval_sec"},
		{"negative lmq count", `{"interfaces":["e"],"last_member_query_count":-1}`, "last_member_query_count"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := config.Parse(json.RawMessage(c.json))
			if err == nil {
				t.Fatal("expected error")
			}
			if cats.CategoryOf(err) != cats.InvalidConfig {
				t.Fatalf("category=%s, want invalid_config", cats.CategoryOf(err))
			}
			if !strings.Contains(err.Error(), c.field) {
				t.Fatalf("error %q does not name field %s", err.Error(), c.field)
			}
		})
	}
}

func TestMalformedJSON(t *testing.T) {
	_, err := config.Parse(json.RawMessage(`{not json`))
	if cats.CategoryOf(err) != cats.InvalidConfig {
		t.Fatalf("category=%s, want invalid_config", cats.CategoryOf(err))
	}
}
