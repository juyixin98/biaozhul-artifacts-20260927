// Package fixture builds synthetic captures and their independent golden
// answers on top of internal/oracle. Every builder returns both the packet
// list (in capture order, which may differ from sequence order) and a
// FixtureSpec naming exactly which bytes are missing or contradictory.
//
// The builders know nothing about internal/reassembly: tests convert
// packets with tcpmodel and compare the engine's output against the spec.
package fixture

import (
	"encoding/hex"
	"fmt"

	"tcpreasm/internal/oracle"
)

// FixtureSpec is the independently derived golden answer.
type FixtureSpec struct {
	Name             string `json:"name"`
	Description      string `json:"description"`
	MissingHandshake bool   `json:"missing_handshake"`

	C2SStreamHex string `json:"c2s_stream_hex"`
	S2CStreamHex string `json:"s2c_stream_hex"`
	// OpenGaps are ranges expected to remain unevidenced at the end,
	// expressed as stream offsets per direction.
	OpenGaps []GapSpec `json:"open_gaps"`
	// Conflicts are expected contradictory byte ranges.
	Conflicts []ConflictSpec `json:"conflicts"`
	// Generations is the expected handshake generation count.
	Generations int `json:"generations"`
	// InjectedReuse indicates whether the capture contains two handshakes.
	InjectedReuse bool `json:"injected_reuse"`
	// Wrap indicates the c2s stream crosses the 2^32 sequence boundary.
	Wrap bool `json:"wrap"`
}

// GapSpec names one open missing interval.
type GapSpec struct {
	Direction string `json:"direction"`
	StartOff  uint64 `json:"start_off"`
	EndOff    uint64 `json:"end_off"`
}

// ConflictSpec names one injected contradiction.
type ConflictSpec struct {
	Direction string `json:"direction"`
	StartOff  uint64 `json:"start_off"`
	EndOff    uint64 `json:"end_off"`
	// OriginalSHA is the hash of the bytes the sender really wrote.
	OriginalSHA string `json:"original_sha256"`
	// InjectedSHA is the hash of the contradictory retransmission.
	InjectedSHA string `json:"injected_sha256"`
	// Category is the expected diag category under the fixture's policy.
	Category string `json:"category"`
}

// Built is a constructed capture plus its answer.
type Built struct {
	Spec    FixtureSpec
	Flow    oracle.Flow
	Packets []oracle.Packet
}

// builder holds working state while a fixture is assembled.
type builder struct {
	flow   oracle.Flow
	sender *oracle.Sender
	pkts   []oracle.Packet
	order  int64
}

func newBuilder(clientPort, serverPort uint16, isnC2S, isnS2C uint32) *builder {
	f := oracle.NewFlow(clientPort, serverPort, isnC2S, isnS2C)
	return &builder{flow: f, sender: oracle.NewSender(f)}
}

func (b *builder) add(p oracle.Packet) {
	b.order++
	if p.Order == 0 {
		p.Order = b.order
	} else if p.Order > b.order {
		b.order = p.Order
	}
	b.pkts = append(b.pkts, p)
}

func (b *builder) addAll(ps []oracle.Packet) {
	for _, p := range ps {
		b.add(p)
	}
}

// reorder permutes the packets whose record ids are in id set: the packets
// keep their positions in the capture but the selected identities arrive in
// reverse order (a clean "these packets were reordered" transformation).
func (b *builder) reorder(ids ...string) {
	want := map[string]bool{}
	for _, id := range ids {
		want[id] = true
	}
	var chosen []oracle.Packet
	for _, p := range b.pkts {
		if want[p.RecordID] {
			chosen = append(chosen, p)
		}
	}
	for i, j := 0, len(chosen)-1; i < j; i, j = i+1, j-1 {
		chosen[i], chosen[j] = chosen[j], chosen[i]
	}
	k := 0
	for i := range b.pkts {
		if want[b.pkts[i].RecordID] {
			b.pkts[i] = chosen[k]
			k++
		}
	}
	b.reassignOrder()
}

func (b *builder) reassignOrder() {
	for i := range b.pkts {
		b.pkts[i].Order = int64(i) + 1
	}
}

