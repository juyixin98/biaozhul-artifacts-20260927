package trie

import (
	"fmt"
	"math/rand"
	"strconv"
	"strings"
	"testing"
)

// TestExhaustive4Bit compares the compressed trie against a plain map over
// every prefix in a 4-bit key space, exercising all split/merge shapes.
func TestExhaustive4Bit(t *testing.T) {
	const width = bitLen(4)
	key := func(n int) []byte { return []byte{byte(n << 4)} }

	t.Run("insert-and-match", func(t *testing.T) {
		tr := New[string](width)
		// (bits, network number); network numbers are masked.
		specs := [][2]int{{0, 0}, {1, 0}, {1, 8}, {2, 12}, {3, 6}, {3, 12}, {4, 5}, {4, 9}, {2, 0}, {4, 6}}
		present := map[[2]int]bool{}
		for _, spec := range specs {
			bits, n := spec[0], spec[1]
			id := fmt.Sprintf("p%d-%d", bits, n)
			var err error
			tr, err = tr.Insert(key(n), bits, id)
			if err != nil {
				t.Fatalf("insert %d/%d: %v", n, bits, err)
			}
			present[spec] = true
		}
		if tr.Len() != len(specs) {
			t.Fatalf("Len = %d, want %d", tr.Len(), len(specs))
		}
		for addr := 0; addr < 16; addr++ {
			chain, err := tr.MatchChain(key(addr))
			if err != nil {
				t.Fatalf("match %d: %v", addr, err)
			}
			var expected []int
			for bits := 4; bits >= 0; bits-- {
				n := addr & (0xF << (4 - bits))
				if present[[2]int{bits, n}] {
					expected = append(expected, bits)
				}
			}
			if len(chain) != len(expected) {
				t.Fatalf("addr %04b chain lengths: got %v want %v", addr, lensOf(chain), expected)
			}
			for i, h := range chain {
				if h.KeyLen != expected[i] {
					t.Fatalf("addr %04b chain order: got %v want %v", addr, lensOf(chain), expected)
				}
			}
		}
	})

	t.Run("delete-roundtrip", func(t *testing.T) {
		tr := New[int](width)
		var specs [][2]int
		rng := rand.New(rand.NewSource(1))
		present := map[[2]int]bool{}
		for round := 0; round < 200; round++ {
			bits := rng.Intn(5)
			n := rng.Intn(16)
			n &= 0xF << (4 - bits)
			spec := [2]int{bits, n}
			if !present[spec] {
				var err error
				tr, err = tr.Insert(key(n), bits, round)
				if err != nil {
					t.Fatalf("insert %v: %v", spec, err)
				}
				present[spec] = true
				specs = append(specs, spec)
			}
		}
		// delete in random order; persistence means each returned trie must be
		// internally consistent and match the remaining set exactly.
		rng.Shuffle(len(specs), func(i, j int) { specs[i], specs[j] = specs[j], specs[i] })
		for _, spec := range specs {
			var removed bool
			var err error
			tr, removed, err = tr.Delete(key(spec[1]), spec[0])
			if err != nil {
				t.Fatalf("delete %v: %v", spec, err)
			}
			if !removed {
				t.Fatalf("delete %v reported absent", spec)
			}
			delete(present, spec)
			// cross-check Every and MatchChain against the reference set
			count := 0
			for s := range present {
				count++
				chain, err := tr.MatchChain(key(s[1]))
				if err != nil {
					t.Fatalf("match after deletes: %v", err)
				}
				found := false
				for _, h := range chain {
					if h.KeyLen == s[0] {
						found = true
					}
				}
				if !found {
					t.Fatalf("prefix %v vanished from MatchChain while supposedly present", s)
				}
			}
			if tr.Len() != count {
				t.Fatalf("Len %d != remaining %d after deleting %v", tr.Len(), count, spec)
			}
		}
		if tr.Len() != 0 {
			t.Fatalf("tree not empty after deleting everything: %d", tr.Len())
		}
		chain, _ := tr.MatchChain(key(7))
		if len(chain) != 0 {
			t.Fatalf("empty tree matched %d hits", len(chain))
		}
	})
}

// TestIPv4RealShape installs realistic overlapping IPv4 prefixes and checks
// the exact match chain.
func TestIPv4RealShape(t *testing.T) {
	tr := New[string](IPv4Bits)
	add := func(pfx string, v string) {
		t.Helper()
		_, p := parseIP(t, pfx)
		var err error
		tr, err = tr.Insert(p.addr, p.bits, v)
		if err != nil {
			t.Fatalf("insert %s: %v", pfx, err)
		}
	}
	add("0.0.0.0/0", "default")
	add("203.0.113.0/24", "p2p")
	add("198.51.100.0/24", "overlap")
	add("198.51.100.7/32", "host")

	_, host := parseIP(t, "198.51.100.7")
	chain, err := tr.MatchChain(host.addr)
	if err != nil {
		t.Fatal(err)
	}
	got := lensOf(chain)
	want := []int{32, 24, 0}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("chain = %v, want %v", got, want)
	}
	if chain[0].Value != "host" || chain[1].Value != "overlap" || chain[2].Value != "default" {
		t.Fatalf("values = %q,%q,%q", chain[0].Value, chain[1].Value, chain[2].Value)
	}
}

type ip struct {
	addr []byte
	bits int
}

func parseIP(t *testing.T, s string) (string, ip) {
	t.Helper()
	host := s
	bits := 0
	if i := strings.IndexByte(s, '/'); i >= 0 {
		host = s[:i]
		var err error
		if bits, err = strconv.Atoi(s[i+1:]); err != nil {
			t.Fatalf("bad prefix %q: %v", s, err)
		}
	}
	parts := splitDots(host)
	if len(parts) != 4 {
		t.Fatalf("bad ipv4 %q", s)
	}
	return s, ip{addr: []byte{byte(parts[0]), byte(parts[1]), byte(parts[2]), byte(parts[3])}, bits: bits}
}

func splitDots(s string) []int {
	var out []int
	cur := 0
	has := false
	for _, c := range s {
		if c == '.' {
			out = append(out, cur)
			cur, has = 0, false
			continue
		}
		cur = cur*10 + int(c-'0')
		has = true
	}
	if has {
		out = append(out, cur)
	}
	return out
}

func lensOf[T any](h []Hit[T]) []int {
	out := make([]int, len(h))
	for i := range h {
		out[i] = h[i].KeyLen
	}
	return out
}
