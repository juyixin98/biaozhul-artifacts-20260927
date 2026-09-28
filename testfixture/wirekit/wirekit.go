// Package wirekit is an INDEPENDENT reference toolkit for DHCPv4 BOOTP
// messages used by the lab tests. It deliberately does NOT import the
// implementation under test (internal/dhcp4): reference answers are
// generated from a separate, deliberately low-level byte assembler so
// that a shared bug cannot make a test pass.
//
// References: RFC 951 (BOOTP), RFC 2131 (DHCP), RFC 2132 (options).
package wirekit

import (
	"errors"
	"fmt"
)

// Field offsets in the 236-byte fixed BOOTP header.
const (
	OffOp      = 0
	OffHType   = 1
	OffHLen    = 2
	OffHops    = 3
	OffXID     = 4
	OffSecs    = 8
	OffFlags   = 10
	OffCIAddr  = 12
	OffYIAddr  = 20
	OffSIAddr  = 24
	OffGIAddr  = 28
	OffCHAddr  = 44
	OffSName   = 48
	OffFile    = 108
	OffOptions = 236
)

// Magic cookie (RFC 2131 §3).
var Cookie = [4]byte{99, 130, 83, 99}

// Op codes.
const (
	OpBootRequest = 1
	OpBootReply   = 2
)

// Message type values (RFC 2132 9.6).
const (
	MTDiscover = 1
	MTOffer    = 2
	MTRequest  = 3
	MTDecline  = 4
	MTACK      = 5
	MTNAK      = 6
	MTRelease  = 7
	MTInform   = 8
)

// Option codes used by the subset.
const (
	OptPad          = 0
	OptSubnetMask   = 1
	OptRouter       = 3
	OptDNS          = 6
	OptHostname     = 12
	OptRequestedIP  = 50
	OptLeaseTime    = 51
	OptMsgType      = 53
	OptServerID     = 54
	OptParamRequest = 55
	OptClientID     = 61
	OptEnd          = 255
)

// MinReplyLen is the minimum length clients must accept and servers pad to.
const MinReplyLen = 300

// Packet is a field-level view over a message used by tests.
type Packet struct {
	Op        byte
	HType     byte
	HLen      byte
	Hops      byte
	XID       [4]byte
	Secs      uint16
	Broadcast bool
	CIAddr    [4]byte
	YIAddr    [4]byte
	SIAddr    [4]byte
	GIAddr    [4]byte
	CHAddr    [6]byte
	Opts      map[byte][]byte
}

// ParseError is a reference parser's classified error.
type ParseError struct {
	Category string
	Detail   string
}

func (e *ParseError) Error() string { return e.Category + ": " + e.Detail }

func perr(cat, detail string, a ...any) error {
	return &ParseError{Category: cat, Detail: fmt.Sprintf(detail, a...)}
}

// Parse decodes a BOOTP/DHCP message with strict framing checks. This is
// the independent reference parser used to validate server replies.
func Parse(b []byte) (*Packet, error) {
	if len(b) < 240 {
		return nil, perr("REF_TOO_SHORT", "%d bytes < 240", len(b))
	}
	if b[OffOp] != OpBootRequest && b[OffOp] != OpBootReply {
		return nil, perr("REF_BAD_OP", "op=%d", b[OffOp])
	}
	if [4]byte{b[236], b[237], b[238], b[239]} != Cookie {
		return nil, perr("REF_BAD_COOKIE", "% x", b[236:240])
	}
	p := &Packet{
		Op: b[OffOp], HType: b[OffHType], HLen: b[OffHLen], Hops: b[OffHops],
		Secs:      uint16(b[OffSecs])<<8 | uint16(b[OffSecs+1]),
		Broadcast: b[OffFlags]&0x80 != 0,
		Opts:      map[byte][]byte{},
	}
	copy(p.XID[:], b[OffXID:OffXID+4])
	copy(p.CIAddr[:], b[OffCIAddr:OffCIAddr+4])
	copy(p.YIAddr[:], b[OffYIAddr:OffYIAddr+4])
	copy(p.SIAddr[:], b[OffSIAddr:OffSIAddr+4])
	copy(p.GIAddr[:], b[OffGIAddr:OffGIAddr+4])
	if !(b[OffHType] == 1 && b[OffHLen] == 6) {
		return nil, perr("REF_BAD_HTYPE", "htype=%d hlen=%d", b[OffHType], b[OffHLen])
	}
	copy(p.CHAddr[:], b[OffCHAddr:OffCHAddr+6])

	i := OffOptions + 4
	for i < len(b) {
		code := b[i]
		i++
		if code == OptPad {
			continue
		}
		if code == OptEnd {
			break
		}
		if i >= len(b) {
			return nil, perr("REF_TRUNC_LEN", "option %d missing length", code)
		}
		l := int(b[i])
		i++
		if i+l > len(b) {
			return nil, perr("REF_OPT_OVERRUN", "option %d len %d", code, l)
		}
		if _, seen := p.Opts[code]; !seen {
			v := make([]byte, l)
			copy(v, b[i:i+l])
			p.Opts[code] = v
		}
		i += l
	}
	return p, nil
}