// drop removes record ids from the capture (simulating loss). It returns
// the removed packets so callers can re-introduce altered copies later.
func (b *builder) drop(ids ...string) []oracle.Packet {
	gone := map[string]bool{}
	for _, id := range ids {
		gone[id] = true
	}
	var dropped []oracle.Packet
	var out []oracle.Packet
	for _, p := range b.pkts {
		if gone[p.RecordID] {
			dropped = append(dropped, p)
			continue
		}
		out = append(out, p)
	}
	b.pkts = out
	b.reassignOrder()
	return dropped
}

// find returns a packet by record id, searching the current capture.
func (b *builder) find(id string) (oracle.Packet, bool) {
	for _, p := range b.pkts {
		if p.RecordID == id {
			return p, true
		}
	}
	return oracle.Packet{}, false
}

// duplicate appends an identical copy of an existing packet at the end.
func (b *builder) duplicate(id string, newID string) {
	if p, ok := b.find(id); ok {
		cp := p
		cp.RecordID = newID
		b.add(cp)
		return
	}
	panic("fixture: duplicate unknown packet " + id)
}

// injectCorruption appends a mutated copy of id (looked up anywhere).
func (b *builder) injectCorruption(src oracle.Packet, newID string, mutate func(seq uint32, payload []byte) []byte) {
	cp := src
	cp.RecordID = newID
	cp.Payload = mutate(src.Seq, append([]byte(nil), src.Payload...))
	b.add(cp)
}

// appendPacket adds any packet directly.
func (b *builder) appendPacket(p oracle.Packet) { b.add(p) }

// corruptMutator flips each byte at the given within-segment offsets.
func corruptMutator(offsets ...int) func(seq uint32, p []byte) []byte {
	return func(_ uint32, p []byte) []byte {
		for _, o := range offsets {
			if o >= 0 && o < len(p) {
				p[o] ^= 0xFF
			}
		}
		return p
	}
}

// ---- named fixtures ---------------------------------------------------------------

