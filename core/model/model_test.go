package model

import "testing"

func TestDefaultConfigIsValid(t *testing.T) {
	if err := DefaultConfig().Validate(); err != nil {
		t.Fatalf("default config invalid: %v", err)
	}
}

func TestValidateSample(t *testing.T) {
	cases := []struct {
		name string
		s    LoadSample
		at   int64
		ok   bool
	}{
		{"good", LoadSample{InstanceID: "i1", Load: 0, ReportedAt: 100}, 100, true},
		{"empty id", LoadSample{InstanceID: "", Load: 0, ReportedAt: 100}, 100, false},
		{"negative load", LoadSample{InstanceID: "i1", Load: -0.1, ReportedAt: 100}, 100, false},
		{"zero ts", LoadSample{InstanceID: "i1", Load: 0, ReportedAt: 0}, 100, false},
		{"future ts", LoadSample{InstanceID: "i1", Load: 0, ReportedAt: 101}, 100, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateSample(tc.s, tc.at)
			if tc.ok && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !tc.ok && err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestInvalidConfigVariants(t *testing.T) {
	mut := func(fn func(*Config)) Config {
		c := DefaultConfig()
		fn(&c)
		return c
	}
	bad := []Config{
		mut(func(c *Config) { c.TargetLoadPerInstance = 0 }),
		mut(func(c *Config) { c.MaxScaleUpFactor = 0.5 }),
		mut(func(c *Config) { c.ScaleDownStableWindow = 0 }),
		mut(func(c *Config) { c.StaleSkew = 0 }),
		mut(func(c *Config) { c.Tolerance = 0 }),
		mut(func(c *Config) { c.Tolerance = 1 }),
		mut(func(c *Config) { c.MinFreshFraction = 0 }),
		mut(func(c *Config) { c.MaxReplicas = -1 }),
		mut(func(c *Config) { c.BootstrapReplicas = 0 }),
		mut(func(c *Config) { c.BootstrapReplicas = 100 }),
	}
	for i, c := range bad {
		if err := c.Validate(); err == nil {
			t.Fatalf("case %d expected invalid config", i)
		}
	}
}
