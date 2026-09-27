// Package tcpmodel is the wire-level vocabulary shared by every layer:
// synthetic packet records, the canonical 4-tuple key, direction labels and
// the JSONL capture format used by fixtures and the ingest HTTP endpoint.
//
// It deliberately contains no reassembly logic. Payloads are carried as raw
// bytes; hex is used only at the JSON boundary.
package tcpmodel

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"strings"
)

// Direction labels a packet relative to the recorded connection.
type Direction string

const (
	// DirUnknown is used only for packets that cannot be oriented (for
	// example a stray SYN/ACK with no prior SYN).
	DirUnknown Direction = "unknown"
	// DirC2S travels from the recorded client (initiator) to server.
	DirC2S Direction = "c2s"
	// DirS2C travels from the recorded server to client.
	DirS2C Direction = "s2c"
)

// Endpoint is one IP:port side of a connection.
type Endpoint struct {
	IP   netip.Addr `json:"-"`
	Port uint16     `json:"port"`
}

// IsValid reports whether both the address and port are set.
func (e Endpoint) IsValid() bool { return e.IP.IsValid() && e.Port != 0 }

// FlowKey is the canonical 4-tuple. KeyString is orientation independent:
// the numerically smaller endpoint sorts first. Reused connections share a
// FlowKey and are separated into handshake generations by the assembler.
type FlowKey struct {
	A      Endpoint `json:"-"`
	B      Endpoint `json:"-"`
	KeyStr string   `json:"key"`
}

// Packet is one observed TCP segment in a capture.
type Packet struct {
	// RecordID is the unique id of the packet within its capture/file.
	RecordID string `json:"record_id,omitempty"`
	// Order is the observation (capture) order. Ties are broken by RecordID.
	Order int64 `json:"order"`
	// CapTS is capture timestamp in milliseconds since the capture epoch;
	// informational only, reassembly is driven by Order.
	CapTS int64 `json:"cap_ts_ms,omitempty"`

	SrcIP   netip.Addr `json:"-"`
	SrcPort uint16     `json:"src_port"`
	DstIP   netip.Addr `json:"-"`
	DstPort uint16     `json:"dst_port"`

	Seq     uint32 `json:"seq"`
	Ack     uint32 `json:"ack,omitempty"`
	HasAck  bool   `json:"has_ack,omitempty"`
	SYN     bool   `json:"syn"`
	ACK     bool   `json:"ack_flag,omitempty"`
	FIN     bool   `json:"fin"`
	RST     bool   `json:"rst"`
	Window  uint16 `json:"window,omitempty"`
	Payload []byte `json:"-"`

	// DirHint lets a fixture force an orientation instead of deriving it
	// from the handshake. Only "c2s"/"s2c" are honored.
	DirHint Direction `json:"dir_hint,omitempty"`
}

// Flow builds the canonical key for the packet's endpoints.
func (p Packet) Flow() (FlowKey, error) {
	if !p.SrcIP.IsValid() || !p.DstIP.IsValid() {
		return FlowKey{}, errors.New("packet has invalid endpoint IP")
	}
	src := Endpoint{IP: p.SrcIP, Port: p.SrcPort}
	dst := Endpoint{IP: p.DstIP, Port: p.DstPort}
	a, b := src, dst
	if CompareEndpoints(src, dst) > 0 {
		a, b = dst, src
	}
	return FlowKey{
		A:      a,
		B:      b,
		KeyStr: fmt.Sprintf("%s:%d<->%s:%d", a.IP, a.Port, b.IP, b.Port),
	}, nil
}

// CompareEndpoints orders endpoints by IP then port; 0 means equal.
func CompareEndpoints(x, y Endpoint) int {
	if c := x.IP.Compare(y.IP); c != 0 {
		return c
	}
	switch {
	case x.Port < y.Port:
		return -1
	case x.Port > y.Port:
		return 1
	default:
		return 0
	}
}

// DirectionOf orients the packet against a flow: which endpoint is its
// source. Returns DirUnknown when the packet belongs to neither side.
func (k FlowKey) DirectionOf(p Packet) Direction {
	src := Endpoint{IP: p.SrcIP, Port: p.SrcPort}
	switch {
	case CompareEndpoints(src, k.A) == 0:
		return DirC2S // caller swaps if A turned out to be the server
	case CompareEndpoints(src, k.B) == 0:
		return DirS2C
	default:
		return DirUnknown
	}
}

