package quantity

import "testing"

func TestParseCPU(t *testing.T) {
	cases := []struct {
		in   string
		want int
		err  bool
	}{
		{"250m", 250, false},
		{"1", 1000, false},
		{"0.25", 250, false},
		{" 500m ", 500, false},
		{"2", 2000, false},
		{"", 0, true},
		{"abc", 0, true},
		{"-1m", 0, true},
		{"m", 0, true},
	}
	for _, c := range cases {
		got, err := ParseCPU(c.in)
		if c.err {
			if err == nil {
				t.Errorf("ParseCPU(%q) expected error, got %d", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseCPU(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("ParseCPU(%q)=%d want %d", c.in, got, c.want)
		}
	}
}

func TestParseMemory(t *testing.T) {
	cases := []struct {
		in   string
		want int64
		err  bool
	}{
		{"128Mi", 128 * 1024 * 1024, false},
		{"1Gi", 1 << 30, false},
		{"512M", 512 * 1000 * 1000, false},
		{"1024", 1024, false},
		{"1Ti", 1 << 40, false},
		{"", 0, true},
		{"xGi", 0, true},
		{"-1Mi", 0, true},
	}
	for _, c := range cases {
		got, err := ParseMemory(c.in)
		if c.err {
			if err == nil {
				t.Errorf("ParseMemory(%q) expected error, got %d", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseMemory(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("ParseMemory(%q)=%d want %d", c.in, got, c.want)
		}
	}
}
