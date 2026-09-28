// Package netmodel defines the protocol-level objects used by the reassembler:
// TCP packet observations, flow identity and 32-bit sequence-space helpers.
// Everything here is a value type; no I/O or persistence lives in this layer.
package netmodel

// TCP flag bits as defined by RFC 9293.
const (
	FlagFIN uint8 = 0x01
	FlagSYN uint8 = 0x02
	FlagRST uint8 = 0x04
	FlagPSH uint8 = 0x08
	FlagACK uint8 = 0x10
	FlagURG uint8 = 0x20
)

// FlowKey identifies a bidirectional connection by the unordered 4-tuple.
// Canonicalization puts the lower IP:port endpoint into A, so packets in
// either direction map to the same key.
type FlowKey struct {
	AIP   string
	APort uint16
	BIP   string
	BPort uint16
}

// Endpoint is one side of a connection.
type Endpoint struct {
	IP   string `json:"ip"`
	Port uint16 `json:"port"`
}

// DirectionKey names a direction of one connection: A->B or B->A using the
// canonical endpoint ordering of FlowKey.
type DirectionKey struct {
	Flow FlowKey
	AtoB bool
}

// Packet is one observed TCP segment.
//
// Seq and Ack are raw 32-bit header sequence numbers and intentionally use
// uint32: callers must not treat them as absolute positions. Use SeqMapper to
// project them into a monotonic coordinate system with wrap semantics.
//
// Timestamp is optional; when present it must be RFC 3339 text in the API.
type Packet struct {
	SrcIP     string   `json:"src_ip"`
	SrcPort   uint16   `json:"src_port"`
	DstIP     string   `json:"dst_ip"`
	DstPort   uint16   `json:"dst_port"`
	Flags     []string `json:"flags,omitempty"` // e.g. ["SYN"], ["FIN","ACK"]
	Seq       uint32   `json:"seq"`
	Ack       uint32   `json:"ack,omitempty"`
	Payload   []byte   `json:"payload,omitempty"` // base64 over JSON
	Timestamp string   `json:"timestamp,omitempty"`
	RecordID  string   `json:"record_id,omitempty"` // caller-supplied evidence id
}

// FlagMask converts the symbolic flag list to a bitmask. Unknown tokens are
// ignored.
func (p Packet) FlagMask() uint8 {
	var m uint8
	for _, f := range p.Flags {
		switch f {
		case "FIN":
			m |= FlagFIN
		case "SYN":
			m |= FlagSYN
		case "RST":
			m |= FlagRST
		case "PSH":
			m |= FlagPSH
		case "ACK":
			m |= FlagACK
		case "URG":
			m |= FlagURG
		}
	}
	return m
}

// Has reports whether the packet carries the given flag bit.
func (p Packet) Has(bit uint8) bool { return p.FlagMask()&bit != 0 }

// Src returns the source endpoint.
func (p Packet) Src() Endpoint { return Endpoint{IP: p.SrcIP, Port: p.SrcPort} }

// Dst returns the destination endpoint.
func (p Packet) Dst() Endpoint { return Endpoint{IP: p.DstIP, Port: p.DstPort} }

// FlowKey returns the canonical unordered 4-tuple key for this packet.
func (p Packet) FlowKey() FlowKey { return MakeFlowKey(p.Src(), p.Dst()) }

// AtoB reports whether the packet travels canonical-A -> canonical-B.
func (p Packet) FlowKeyAndDir() (FlowKey, bool) {
	k := p.FlowKey()
	aToB := Endpoint{IP: p.SrcIP, Port: p.SrcPort} == Endpoint{IP: k.AIP, Port: k.APort}
	return k, aToB
}

// MakeFlowKey canonicalizes two endpoints into an unordered key. Endpoints are
// ordered by IP text then port, which is deterministic and direction-free;
// direction (A->B / B->A) is tracked separately.
func MakeFlowKey(x, y Endpoint) FlowKey {
	if lessEndpoint(x, y) {
		return FlowKey{AIP: x.IP, APort: x.Port, BIP: y.IP, BPort: y.Port}
	}
	return FlowKey{AIP: y.IP, APort: y.Port, BIP: x.IP, BPort: x.Port}
}

func lessEndpoint(x, y Endpoint) bool {
	if x.IP != y.IP {
		return x.IP < y.IP
	}
	return x.Port < y.Port
}

// String renders the key as ip:port<->ip:port (canonical order).
func (k FlowKey) String() string {
	return ep(k.AIP, k.APort) + "<->" + ep(k.BIP, k.BPort)
}

// OtherEndpoint returns canonical B when from is canonical A and vice versa.
func (k FlowKey) OtherEndpoint(from Endpoint) Endpoint {
	if (Endpoint{IP: k.AIP, Port: k.APort}) == from {
		return Endpoint{IP: k.BIP, Port: k.BPort}
	}
	return Endpoint{IP: k.AIP, Port: k.APort}
}

func ep(ip string, port uint16) string { return ip + ":" + itoa(port) }

func itoa(n uint16) string {
	if n == 0 {
		return "0"
	}
	var b [5]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
