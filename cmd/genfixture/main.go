// Command genfixture synthesises the offline PCAP fixtures and their
// golden expectations used by the test suite and by scripts/verify.sh.
//
// It is deliberately independent of the reassembly core: each datagram
// payload is generated BEFORE fragmentation, so the expected bytes are
// the original payload, not anything produced by code under test.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"net/netip"
	"os"
	"path/filepath"
	"time"

	"ipreasm/internal/ipv4"
	"ipreasm/internal/pcap"
)

const testTimeout = 30 * time.Second

type spec struct {
	src, dst netip.Addr
	proto    byte
	id       uint16
}

type rawFrag struct {
	s        spec
	offUnits uint16
	more     bool
	data     []byte
	ts       time.Time
}

type wholeDatagram struct {
	s    spec
	data []byte
	ts   time.Time
}

type keyJSON struct {
	Src   string `json:"src"`
	Dst   string `json:"dst"`
	Proto int    `json:"proto"`
	ID    int    `json:"id"`
}

// expectJSON is the independent golden assertion for one datagram.
type expectJSON struct {
	Key        keyJSON `json:"key"`
	Expect     string  `json:"expect"` // completed | rejected | expired
	Reason     string  `json:"reason,omitempty"`
	SHA256     string  `json:"sha256,omitempty"`
	Length     int     `json:"length,omitempty"`
	Frags      int     `json:"frags,omitempty"`
	Duplicates int     `json:"duplicate_fragments,omitempty"`
	GoldenFile string  `json:"golden_file,omitempty"`
}

type fixtureJSON struct {
	File      string       `json:"file"`
	Timeout   string       `json:"timeout,omitempty"`
	Datagrams []expectJSON `json:"datagrams"`
	Notes     string       `json:"notes,omitempty"`
}

type manifestJSON struct {
	GeneratedBy string        `json:"generated_by"`
	GeneratedAt string        `json:"generated_at"`
	Timeout     string        `json:"timeout"`
	Fixtures    []fixtureJSON `json:"fixtures"`
}

func main() {
	out := flag.String("out", "testdata", "output directory")
	flag.Parse()
	if err := generate(*out); err != nil {
		fmt.Fprintf(os.Stderr, "genfixture: %v\n", err)
		os.Exit(1)
	}
}

func generate(out string) error {
	if err := os.MkdirAll(out, 0o755); err != nil {
		return err
	}
	base := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	manifest := manifestJSON{
		GeneratedBy: "cmd/genfixture",
		GeneratedAt: base.Format(time.RFC3339),
		Timeout:     testTimeout.String(),
	}

	for _, g := range []func(string, time.Time, *manifestJSON) error{
		genBasic, genOverlap, genConflictLast, genBadLength, genTimeoutReuse,
	} {
		if err := g(out, base, &manifest); err != nil {
			return err
		}
	}

	mf, err := os.Create(filepath.Join(out, "manifest.json"))
	if err != nil {
		return err
	}
	defer mf.Close()
	enc := json.NewEncoder(mf)
	enc.SetIndent("", "  ")
	if err := enc.Encode(manifest); err != nil {
		return err
	}
	fmt.Printf("wrote %d fixtures + manifest to %s\n", len(manifest.Fixtures), out)
	return nil
}