// Other returns the endpoint of a flow that is not e (or the zero endpoint
// when e belongs to neither side).
func (k FlowKey) Other(e Endpoint) Endpoint {
	switch CompareEndpoints(e, k.A) {
	case 0:
		return k.B
	case 1, -1:
		if CompareEndpoints(e, k.B) == 0 {
			return k.A
		}
	}
	return Endpoint{}
}

// ---- JSONL wire representation -------------------------------------------------

type jsonPacket struct {
	RecordID string    `json:"record_id,omitempty"`
	Order    int64     `json:"order"`
	CapTS    int64     `json:"cap_ts_ms,omitempty"`
	SrcIP    string    `json:"src_ip"`
	SrcPort  uint16    `json:"src_port"`
	DstIP    string    `json:"dst_ip"`
	DstPort  uint16    `json:"dst_port"`
	Seq      uint32    `json:"seq"`
	Ack      uint32    `json:"ack,omitempty"`
	HasAck   bool      `json:"has_ack,omitempty"`
	SYN      bool      `json:"syn"`
	ACK      bool      `json:"ack_flag,omitempty"`
	FIN      bool      `json:"fin"`
	RST      bool      `json:"rst"`
	Window   uint16    `json:"window,omitempty"`
	Payload  string    `json:"payload_hex,omitempty"`
	DirHint  Direction `json:"dir_hint,omitempty"`
}

// MarshalJSON encodes payloads as lowercase hex.
func (p Packet) MarshalJSON() ([]byte, error) {
	jp := jsonPacket{
		RecordID: p.RecordID, Order: p.Order, CapTS: p.CapTS,
		SrcPort: p.SrcPort, DstPort: p.DstPort,
		Seq: p.Seq, Ack: p.Ack, HasAck: p.HasAck,
		SYN: p.SYN, ACK: p.ACK, FIN: p.FIN, RST: p.RST, Window: p.Window,
		Payload: hex.EncodeToString(p.Payload), DirHint: p.DirHint,
	}
	if p.SrcIP.IsValid() {
		jp.SrcIP = p.SrcIP.String()
	}
	if p.DstIP.IsValid() {
		jp.DstIP = p.DstIP.String()
	}
	return json.Marshal(jp)
}

// UnmarshalJSON decodes one capture record.
func (p *Packet) UnmarshalJSON(b []byte) error {
	var jp jsonPacket
	if err := json.Unmarshal(b, &jp); err != nil {
		return err
	}
	var pl []byte
	if jp.Payload != "" {
		v, err := hex.DecodeString(jp.Payload)
		if err != nil {
			return fmt.Errorf("payload_hex: %w", err)
		}
		pl = v
	}
	src, err := netip.ParseAddr(jp.SrcIP)
	if err != nil {
		return fmt.Errorf("src_ip: %w", err)
	}
	dst, err := netip.ParseAddr(jp.DstIP)
	if err != nil {
		return fmt.Errorf("dst_ip: %w", err)
	}
	*p = Packet{
		RecordID: jp.RecordID, Order: jp.Order, CapTS: jp.CapTS,
		SrcIP: src, SrcPort: jp.SrcPort, DstIP: dst, DstPort: jp.DstPort,
		Seq: jp.Seq, Ack: jp.Ack, HasAck: jp.HasAck,
		SYN: jp.SYN, ACK: jp.ACK, FIN: jp.FIN, RST: jp.RST, Window: jp.Window,
		Payload: pl, DirHint: jp.DirHint,
	}
	return nil
}

// SegmentLen returns the amount of sequence space this segment consumes:
// one for SYN, one for FIN, plus the payload length (RFC 793 SEG.LEN).
func (p Packet) SegmentLen() uint64 {
	n := uint64(len(p.Payload))
	if p.SYN {
		n++
	}
	if p.FIN {
		n++
	}
	return n
}

// DataSeqRange returns the half-open absolute-32bit range of the *data*
// bytes: [Seq, Seq+len(payload)). SYN/FIN accounting is handled elsewhere.
func (p Packet) DataSeqRange() (uint32, uint32) {
	return p.Seq, p.Seq + uint32(len(p.Payload))
}

// ParseCapture decodes a JSONL capture (one JSON object per non-empty line).
// Lines beginning with '#' are treated as comments, which keeps fixtures
// self-documenting.
func ParseCapture(data []byte) ([]Packet, error) {
	var out []Packet
	for lineNo, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		var p Packet
		if err := json.Unmarshal([]byte(line), &p); err != nil {
			return nil, fmt.Errorf("capture line %d: %w", lineNo+1, err)
		}
		out = append(out, p)
	}
	return out, nil
}