// Builder assembles requests (and, in tests, adversarial malformed bytes).
type Builder struct {
	p *Packet
}

// NewRequest starts a BOOTREQUEST for an Ethernet client.
func NewRequest(xid [4]byte, chaddr [6]byte) *Builder {
	return &Builder{p: &Packet{
		Op: OpBootRequest, HType: 1, HLen: 6, XID: xid, CHAddr: chaddr,
		Opts: map[byte][]byte{},
	}}
}

// FromPacket starts a builder from an existing parsed packet (e.g. to
// mutate/replay a captured request).
func FromPacket(p *Packet) *Builder {
	cp := *p
	cp.Opts = map[byte][]byte{}
	for k, v := range p.Opts {
		cp.Opts[k] = append([]byte(nil), v...)
	}
	return &Builder{p: &cp}
}

func (b *Builder) Broadcast() *Builder    { b.p.Broadcast = true; return b }
func (b *Builder) Secs(s uint16) *Builder { b.p.Secs = s; return b }
func (b *Builder) Hops(h byte) *Builder   { b.p.Hops = h; return b }
func (b *Builder) CIAddr(a [4]byte) *Builder {
	b.p.CIAddr = a
	return b
}
func (b *Builder) GIAddr(a [4]byte) *Builder { b.p.GIAddr = a; return b }
func (b *Builder) Op(op byte) *Builder       { b.p.Op = op; return b }

// MsgType sets option 53.
func (b *Builder) MsgType(t byte) *Builder {
	b.p.Opts[OptMsgType] = []byte{t}
	return b
}
func (b *Builder) ServerID(ip [4]byte) *Builder {
	b.p.Opts[OptServerID] = ip[:]
	return b
}
func (b *Builder) RequestedIP(ip [4]byte) *Builder {
	b.p.Opts[OptRequestedIP] = ip[:]
	return b
}
func (b *Builder) LeaseTime(secs uint32) *Builder {
	b.p.Opts[OptLeaseTime] = U32(secs)
	return b
}
func (b *Builder) ClientID(raw []byte) *Builder {
	b.p.Opts[OptClientID] = append([]byte(nil), raw...)
	return b
}
func (b *Builder) Hostname(h string) *Builder {
	b.p.Opts[OptHostname] = []byte(h)
	return b
}
func (b *Builder) ParamRequest(codes ...byte) *Builder {
	b.p.Opts[OptParamRequest] = append([]byte(nil), codes...)
	return b
}

// RawOption sets an arbitrary option (for malformed-input tests).
func (b *Builder) RawOption(code byte, v []byte) *Builder {
	b.p.Opts[code] = append([]byte(nil), v...)
	return b
}

// DeleteOption removes an option (for malformed-input tests).
func (b *Builder) DeleteOption(code byte) *Builder {
	delete(b.p.Opts, code)
	return b
}