// genBasic: three complete datagrams in in-order, last-fragment-first and
// shuffled arrival order, one exact duplicate, plus one unfragmented
// packet that must be passed through, not "reassembled".
func genBasic(out string, base time.Time, m *manifestJSON) error {
	const fname = "basic.pcap"
	var records []rawFrag
	var expected []expectJSON

	// A: in order, 3 frags 104/104/72 = 280 bytes, proto UDP (17)
	a := spec{netip.MustParseAddr("10.0.0.1"), netip.MustParseAddr("10.0.0.2"), 17, 0x1001}
	aPayload := mkPayload(0x1001, 280)
	aFrags := tag(a, split(aPayload, []int{104, 104}))
	for i := range aFrags {
		aFrags[i].ts = base.Add(time.Duration(i) * time.Millisecond)
		records = append(records, aFrags[i])
	}
	if err := writeGolden(out, a, aPayload); err != nil {
		return err
	}
	expected = append(expected, golden(a, aPayload, 3, 0))

	// B: last fragment first, reverse order, 4 frags, proto ICMP (1)
	b := spec{netip.MustParseAddr("10.0.0.3"), netip.MustParseAddr("10.0.0.4"), 1, 0x1002}
	bPayload := mkPayload(0x1002, 300)
	bFrags := tag(b, split(bPayload, []int{72, 72, 72}))
	reverseFrags(bFrags)
	for i := range bFrags {
		bFrags[i].ts = base.Add(100*time.Millisecond + time.Duration(i)*time.Millisecond)
		records = append(records, bFrags[i])
	}
	if err := writeGolden(out, b, bPayload); err != nil {
		return err
	}
	expected = append(expected, golden(b, bPayload, 4, 0))

	// C: shuffled order, one exact duplicate retransmitted first
	c := spec{netip.MustParseAddr("10.0.0.5"), netip.MustParseAddr("10.0.0.6"), 17, 0x1003}
	cPayload := mkPayload(0x1003, 256)
	cFrags := tag(c, split(cPayload, []int{64, 64, 64}))
	rng := rand.New(rand.NewSource(20260927))
	rng.Shuffle(len(cFrags), func(i, j int) { cFrags[i], cFrags[j] = cFrags[j], cFrags[i] })
	dup := cFrags[0]
	dup.ts = base.Add(200 * time.Millisecond)
	records = append(records, dup)
	for i := range cFrags {
		cFrags[i].ts = base.Add(201*time.Millisecond + time.Duration(i)*time.Millisecond)
		records = append(records, cFrags[i])
	}
	if err := writeGolden(out, c, cPayload); err != nil {
		return err
	}
	expected = append(expected, golden(c, cPayload, 4, 1))

	wholes := []wholeDatagram{
		{s: spec{netip.MustParseAddr("10.0.0.7"), netip.MustParseAddr("10.0.0.8"), 6, 0x9001},
			data: mkPayload(0x9001, 128), ts: base.Add(-10 * time.Millisecond)},
	}
	if err := writePCAP(filepath.Join(out, fname), records, wholes); err != nil {
		return err
	}
	m.Fixtures = append(m.Fixtures, fixtureJSON{
		File: fname, Datagrams: expected,
		Notes: "in-order, last-fragment-first, shuffled+duplicate, plus 1 unfragmented packet",
	})
	return nil
}

// genOverlap: fragments overlap; the whole group is rejected and the late
// fragment of the same key is dropped as poisoned.
func genOverlap(out string, base time.Time, m *manifestJSON) error {
	const fname = "overlap.pcap"
	d := spec{netip.MustParseAddr("10.0.1.1"), netip.MustParseAddr("10.0.1.2"), 17, 0x1101}
	records := []rawFrag{
		{s: d, offUnits: 0, more: true, data: mkPayload(0x1101, 160), ts: base},
		// covers [128,288): overlaps [128,160) with frag0, distinct bytes
		{s: d, offUnits: 16, more: true, data: mkPayload(0x1111, 160), ts: base.Add(time.Millisecond)},
		// late fragment: group already poisoned, dropped
		{s: d, offUnits: 36, more: false, data: mkPayload(0x1101, 80), ts: base.Add(2 * time.Millisecond)},
	}
	if err := writePCAP(filepath.Join(out, fname), records, nil); err != nil {
		return err
	}
	m.Fixtures = append(m.Fixtures, fixtureJSON{File: fname, Datagrams: []expectJSON{
		{Key: keyOf(d), Expect: "rejected", Reason: "overlap"},
	}})
	return nil
}

