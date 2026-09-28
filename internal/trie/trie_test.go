package trie

import (
	"bytes"
	"fmt"
	"math/rand"
	"testing"
)

// naiveModel 是测试专用的朴素参照：全量 map + 线性 LPM，
// 与被测前缀树无关。
type modelKey struct {
	bits int
	hex  string // 与地址等宽的十六进制串
}

type naiveModel[V comparable] struct {
	width int // 地址字节数
	data  map[modelKey]V
}

func pkey(b []byte, bits int) modelKey {
	return modelKey{bits: bits, hex: fmt.Sprintf("%x", b)}
}

func (m *naiveModel[V]) put(addr []byte, bits int, v V) {
	key := make([]byte, m.width)
	copy(key, addr)
	m.data[pkey(key, bits)] = v
}

func (m *naiveModel[V]) del(addr []byte, bits int) bool {
	key := make([]byte, m.width)
	copy(key, addr)
	k := pkey(key, bits)
	if _, ok := m.data[k]; !ok {
		return false
	}
	delete(m.data, k)
	return true
}

func (m *naiveModel[V]) get(addr []byte, bits int) (V, bool) {
	key := make([]byte, m.width)
	copy(key, addr)
	v, ok := m.data[pkey(key, bits)]
	return v, ok
}

func matchPrefix(addr, pfx []byte, bits int) bool {
	for i := 0; i < bits; i++ {
		if bitAt(addr, i) != bitAt(pfx, i) {
			return false
		}
	}
	return true
}

// lpm 朴素最长前缀：线性扫描全部前缀选最深匹配。
func (m *naiveModel[V]) lpm(addr []byte) (V, bool) {
	var zero V
	best := -1
	val := zero
	for _, e := range m.entries() {
		if matchPrefix(addr, e.pfx, e.bits) && e.bits > best {
			best, val = e.bits, e.val
		}
	}
	return val, best >= 0
}

type entry[V any] struct {
	pfx  []byte
	bits int
	val  V
}

func (m *naiveModel[V]) entries() []entry[V] {
	out := make([]entry[V], 0, len(m.data))
	for k, v := range m.data {
		raw, err := decodeHex(k.hex)
		if err != nil {
			panic(err)
		}
		out = append(out, entry[V]{pfx: raw, bits: k.bits, val: v})
	}
	return out
}

func decodeHex(s string) ([]byte, error) {
	out := make([]byte, len(s)/2)
	for i := 0; i < len(out); i++ {
		var b byte
		for j := 0; j < 2; j++ {
			c := s[i*2+j]
			var n byte
			switch {
			case c >= '0' && c <= '9':
				n = c - '0'
			case c >= 'a' && c <= 'f':
				n = c - 'a' + 10
			default:
				return nil, fmt.Errorf("bad hex")
			}
			b = b<<4 | n
		}
		out[i] = b
	}
	return out, nil
}

func TestPutGetDeleteExact(t *testing.T) {
	tr := New[string]()
	a := []byte{10, 1, 2, 0}
	tr.Put(a, 24, "net")
	if v, ok := tr.Get(a, 24); !ok || v != "net" {
		t.Fatalf("get exact = %q,%v", v, ok)
	}
	// 主机位不同但同 /24 应取到同一条。
	if v, ok := tr.Get([]byte{10, 1, 2, 99}, 24); !ok || v != "net" {
		t.Fatalf("get same prefix different host bits failed")
	}
	// 更具体前缀尚不存在。
	if _, ok := tr.Get([]byte{10, 1, 2, 3}, 32); ok {
		t.Fatalf("unexpected /32")
	}
	if !tr.Delete(a, 24) {
		t.Fatalf("delete failed")
	}
	if _, ok := tr.Get(a, 24); ok {
		t.Fatalf("still present after delete")
	}
	if tr.Delete(a, 24) {
		t.Fatalf("second delete reported removed")
	}
}

func TestDefaultRouteLPM(t *testing.T) {
	tr := New[string]()
	tr.Put([]byte{0, 0, 0, 0}, 0, "default")
	tr.Put([]byte{10, 0, 0, 0}, 8, "ten")
	tr.Put([]byte{10, 2, 0, 0}, 16, "ten-two")

	cases := []struct {
		addr []byte
		want string
	}{
		{[]byte{8, 8, 8, 8}, "default"},
		{[]byte{10, 1, 1, 1}, "ten"},
		{[]byte{10, 2, 3, 4}, "ten-two"},
		{[]byte{10, 2, 0, 0}, "ten-two"},
	}
	for _, c := range cases {
		got, ok := tr.LongestPrefix(c.addr)
		if !ok || got != c.want {
			t.Fatalf("LPM %v = %q,%v want %q", c.addr, got, ok, c.want)
		}
	}
}