// Build serializes the packet. Output is zero-padded to at least 300
// bytes (MinReplyLen). Options are emitted in deterministic order (53
// first, then ascending code) so reference captures are reproducible;
// the server must not depend on option order.
func (b *Builder) Build() []byte {
	p := b.p
	// Options used by this subset are tiny; 576 is the DHCP minimum
	// maximum message size clients must accept, ample headroom.
	buf := make([]byte, 576)
	buf[OffOp] = p.Op
	buf[OffHType] = p.HType
	buf[OffHLen] = p.HLen
	buf[OffHops] = p.Hops
	copy(buf[OffXID:OffXID+4], p.XID[:])
	buf[OffSecs] = byte(p.Secs >> 8)
	buf[OffSecs+1] = byte(p.Secs)
	if p.Broadcast {
		buf[OffFlags] = 0x80
	}
	copy(buf[OffCIAddr:OffCIAddr+4], p.CIAddr[:])
	copy(buf[OffYIAddr:OffYIAddr+4], p.YIAddr[:])
	copy(buf[OffSIAddr:OffSIAddr+4], p.SIAddr[:])
	copy(buf[OffGIAddr:OffGIAddr+4], p.GIAddr[:])
	copy(buf[OffCHAddr:OffCHAddr+6], p.CHAddr[:])
	copy(buf[OffOptions:OffOptions+4], Cookie[:])

	pos := OffOptions + 4
	if v, ok := p.Opts[OptMsgType]; ok {
		pos = writeOpt(buf, pos, OptMsgType, v)
	}
	codes := make([]int, 0, len(p.Opts))
	for c := range p.Opts {
		if c != OptMsgType {
			codes = append(codes, int(c))
		}
	}
	for i := 1; i < len(codes); i++ { // insertion sort, deterministic
		for j := i; j > 0 && codes[j] < codes[j-1]; j-- {
			codes[j], codes[j-1] = codes[j-1], codes[j]
		}
	}
	for _, c := range codes {
		pos = writeOpt(buf, pos, byte(c), p.Opts[byte(c)])
	}
	buf[pos] = OptEnd
	pos++
	if pos < MinReplyLen {
		pos = MinReplyLen
	}
	return buf[:pos]
}

func writeOpt(buf []byte, pos int, code byte, v []byte) int {
	if pos+2+len(v) >= len(buf) {
		panic("wirekit: option area exceeds 576-byte fixture message")
	}
	buf[pos] = code
	buf[pos+1] = byte(len(v))
	copy(buf[pos+2:], v)
	return pos + 2 + len(v)
}

// MsgTypeName names a message type byte.
func MsgTypeName(t byte) string {
	switch t {
	case MTDiscover:
		return "DISCOVER"
	case MTOffer:
		return "OFFER"
	case MTRequest:
		return "REQUEST"
	case MTACK:
		return "ACK"
	case MTNAK:
		return "NAK"
	case MTRelease:
		return "RELEASE"
	default:
		return fmt.Sprintf("UNKNOWN(%d)", t)
	}
}

// MsgType returns option 53 from a parsed packet.
func (p *Packet) MsgType() (byte, bool) {
	v, ok := p.Opts[OptMsgType]
	return v[0], ok && len(v) == 1
}

// U32 encodes a big-endian uint32.
func U32(v uint32) []byte {
	return []byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)}
}

// U32At reads a big-endian uint32.
func U32At(b []byte) uint32 {
	return uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
}

// IP parses a dotted-quad into 4 bytes (test fixture helper).
func IP(s string) [4]byte {
	var a, b, c, d int
	if _, err := fmt.Sscanf(s, "%d.%d.%d.%d", &a, &b, &c, &d); err != nil ||
		a > 255 || b > 255 || c > 255 || d > 255 {
		panic(errors.New("wirekit: bad ipv4 " + s))
	}
	return [4]byte{byte(a), byte(b), byte(c), byte(d)}
}

// IPStr renders 4 bytes as dotted-quad.
func IPStr(a [4]byte) string {
	return fmt.Sprintf("%d.%d.%d.%d", a[0], a[1], a[2], a[3])
}

// MAC parses aa:bb:cc:dd:ee:ff.
func MAC(s string) [6]byte {
	var m [6]byte
	if _, err := fmt.Sscanf(s, "%02x:%02x:%02x:%02x:%02x:%02x",
		&m[0], &m[1], &m[2], &m[3], &m[4], &m[5]); err != nil {
		panic(errors.New("wirekit: bad mac " + s))
	}
	return m
}

// MACStr renders a MAC.
func MACStr(m [6]byte) string {
	return fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5])
}

// XID renders a transaction id as hex.
func XID(x [4]byte) string { return fmt.Sprintf("%08x", x) }