// genConflictLast: two distinct final fragments announcing different
// total lengths. A gap keeps the first announcement from completing the
// group, so the disagreement is observable.
func genConflictLast(out string, base time.Time, m *manifestJSON) error {
	const fname = "conflict_last.pcap"
	e := spec{netip.MustParseAddr("10.0.2.1"), netip.MustParseAddr("10.0.2.2"), 6, 0x1201}
	records := []rawFrag{
		{s: e, offUnits: 0, more: true, data: mkPayload(0x1201, 128), ts: base},
		// final frag at byte 256 announcing total 320; [128,256) missing
		{s: e, offUnits: 32, more: false, data: mkPayload(0x1210, 64), ts: base.Add(time.Millisecond)},
		// conflicting final frag at the same offset announcing total 384
		{s: e, offUnits: 32, more: false, data: mkPayload(0x1211, 128), ts: base.Add(2 * time.Millisecond)},
	}
	if err := writePCAP(filepath.Join(out, fname), records, nil); err != nil {
		return err
	}
	m.Fixtures = append(m.Fixtures, fixtureJSON{File: fname, Datagrams: []expectJSON{
		{Key: keyOf(e), Expect: "rejected", Reason: "conflicting-last"},
	}})
	return nil
}

// genBadLength: a non-final fragment whose length is not a multiple of 8.
func genBadLength(out string, base time.Time, m *manifestJSON) error {
	const fname = "badlen.pcap"
	b := spec{netip.MustParseAddr("10.0.3.1"), netip.MustParseAddr("10.0.3.2"), 17, 0x1301}
	records := []rawFrag{
		{s: b, offUnits: 0, more: true, data: mkPayload(0x1301, 120), ts: base},
		// MF=1 but length 84 is not a multiple of 8
		{s: b, offUnits: 15, more: true, data: mkPayload(0x1311, 84), ts: base.Add(time.Millisecond)},
	}
	if err := writePCAP(filepath.Join(out, fname), records, nil); err != nil {
		return err
	}
	m.Fixtures = append(m.Fixtures, fixtureJSON{File: fname, Datagrams: []expectJSON{
		{Key: keyOf(b), Expect: "rejected", Reason: "bad-length"},
	}})
	return nil
}

// genTimeoutReuse: F starts with part of an OLD payload and goes quiet
// past the timeout; G is missing its middle; then identification 0x2001
// is reused by a NEW complete datagram carrying different bytes.
func genTimeoutReuse(out string, base time.Time, m *manifestJSON) error {
	const fname = "timeout_reuse.pcap"
	f := spec{netip.MustParseAddr("10.0.4.1"), netip.MustParseAddr("10.0.4.2"), 17, 0x2001}
	g := spec{netip.MustParseAddr("10.0.4.3"), netip.MustParseAddr("10.0.4.4"), 17, 0x2002}

	oldPayload := mkPayload(0xAAAA, 160)
	newPayload := mkPayload(0xBBBB, 200)
	records := []rawFrag{
		// old F: first two fragments, final fragment never arrives
		{s: f, offUnits: 0, more: true, data: oldPayload[0:64], ts: base},
		{s: f, offUnits: 8, more: true, data: oldPayload[64:128], ts: base.Add(time.Millisecond)},
		// G: first and last but the middle never arrives
		{s: g, offUnits: 0, more: true, data: mkPayload(0xCCCC, 64), ts: base.Add(2 * time.Millisecond)},
		{s: g, offUnits: 32, more: false, data: mkPayload(0xCCCC, 32), ts: base.Add(3 * time.Millisecond)},
	}
	newFrags := tag(f, split(newPayload, []int{64, 64, 64}))
	for i := range newFrags {
		newFrags[i].ts = base.Add(testTimeout + time.Second + time.Duration(i)*time.Millisecond)
		records = append(records, newFrags[i])
	}
	if err := writePCAP(filepath.Join(out, fname), records, nil); err != nil {
		return err
	}
	if err := writeGolden(out, f, newPayload); err != nil {
		return err
	}
	m.Fixtures = append(m.Fixtures, fixtureJSON{
		File:    fname,
		Timeout: testTimeout.String(),
		Notes:   "old partial F and incomplete G expire; ID 0x2001 reused by a new complete datagram",
		Datagrams: []expectJSON{
			{Key: keyOf(f), Expect: "expired"},
			{Key: keyOf(g), Expect: "expired"},
			golden(f, newPayload, 4, 0), // then completed after reuse
		},
	})
	return nil
}

