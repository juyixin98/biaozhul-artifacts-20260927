package config_test

import (
	"strings"
	"testing"
	"time"

	"dhcp4lab/internal/config"
)

func TestDefaultIsValid(t *testing.T) {
	if err := config.Default().Validate(); err != nil {
		t.Fatalf("default config invalid: %v", err)
	}
}

func TestParseAppliesDefaults(t *testing.T) {
	cfg, err := config.Parse([]byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ListenUDP != "127.0.0.1:10067" {
		t.Fatalf("listen default = %q", cfg.ListenUDP)
	}
	if cfg.LeaseTime.Duration != 2*time.Minute {
		t.Fatalf("lease default = %s", cfg.LeaseTime)
	}
}

func TestDurationForms(t *testing.T) {
	cfg, err := config.Parse([]byte(`{"lease_time":"5m","offer_ttl":"10s"}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.LeaseTime.Duration != 5*time.Minute {
		t.Fatalf("string duration = %s", cfg.LeaseTime)
	}

	cfg2, err := config.Parse([]byte(`{"lease_time":90,"offer_ttl":10}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg2.LeaseTime.Duration != 90*time.Second {
		t.Fatalf("numeric duration = %s", cfg2.LeaseTime)
	}
}

func TestRejectsNonLoopback(t *testing.T) {
	raw := []byte(`{"listen_udp":"0.0.0.0:67","admin_http":"127.0.0.1:18067"}`)
	if _, err := config.Parse(raw); err == nil ||
		!strings.Contains(err.Error(), "non-loopback") {
		t.Fatalf("want non-loopback refusal, got %v", err)
	}
}

func TestRejectsPrivilegedPort(t *testing.T) {
	raw := []byte(`{"listen_udp":"127.0.0.1:67","admin_http":"127.0.0.1:18067"}`)
	_, err := config.Parse(raw)
	if err == nil || !strings.Contains(err.Error(), "privileged") {
		t.Fatalf("want privileged port refusal, got %v", err)
	}
}

func TestRejectsPoolOutsideNetwork(t *testing.T) {
	raw := []byte(`{"pool_start":"10.0.0.2","pool_end":"10.0.0.9"}`)
	_, err := config.Parse(raw)
	if err == nil || !strings.Contains(err.Error(), "inside network") {
		t.Fatalf("want pool containment error, got %v", err)
	}
}

func TestRejectsServerIDInPool(t *testing.T) {
	raw := []byte(`{"server_id":"192.0.2.20"}`)
	_, err := config.Parse(raw)
	if err == nil || !strings.Contains(err.Error(), "allocatable pool") {
		t.Fatalf("want server-in-pool error, got %v", err)
	}
}

func TestRejectsOfferLongerThanLease(t *testing.T) {
	raw := []byte(`{"offer_ttl":"5m","lease_time":"1m"}`)
	_, err := config.Parse(raw)
	if err == nil || !strings.Contains(err.Error(), "offer_ttl") {
		t.Fatalf("want ttl ordering error, got %v", err)
	}
}

func TestRejectsUnknownField(t *testing.T) {
	if _, err := config.Parse([]byte(`{"bogus":1}`)); err == nil {
		t.Fatal("unknown field accepted")
	}
}