func TestRandomOpsAgainstNaive(t *testing.T) {
	for _, width := range []int{4, 16} {
		width := width
		t.Run(fmt.Sprintf("width=%d", width), func(t *testing.T) {
			rng := rand.New(rand.NewSource(int64(width * 7919)))
			tr := New[int]()
			ref := &naiveModel[int]{width: width, data: map[modelKey]int{}}
			key := func(bits int) []byte {
				b := make([]byte, width)
				rng.Read(b)
				if bits < width*8 {
					// 清零主机位，模拟掩码键。
					for i := bits; i < width*8; i++ {
						b[i/8] &^= 1 << (7 - uint(i%8))
					}
				}
				return b
			}
			for iter := 0; iter < 4000; iter++ {
				bits := rng.Intn(width*8 + 1)
				k := key(bits)
				switch rng.Intn(10) {
				case 0, 1, 2, 3, 4, 5: // put
					v := rng.Intn(100000)
					tr.Put(k, bits, v)
					ref.put(k, bits, v)
				case 6: // delete
					g1 := tr.Delete(k, bits)
					g2 := ref.del(k, bits)
					if g1 != g2 {
						t.Fatalf("iter %d delete mismatch trie=%v naive=%v", iter, g1, g2)
					}
				default: // get
					v1, ok1 := tr.Get(k, bits)
					v2, ok2 := ref.get(k, bits)
					if ok1 != ok2 || (ok1 && v1 != v2) {
						t.Fatalf("iter %d get mismatch (%v,%d) vs (%v,%d)", iter, ok1, v1, ok2, v2)
					}
				}

				// 每轮都对随机地址做 LPM 对照。
				addr := make([]byte, width)
				rng.Read(addr)
				v1, ok1 := tr.LongestPrefix(addr)
				v2, ok2 := ref.lpm(addr)
				if ok1 != ok2 || (ok1 && v1 != v2) {
					t.Fatalf("iter %d LPM mismatch addr=%x: (%v,%d) vs (%v,%d)",
						iter, addr, ok1, v1, ok2, v2)
				}
			}
			if tr.Count() != len(ref.data) {
				t.Fatalf("count %d vs naive %d", tr.Count(), len(ref.data))
			}
		})
	}
}

func TestDeleteRecompresses(t *testing.T) {
	tr := New[string]()
	tr.Put([]byte{10, 0, 0, 0}, 8, "a")
	tr.Put([]byte{10, 1, 0, 0}, 16, "b")
	tr.Put([]byte{10, 1, 2, 0}, 24, "c")

	tr.Delete([]byte{10, 1, 2, 0}, 24)
	if v, ok := tr.LongestPrefix([]byte{10, 1, 2, 3}); !ok || v != "b" {
		t.Fatalf("after delete /24 expected /16, got %q,%v", v, ok)
	}
	tr.Delete([]byte{10, 1, 0, 0}, 16)
	if v, ok := tr.LongestPrefix([]byte{10, 1, 2, 3}); !ok || v != "a" {
		t.Fatalf("after delete /16 expected /8, got %q,%v", v, ok)
	}
	if tr.Count() != 1 {
		t.Fatalf("expected 1 node, got %d", tr.Count())
	}
	// 根应已被压缩为单段（skip=8，无子节点）。
	if tr.root.skip != 8 || tr.root.children[0] != nil || tr.root.children[1] != nil {
		t.Fatalf("root not recompressed: %+v", tr.root)
	}
}

func TestSnapshotIsolation(t *testing.T) {
	tr := New[string]()
	tr.Put([]byte{1, 2, 3, 0}, 24, "v1")
	snap := tr.Snapshot()
	tr.Put([]byte{1, 2, 3, 0}, 24, "v2")
	tr.Put([]byte{5, 5, 5, 5}, 32, "host")

	if v, _ := snap.Get([]byte{1, 2, 3, 0}, 24); v != "v1" {
		t.Fatalf("snapshot mutated: %q", v)
	}
	if _, ok := snap.Get([]byte{5, 5, 5, 5}, 32); ok {
		t.Fatalf("snapshot saw later insertion")
	}
	if v, _ := tr.LongestPrefix([]byte{1, 2, 3, 9}); v != "v2" {
		t.Fatalf("live tree did not see update: %q", v)
	}
}

func TestIPv6FullWidth(t *testing.T) {
	tr := New[string]()
	// 2001:db8::/32 与 2001:db8:1::/48、默认 ::/0
	tr.Put(bytes.Repeat([]byte{0}, 16), 0, "v6default")
	p32 := []byte{0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
	tr.Put(p32, 32, "doc")
	p48 := []byte{0x20, 0x01, 0x0d, 0xb8, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
	tr.Put(p48, 48, "doc-1")

	if v, ok := tr.LongestPrefix([]byte{0x20, 0x01, 0x0d, 0xb8, 9, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1}); !ok || v != "doc" {
		t.Fatalf("v6 /32 LPM got %q,%v", v, ok)
	}
	if v, ok := tr.LongestPrefix(p48); !ok || v != "doc-1" {
		t.Fatalf("v6 /48 LPM got %q,%v", v, ok)
	}
	if v, ok := tr.LongestPrefix([]byte{0x26, 0x06, 0x47, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1}); !ok || v != "v6default" {
		t.Fatalf("v6 default got %q,%v", v, ok)
	}
}

func TestWalkOrderAndCount(t *testing.T) {
	tr := New[int]()
	tr.Put([]byte{10, 0, 0, 0}, 8, 10)
	tr.Put([]byte{192, 168, 0, 0}, 16, 192)
	tr.Put([]byte{10, 0, 0, 0}, 8, 11) // 覆盖值，不新增节点
	got := []int{}
	tr.Walk(func(v int) bool { got = append(got, v); return true })
	if len(got) != 2 {
		t.Fatalf("walk saw %d values, want 2", len(got))
	}
}