// InOrder builds the trivial baseline: handshake + both directions in order.
func InOrder() Built {
	b := newBuilder(40001, 8080, 1000, 5000)
	c2s := oracle.RangeStream(40, 0)
	s2c := oracle.RangeStream(30, 100)
	b.addAll(oracle.HandshakePackets(b.flow))
	b.addAll(b.sender.DataPackets(false, c2s, []int{10, 10, 10, 10}, "c", true))
	b.addAll(b.sender.DataPackets(true, s2c, []int{15, 15}, "s", true))
	return Built{
		Spec: FixtureSpec{
			Name: "in_order", Description: "clean handshake and in-order bidirectional streams",
			C2SStreamHex: hex.EncodeToString(c2s), S2CStreamHex: hex.EncodeToString(s2c),
			Generations: 1,
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// OutOfOrder drops nothing but reorders data packets; final stream must be
// identical and contain no open gap.
func OutOfOrder() Built {
	b := newBuilder(40002, 8080, 2000, 6000)
	c2s := oracle.RangeStream(50, 10)
	s2c := oracle.RangeStream(25, 70)
	b.addAll(handshake(b))
	d := b.sender.DataPackets(false, c2s, []int{10, 10, 10, 10, 10}, "oo", false)
	b.addAll(d)
	b.addAll(b.sender.DataPackets(true, s2c, []int{25}, "os", true))
	// Reverse the five c2s data packets (each 10 bytes).
	b.reorder("oo-1", "oo-2", "oo-3", "oo-4", "oo-5")
	// Close c2s after the reordering.
	b.add(b.sender.FINPacket(false, "oo-fin"))
	return Built{
		Spec: FixtureSpec{
			Name: "out_of_order", Description: "data packets arrive fully reversed",
			C2SStreamHex: hex.EncodeToString(c2s), S2CStreamHex: hex.EncodeToString(s2c),
			Generations: 1,
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// Retransmission sends the stream, then identical copies of two segments.
func Retransmission() Built {
	b := newBuilder(40003, 8080, 3000, 7000)
	c2s := oracle.RangeStream(45, 20)
	b.addAll(handshake(b))
	b.addAll(b.sender.DataPackets(false, c2s, []int{15, 15, 15}, "rt", true))
	b.duplicate("rt-1", "rt-1-rex")
	b.duplicate("rt-3", "rt-3-rex")
	return Built{
		Spec: FixtureSpec{
			Name:         "retransmission",
			Description:  "identical retransmissions of already delivered/buffered segments",
			C2SStreamHex: hex.EncodeToString(c2s), Generations: 1,
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// ConflictingRetransmission delivers the stream then contradicts a delivered
// byte: bytes already delivered are immutable; the contradiction is an
// UNDECIDABLE conflict ledger entry.
func ConflictingRetransmission() Built {
	b := newBuilder(40004, 8080, 4000, 8000)
	c2s := oracle.RangeStream(30, 30)
	b.addAll(handshake(b))
	b.addAll(b.sender.DataPackets(false, c2s, []int{10, 10, 10}, "cx", true))
	// Re-send packet 2 (offsets 10..19) with byte 0 flipped.
	b.injectCorruption(mustFind(b.pkts, "cx-2"), "cx-2-bad", corruptMutator(0))
	bad := oracle.RangeStream(10, 40)
	bad[0] ^= 0xFF
	original := c2s[10:20]
	return Built{
		Spec: FixtureSpec{
			Name:         "conflicting_retransmission",
			Description:  "late retransmission contradicts already-delivered bytes",
			C2SStreamHex: hex.EncodeToString(c2s), Generations: 1,
			Conflicts: []ConflictSpec{{
				Direction: "c2s", StartOff: 10, EndOff: 11,
				OriginalSHA: oracle.Hash(original[:1]),
				InjectedSHA: oracle.Hash(bad[:1]),
				Category:    "UNDECIDABLE_CONFLICT_AGAINST_DELIVERED",
			}},
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// OverlapConflict constructs the textbook overlapping-segment case while a
// run is still buffered behind a hole:
//
//	stream offsets:   0..9        10..29
//	incumbent segment  ov-2       = AAAAAAAAAA (offsets 10..19)
//	newcomer segment  ov-cover    = XXXXAAAAAAA (offsets 16..25), overlapping
//	                            offsets 16..19 with contradictory X bytes
//
// Both segments arrive before byte 10 exists, so neither is delivered.
// Policies therefore produce distinct, asserted outcomes:
//
//   - first-wins : bytes 16..19 = A (incumbent); final stream is the original
//   - last-wins  : bytes 16..19 = X (newcomer); final stream deviates there
//   - quarantine : bytes 16..19 held; delivery stops at 16, conflict open
func OverlapConflict() Built {
	b := newBuilder(40005, 8080, 5000, 9000)
	b.addAll(handshake(b))

	const firstData = uint32(5000 + 1) // seq of stream offset 0
	original := oracle.RangeStream(30, 50)
	// Incumbent at 10..19: bytes 10..15 match the real stream, bytes 16..19
	// are 'A' — only the last four are later contradicted by the newcomer.
	inc := append([]byte(nil), original[10:16]...)
	inc = append(inc, 'A', 'A', 'A', 'A')
	// Newcomer at 16..25: contradicts bytes 16..19 with 'X', agrees on 20..25.
	cover := append([]byte{'X', 'X', 'X', 'X'}, original[20:26]...)
	// Incumbent ov-2 buffered at offsets 10..19.
	b.appendPacket(b.sender.Segment("ov-2", false, firstData+10, inc, oracle.KindData))
	// Newcomer ov-cover at offsets 16..25.
	b.appendPacket(b.sender.Segment("ov-cover", false, firstData+16, cover, oracle.KindData))
	// Fill offsets 0..9.
	b.appendPacket(b.sender.Segment("ov-1", false, firstData, original[:10], oracle.KindData))
	// Fill offsets 26..29 and close (20..25 already supplied by newcomer).
	b.appendPacket(b.sender.Segment("ov-3", false, firstData+26, original[26:], oracle.KindDataFIN))

	// The 4 contradictory bytes are offsets 16..19.
	originalOverlap := original[16:20]
	injectedOverlap := cover[0:4]
	return Built{
		Spec: FixtureSpec{
			Name:         "overlap_conflict",
			Description:  "overlapping segments disagree on buffered bytes 16..19",
			C2SStreamHex: hex.EncodeToString(original), Generations: 1,
			Conflicts: []ConflictSpec{{
				Direction: "c2s", StartOff: 16, EndOff: 20,
				OriginalSHA: oracle.Hash(originalOverlap),
				InjectedSHA: oracle.Hash(injectedOverlap),
				Category:    "POLICY_DEPENDENT",
			}},
		},
		Flow:    b.flow,
		Packets: b.pkts,
	}
}

func mustFind(ps []oracle.Packet, id string) oracle.Packet {
	for _, p := range ps {
		if p.RecordID == id {
			return p
		}
	}
	panic("fixture: missing packet " + id)
}

// MissingSegments removes one middle packet from each direction; only the
// contiguous prefix is delivered and precise open gaps remain.
func MissingSegments() Built {
	b := newBuilder(40006, 8080, 6000, 11000)
	c2s := oracle.RangeStream(60, 60)
	s2c := oracle.RangeStream(45, 90)
	b.addAll(handshake(b))
	b.addAll(b.sender.DataPackets(false, c2s, []int{15, 15, 15, 15}, "ms", false))
	b.addAll(b.sender.DataPackets(true, s2c, []int{15, 15, 15}, "sm", false))
	b.drop("ms-3") // c2s offsets 30..44
	b.drop("sm-2") // s2c offsets 15..29
	b.add(b.sender.FINPacket(false, "ms-fin"))
	b.add(withS2C(b.sender.FINPacket(true, "sm-fin")))
	return Built{
		Spec: FixtureSpec{
			Name:         "missing_segments",
			Description:  "one interior segment lost per direction",
			C2SStreamHex: hex.EncodeToString(c2s), S2CStreamHex: hex.EncodeToString(s2c),
			Generations: 1,
			OpenGaps: []GapSpec{
				{Direction: "c2s", StartOff: 30, EndOff: 45},
				{Direction: "s2c", StartOff: 15, EndOff: 30},
			},
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// WrapBoundary places the c2s ISN so that a 300-byte stream crosses the
// 2^32 boundary; the stream still arrives in order and must reassemble
// byte-for-byte.
func WrapBoundary() Built {
	isn := uint32(0xFFFFFFFF - 99) // SYN at -100; first data at -99
	b := newBuilder(40007, 8080, isn, 12345)
	c2s := oracle.RangeStream(300, 210)
	b.addAll(handshake(b))
	// Segment across the boundary explicitly.
	b.addAll(b.sender.DataPackets(false, c2s, []int{60, 60, 60, 60, 60}, "wb", true))
	return Built{
		Spec: FixtureSpec{
			Name:         "wrap_boundary",
			Description:  "c2s data crosses the 32-bit sequence wrap point",
			C2SStreamHex: hex.EncodeToString(c2s), Generations: 1, Wrap: true,
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// WrapBoundaryGap loses a segment straddling the wrap boundary; the gap
// must be reported at the correct *stream* offsets.
func WrapBoundaryGap() Built {
	isn := uint32(0xFFFFFFFF - 49) // first data at -48
	b := newBuilder(40008, 8080, isn, 22222)
	c2s := oracle.RangeStream(120, 30)
	b.addAll(handshake(b))
	b.addAll(b.sender.DataPackets(false, c2s, []int{30, 30, 30, 30}, "wg", true))
	b.drop("wg-2") // stream offsets 30..59 straddle the 2^32 boundary
	return Built{
		Spec: FixtureSpec{
			Name:         "wrap_boundary_gap",
			Description:  "lost segment straddles the sequence wrap point",
			C2SStreamHex: hex.EncodeToString(c2s), Generations: 1, Wrap: true,
			OpenGaps: []GapSpec{{Direction: "c2s", StartOff: 30, EndOff: 60}},
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// HalfClose closes c2s (FIN) while s2c continues; afterwards a stray data
// packet beyond the FIN must be rejected with DATA_AFTER_FIN.
func HalfClose() Built {
	b := newBuilder(40009, 8080, 7000, 13000)
	c2s := oracle.RangeStream(20, 0)
	s2c := oracle.RangeStream(60, 40)
	b.addAll(handshake(b))
	b.addAll(b.sender.DataPackets(false, c2s, []int{20}, "hc", true)) // closes c2s
	b.addAll(b.sender.DataPackets(true, s2c, []int{20, 20, 20}, "hs2", true))
	// Stray c2s data after FIN: seq == fin_nxt (one past FIN).
	stray := oracle.Packet{
		RecordID: "hc-stray", Kind: oracle.KindData, FromS2C: false,
		Seq: b.flow.ISNC2S + 1 + 20 + 1, HasAck: true,
		Payload: []byte("XX"),
	}
	b.add(stray)
	return Built{
		Spec: FixtureSpec{
			Name:         "half_close",
			Description:  "c2s half-closes then a stray data segment must be rejected",
			C2SStreamHex: hex.EncodeToString(c2s), S2CStreamHex: hex.EncodeToString(s2c),
			Generations: 1,
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// MissingHandshake starts with data and never shows a SYN.
func MissingHandshake() Built {
	b := newBuilder(40010, 8080, 0xDEADBEEF, 0xCAFEBABE)
	c2s := oracle.RangeStream(40, 10)
	// Data packets with a plausible seq but no handshake packets at all.
	d := b.sender.DataPackets(false, c2s, []int{20, 20}, "mh", false)
	b.addAll(d)
	b.add(b.sender.FINPacket(false, "mh-fin"))
	return Built{
		Spec: FixtureSpec{
			Name:         "missing_handshake",
			Description:  "capture begins with data, SYN/SYN-ACK absent",
			C2SStreamHex: hex.EncodeToString(c2s), Generations: 1,
			MissingHandshake: true,
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// ConnectionReuse completes a first full exchange including FINs, then a
// second handshake (new ISNs, same 4-tuple) runs a second independent
// stream. The two generations must never share bytes.
func ConnectionReuse() Built {
	b := newBuilder(40011, 8080, 8000, 14000)
	firstC2S := oracle.RangeStream(25, 1)
	firstS2C := oracle.RangeStream(20, 70)
	b.addAll(oracle.HandshakePackets(b.flow))
	b.addAll(b.sender.DataPackets(false, firstC2S, []int{25}, "g1c", true))
	b.addAll(b.sender.DataPackets(true, firstS2C, []int{20}, "g1s", true))

	// Second handshake: reuse with deliberately close ISNs to stress the
	// generation binding.
	isn2C2S := uint32(8000 + 200)
	isn2S2C := uint32(14000 + 200)
	s2 := oracle.NewSenderAt(isn2C2S, isn2S2C, b.order)
	b.appendPacket(oracle.Packet{RecordID: "g2-syn", Kind: oracle.KindSYN, Seq: isn2C2S, Order: s2.NextOrder()})
	b.appendPacket(oracle.Packet{RecordID: "g2-synack", Kind: oracle.KindSYNACK, FromS2C: true,
		Seq: isn2S2C, Ack: isn2C2S + 1, HasAck: true, Order: s2.NextOrder()})
	b.appendPacket(oracle.Packet{RecordID: "g2-ack", Kind: oracle.KindACK,
		Seq: isn2C2S + 1, Ack: isn2S2C + 1, HasAck: true, Order: s2.NextOrder()})
	secondC2S := oracle.RangeStream(35, 97)
	secondS2C := oracle.RangeStream(15, 33)
	for _, p := range s2.DataPackets(false, secondC2S, []int{35}, "g2c", true) {
		b.appendPacket(p)
	}
	for _, p := range s2.DataPackets(true, secondS2C, []int{15}, "g2s", true) {
		b.appendPacket(p)
	}

	return Built{
		Spec: FixtureSpec{
			Name:        "connection_reuse",
			Description: "two sequential handshakes on one 4-tuple, distinct generations",
			Generations: 2, InjectedReuse: true,
			// Streams of the *last* generation are what callers typically
			// query; the test verifies both generations explicitly, so keep
			// the golden hex for generation 2 here and assert generation 1
			// separately in the test.
			C2SStreamHex: hex.EncodeToString(secondC2S),
			S2CStreamHex: hex.EncodeToString(secondS2C),
		},
		Packets: b.pkts,
		Flow:    b.flow,
	}
}

// All returns every named builder.
func All() []Built {
	return []Built{
		InOrder(), OutOfOrder(), Retransmission(), ConflictingRetransmission(),
		OverlapConflict(), MissingSegments(), WrapBoundary(), WrapBoundaryGap(),
		HalfClose(), MissingHandshake(), ConnectionReuse(),
	}
}

func handshake(b *builder) []oracle.Packet {
	ps := oracle.HandshakePackets(b.flow)
	b.sender = oracle.NewSender(b.flow) // reset sender counters
	return ps
}

func withS2C(p oracle.Packet) oracle.Packet { p.FromS2C = true; return p }

// Describe renders a one-line summary for logs.
func (bl Built) Describe() string {
	return fmt.Sprintf("%s: %d packets, gens=%d, gaps=%d, conflicts=%d",
		bl.Spec.Name, len(bl.Packets), bl.Spec.Generations,
		len(bl.Spec.OpenGaps), len(bl.Spec.Conflicts))
}
