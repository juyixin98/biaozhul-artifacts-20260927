package config

import (
	"strings"
	"testing"
	"time"
)

func TestDefaultIsValid(t *testing.T) {
	if err := Default().Validate(); err != nil {
		t.Fatalf("default config must validate: %v", err)
	}
}

func TestValidateRejects(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*Config)
		want   string
	}{
		{"bad external ip", func(c *Config) { c.ExternalIP = "999.1.1.1" }, "external_ip"},
		{"inverted tcp range", func(c *Config) { c.TCPPortMin, c.TCPPortMax = 20100, 20000 }, "min"},
		{"privileged port", func(c *Config) { c.TCPPortMin, c.TCPPortMax = 80, 90 }, "1024"},
		{"zero udp timeout", func(c *Config) { c.UDPTimeout = Duration{0} }, "udp_timeout"},
		{"negative cooldown", func(c *Config) { c.ReuseCooldown = Duration{-time.Second} }, "cooldown"},
		{"bad listen", func(c *Config) { c.HTTPListen = "no-port" }, "http_listen"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := Default()
			tc.mutate(&c)
			err := c.Validate()
			if err == nil {
				t.Fatalf("expected validation error containing %q", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not contain %q", err.Error(), tc.want)
			}
		})
	}
}

func TestDurationRoundTrip(t *testing.T) {
	var d Duration
	if err := d.UnmarshalJSON([]byte(`"90s"`)); err != nil {
		t.Fatal(err)
	}
	if d.Duration != 90*time.Second {
		t.Fatalf("parsed %v", d.Duration)
	}
	b, err := d.MarshalJSON()
	if err != nil || string(b) != `"1m30s"` {
		t.Fatalf("marshaled %s err=%v", b, err)
	}
	if err := d.UnmarshalJSON([]byte(`"nope"`)); err == nil {
		t.Fatal("expected parse error")
	}
}
