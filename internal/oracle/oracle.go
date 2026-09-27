// Package oracle is the independent reference implementation used by
// fixtures and tests. It is deliberately separate from the reassembly
// package: it models an ideal TCP *sender* (linear byte source + 32-bit
// sequence numbers + SYN/FIN accounting) rather than the reassembling
// receiver under test. The reference answer ("which bytes the original
// stream contained and at which offsets") therefore cannot be produced by
// the code being audited.
package oracle

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// Mod is the TCP sequence space modulus.
const Mod uint64 = 1 << 32

// ModAdd adds offset n to a 32-bit sequence number with wrap.
func ModAdd(seq uint32, n uint64) uint32 {
	return seq + uint32(n)
}

// ModSub returns the modular distance a-b in (-2^31,2^31].
func ModSub(a, b uint32) int64 {
	d := int64(uint32(a - b))
	if d >= 1<<31 {
		d -= int64(Mod)
	}
	return d
}

// Endpoint identifies a synthetic peer.
type Endpoint struct {
	IP   string
	Port uint16
}

func (e Endpoint) String() string { return fmt.Sprintf("%s:%d", e.IP, e.Port) }

// Flow is one synthetic connection's fixed endpoints and ISNs.
type Flow struct {
	Client Endpoint
	Server Endpoint
	ISNC2S uint32
	ISNS2C uint32
}

// NewFlow builds a flow with fixed endpoints and caller-chosen ISNs.
func NewFlow(clientPort, serverPort uint16, isnC2S, isnS2C uint32) Flow {
	return Flow{
		Client: Endpoint{IP: "10.0.0.10", Port: clientPort},
		Server: Endpoint{IP: "10.0.0.20", Port: serverPort},
		ISNC2S: isnC2S, ISNS2C: isnS2C,
	}
}

// RandomISN returns a random 32-bit ISN.
func RandomISN() uint32 {
	var b [4]byte
	_, _ = rand.Read(b[:])
	return uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
}

// PacketKind enumerates what a synthetic packet contains.
type PacketKind int

const (
	KindSYN PacketKind = iota
	KindSYNACK
	KindACK
	KindData
	KindDataFIN
	KindFIN
	KindRST
)

// Packet is an oracle-side wire description (tcpmodel-free so the reference
// generator shares no code with the receiver).
type Packet struct {
	RecordID string
	Kind     PacketKind
	FromS2C  bool
	Seq      uint32
	Ack      uint32
	HasAck   bool
	Payload  []byte
	Order    int64
	DirHint  string // "" (derive), "c2s", "s2c"
}

// Sender emits the canonical, in-order packets for one stream.
type Sender struct {
	flow   Flow
	c2sNxt uint32 // next c2s seq to assign (starts after SYN)
	s2cNxt uint32
	order  int64
}

// NewSender starts after the three-way handshake. Handshake packets are
// available via HandshakePackets.
func NewSender(f Flow) *Sender {
	return &Sender{flow: f, c2sNxt: f.ISNC2S + 1, s2cNxt: f.ISNS2C + 1, order: 3}
}

// NewSenderAt starts a sender with explicit initial sequence numbers and
// order counter, used for second generations on a reused 4-tuple.
func NewSenderAt(isnC2S, isnS2C uint32, order int64) *Sender {
	return &Sender{flow: Flow{ISNC2S: isnC2S, ISNS2C: isnS2C},
		c2sNxt: isnC2S + 1, s2cNxt: isnS2C + 1, order: order}
}

// BumpOrder advances the shared order counter without emitting a packet.
func (s *Sender) BumpOrder(n int64) { s.order += n }

// NextOrder returns the next order value and advances the counter.
func (s *Sender) NextOrder() int64 { s.order++; return s.order }

// HandshakePackets returns the three SYN / SYN-ACK / ACK packets.
func HandshakePackets(f Flow) []Packet {
	return []Packet{
		{RecordID: "hs-syn", Kind: KindSYN, Seq: f.ISNC2S, Order: 1},
		{RecordID: "hs-synack", Kind: KindSYNACK, FromS2C: true,
			Seq: f.ISNS2C, Ack: f.ISNC2S + 1, HasAck: true, Order: 2},
		{RecordID: "hs-ack", Kind: KindACK,
			Seq: f.ISNC2S + 1, Ack: f.ISNS2C + 1, HasAck: true, Order: 3},
	}
}

