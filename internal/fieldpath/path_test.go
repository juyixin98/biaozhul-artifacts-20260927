package fieldpath

import "testing"

func TestParseStringRoundtrip(t *testing.T) {
	cases := []string{
		"replicas",
		"spec.image",
		`ingresses[name="edge-1"].host`,
		`ingresses[name="edge-1"].ports[port=8080].name`,
		`tags[^"canary"]`,
		`tags[^"a.b[]d"]`,
		`tags[^"a quote: \"q\""]`,
		`set[^42]`,
		`flags[^true]`,
	}
	for _, c := range cases {
		p, err := Parse(c)
		if err != nil {
			t.Fatalf("parse %q: %v", c, err)
		}
		if got := p.String(); got != c {
			t.Fatalf("roundtrip %q -> %q", c, got)
		}
		// text marshaling
		tb, err := p.MarshalText()
		if err != nil || string(tb) != c {
			t.Fatalf("marshal %q: %q %v", c, tb, err)
		}
		var back Path
		if err := back.UnmarshalText(tb); err != nil || back.String() != c {
			t.Fatalf("unmarshal %q: %q %v", c, back, err)
		}
	}
}

func TestParseErrors(t *testing.T) {
	bad := []string{
		`.foo`,
		`a.`,
		`a[`,
		`a[x=1`,
		`a[^1`,
		`a[b=]`,
		`a[=1]`,
		`a[^1 2]`,
	}
	for _, b := range bad {
		if _, err := Parse(b); err == nil {
			t.Fatalf("expected parse error for %q", b)
		}
	}
}

func TestPrefixAndLookup(t *testing.T) {
	p, _ := Parse(`ingresses[name="edge-1"].ports[port=8080].name`)
	if !p.HasPrefix(p) {
		t.Fatal("path should be prefix of itself")
	}
	parent, _ := Parse(`ingresses[name="edge-1"]`)
	if !p.HasPrefix(parent) {
		t.Fatal("element should be under its parent")
	}
	sibling, _ := Parse(`ingresses[name="edge-2"]`)
	if p.HasPrefix(sibling) {
		t.Fatal("different key must not be a prefix")
	}
	setPath, _ := Parse(`tags[^"canary"]`)
	tags, _ := Parse("tags")
	if !setPath.HasPrefix(tags) {
		t.Fatal("set element should be under tags")
	}

	tree := map[string]any{
		"ingresses": []any{
			map[string]any{"name": "edge-1", "host": "a"},
			map[string]any{"name": "edge-2", "host": "b"},
		},
		"tags": []any{"x", "canary"},
	}
	if _, ok := Lookup(tree, p); ok {
		t.Fatal("expected miss for a nested path that does not exist in tree")
	}
	host, _ := Parse(`ingresses[name="edge-2"].host`)
	if v, ok := Lookup(tree, host); !ok || v != "b" {
		t.Fatalf("lookup edge-2 host: %v %v", v, ok)
	}
	tok, _ := Parse(`tags[^"canary"]`)
	if v, ok := Lookup(tree, tok); !ok || v != "canary" {
		t.Fatalf("lookup set token: %v %v", v, ok)
	}
}

func TestToken(t *testing.T) {
	cases := []struct {
		v    any
		want string
	}{
		{"edge-1", `"edge-1"`},
		{float64(8080), `8080`},
		{true, `true`},
		{nil, `null`},
	}
	for _, c := range cases {
		got, err := Token(c.v)
		if err != nil || got != c.want {
			t.Fatalf("token %v = %q, %v; want %q", c.v, got, err, c.want)
		}
	}
	if _, err := Token([]any{1}); err == nil {
		t.Fatal("non-scalar token must error")
	}
}