// --- helpers independent of package reasm --------------------------------

// mkPayload builds deterministic payload bytes from the datagram identity.
func mkPayload(seed uint32, n int) []byte {
	b := make([]byte, n)
	for i := range b {
		x := seed ^ uint32(i)*2654435761
		x ^= x << 13
		x ^= x >> 17
		x ^= x << 5
		b[i] = byte(x) ^ byte(i)
	}
	return b
}

// split fragments payload at the given chunk sizes (all multiples of 8);
// the remainder forms the final fragment. This is the generator's own
// fragmentation logic, not the code under test.
func split(payload []byte, sizes []int) []rawFrag {
	var out []rawFrag
	off := 0
	for _, sz := range sizes {
		if sz%8 != 0 {
			panic(fmt.Sprintf("generator bug: chunk %d not multiple of 8", sz))
		}
		if off+sz > len(payload) {
			panic("generator bug: chunk past payload end")
		}
		out = append(out, rawFrag{offUnits: uint16(off / 8), more: true, data: payload[off : off+sz]})
		off += sz
	}
	out = append(out, rawFrag{offUnits: uint16(off / 8), more: false, data: payload[off:]})
	return out
}

func tag(s spec, frags []rawFrag) []rawFrag {
	for i := range frags {
		frags[i].s = s
	}
	return frags
}

func reverseFrags(f []rawFrag) {
	for i, j := 0, len(f)-1; i < j; i, j = i+1, j-1 {
		f[i], f[j] = f[j], f[i]
	}
}

func keyOf(s spec) keyJSON {
	return keyJSON{Src: s.src.String(), Dst: s.dst.String(), Proto: int(s.proto), ID: int(s.id)}
}

// golden builds the expectation from the ORIGINAL payload (hashed and
// written to a golden file before any fragmentation).
func golden(s spec, payload []byte, frags, dups int) expectJSON {
	sum := sha256.Sum256(payload)
	return expectJSON{
		Key:        keyOf(s),
		Expect:     "completed",
		SHA256:     hex.EncodeToString(sum[:]),
		Length:     len(payload),
		Frags:      frags,
		Duplicates: dups,
		GoldenFile: goldenName(s),
	}
}

func goldenName(s spec) string { return fmt.Sprintf("golden_%04x.bin", s.id) }

func writeGolden(out string, s spec, payload []byte) error {
	return os.WriteFile(filepath.Join(out, goldenName(s)), payload, 0o644)
}

func writePCAP(path string, frags []rawFrag, wholes []wholeDatagram) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	w, err := pcap.NewWriter(f, pcap.LinkEthernet)
	if err != nil {
		return err
	}
	write := func(ts time.Time, ip []byte) error {
		eth := make([]byte, 14+len(ip))
		eth[12], eth[13] = 0x08, 0x00 // EtherType IPv4
		copy(eth[14:], ip)
		return w.WriteRecord(ts, eth)
	}
	// Merge by timestamp so the capture order equals the temporal order
	// used by each scenario.
	type item struct {
		ts time.Time
		ip []byte
	}
	var items []item
	for _, wd := range wholes {
		ip, err := ipv4.MarshalFragment(wd.s.src, wd.s.dst, wd.s.proto, wd.s.id, 0, false, 64, wd.data)
		if err != nil {
			return err
		}
		items = append(items, item{wd.ts, ip})
	}
	for _, fr := range frags {
		ip, err := ipv4.MarshalFragment(fr.s.src, fr.s.dst, fr.s.proto, fr.s.id, fr.offUnits, fr.more, 64, fr.data)
		if err != nil {
			return err
		}
		items = append(items, item{fr.ts, ip})
	}
	// stable sort preserves the deliberately chosen order at equal ts
	for i := 1; i < len(items); i++ {
		for j := i; j > 0 && items[j-1].ts.After(items[j].ts); j-- {
			items[j-1], items[j] = items[j], items[j-1]
		}
	}
	for _, it := range items {
		if err := write(it.ts, it.ip); err != nil {
			return err
		}
	}
	return nil
}