// DataPackets segments one direction's byte stream into in-order segments.
// When closeNow is true the FIN is attached to the final data segment
// (KindDataFIN) for a non-empty stream, or emitted as a standalone FIN when
// the stream is empty. The FIN consumes exactly one sequence number.
func (s *Sender) DataPackets(fromS2C bool, stream []byte, sizes []int, idPrefix string, closeNow bool) []Packet {
	var out []Packet
	nxt := &s.c2sNxt
	if fromS2C {
		nxt = &s.s2cNxt
	}
	off := 0
	idx := 0
	for off < len(stream) {
		n := len(stream) - off
		if idx < len(sizes) && sizes[idx] > 0 && sizes[idx] < n {
			n = sizes[idx]
		}
		chunk := append([]byte(nil), stream[off:off+n]...)
		seq := *nxt
		kind := KindData
		last := off+n == len(stream)
		if last && closeNow {
			kind = KindDataFIN
		}
		idx++
		s.order++
		out = append(out, Packet{
			RecordID: fmt.Sprintf("%s-%d", idPrefix, idx),
			Kind:     kind, FromS2C: fromS2C, Seq: seq, Payload: chunk,
			HasAck: true, Order: s.order,
		})
		*nxt = ModAdd(seq, uint64(n))
		if kind == KindDataFIN {
			// FIN rides the last segment and consumes the next sequence.
			*nxt = ModAdd(*nxt, 1)
		}
		off += n
	}
	if closeNow && len(stream) == 0 {
		s.order++
		out = append(out, Packet{
			RecordID: fmt.Sprintf("%s-fin", idPrefix),
			Kind:     KindFIN, FromS2C: fromS2C, Seq: *nxt,
			HasAck: true, Order: s.order,
		})
		*nxt = ModAdd(*nxt, 1)
	}
	return out
}

// FINPacket emits a standalone FIN for one direction.
func (s *Sender) FINPacket(fromS2C bool, id string) Packet {
	s.order++
	nxt := s.c2sNxt
	if fromS2C {
		nxt = s.s2cNxt
	}
	p := Packet{RecordID: id, Kind: KindFIN, FromS2C: fromS2C, Seq: nxt, HasAck: true, Order: s.order}
	if fromS2C {
		s.s2cNxt = ModAdd(s.s2cNxt, 1)
	} else {
		s.c2sNxt = ModAdd(s.c2sNxt, 1)
	}
	return p
}

// Segment emits a raw data segment with caller-chosen sequence number and
// payload (used to construct overlapping/retransmitted captures). It does
// not advance the linear sender cursor: callers model deliberate anomalies.
func (s *Sender) Segment(id string, fromS2C bool, seq uint32, payload []byte, kind PacketKind) Packet {
	s.order++
	return Packet{RecordID: id, Kind: kind, FromS2C: fromS2C, Seq: seq,
		Payload: append([]byte(nil), payload...), HasAck: true, Order: s.order}
}

// ---- expected answer model --------------------------------------------------------

// ExpectedStream is the independent reference answer for one direction.
type ExpectedStream struct {
	// Original is the complete byte stream the application sent.
	Original []byte
	// Missing are byte intervals [start,end) the fixture deliberately
	// removed from the capture. With strict reassembly these must appear
	// verbatim as open gaps.
	Missing [][2]uint64
}

// ExpectedAssembly is the full reference answer for a fixture.
type ExpectedAssembly struct {
	Flow Flow
	C2S  ExpectedStream
	S2C  ExpectedStream
	// Conflicts are the contradictory ranges the fixture injects: each
	// names the byte offset range and the SHA-256 of original vs injected.
	Conflicts []ExpectedConflict
	// ReuseGeneration is set when the capture contains a second handshake
	// generation on the same 4-tuple.
	ReuseGeneration *ExpectedAssembly
	// MissingHandshake marks captures that begin with data and no SYN.
	MissingHandshake bool
}

// ExpectedConflict describes an injected contradiction precisely.
type ExpectedConflict struct {
	Direction   string
	StartOff    uint64
	EndOff      uint64
	OriginalSHA string
	InjectedSHA string
	RecordID    string
}

// Hash returns the SHA-256 hex of bytes (used by tests for byte-level
// comparisons without embedding long expected payloads).
func Hash(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Repeat builds a deterministic payload: byte(offset) cycles the alphabet.
func Repeat(n int, seed byte) []byte {
	out := make([]byte, n)
	const alphabet = "abcdefghijklmnopqrstuvwxyz"
	for i := 0; i < n; i++ {
		out[i] = alphabet[(int(seed)+i)%26]
	}
	return out
}

// RangeStream builds a payload whose byte at offset i is (base+i) mod 256.
// Such a stream makes *which* byte is missing or contradicted trivial to
// identify independently of the implementation under test.
func RangeStream(n int, base byte) []byte {
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		out[i] = base + byte(i)
	}
	return out
}
