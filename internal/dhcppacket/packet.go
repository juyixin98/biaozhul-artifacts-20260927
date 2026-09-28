// Package dhcppacket encodes and decodes the subset of BOOTP/DHCPv4 used by
// this lab: message types DISCOVER(1), OFFER(2), REQUEST(3), ACK(5), NAK(6)
// and RELEASE(7). It implements only what RFC 2131 requires for those types;
// unknown or unsupported options are preserved only when relevant.
//
// Wire layout (RFC 2131 §2):
//
//	0                   1                   2                   3
//	0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
//	+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
//	|     op (1)    |   htype (1)   |   hlen (1)   |   hops (1)    |
//	|                       xid (4)                                |
//	|                      secs (2)  |           flags (2)         |
//	|                     ciaddr (4)                               |
//	|                     yiaddr (4)                               |
//	|                     siaddr (4)                               |
//	|                     giaddr (4)                               |
//	|                                                             |
//	|                     chaddr (16)                              |
//	|                                                             |
//	|                     sname  (64)                              |
//	|                     file   (128)                             |
//	|                     options (variable)  magic + TLVs + end   |
package dhcppacket

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
)

// BOOTP op codes.
const (
	OpBootRequest = 1 // client -> server
	OpBootReply   = 2 // server -> client
)

// DHCP message types (option 53).
const (
	MsgDiscover = 1
	MsgOffer    = 2
	MsgRequest  = 3
	MsgDecline  = 4
	MsgAck      = 5
	MsgNak      = 6
	MsgRelease  = 7
	MsgInform   = 8
)

// Option codes used by this subset.
const (
	OptPad           = 0
	OptSubnetMask    = 1
	OptRouter        = 3
	OptDNSServer     = 6
	OptBroadcastAddr = 28
	OptRequestedIP   = 50
	OptLeaseTime     = 51
	OptMessageType   = 53
	OptServerID      = 54
	OptParameterList = 55
	OptMessage       = 56
	OptClientID      = 61
	OptRenewalTime   = 58 // T1
	OptRebindingTime = 59 // T2
	OptClientLastTX  = 91
	OptEnd           = 255
)

// MagicCookie identifies a DHCP options area (RFC 2131).
var MagicCookie = [4]byte{99, 130, 83, 99}

// MinPacketLen is the fixed BOOTP header + magic cookie. Max is BOOTP 576.
const (
	headerLen    = 236
	minPacketLen = headerLen + 4
	maxPacketLen = 576
)

// ParseError describes a malformed packet; the transport layer maps it to the
// failure category "malformed_packet" instead of silently answering success.
type ParseError struct{ Reason string }

func (e *ParseError) Error() string { return "malformed DHCP packet: " + e.Reason }

func parseError(format string, a ...any) error {
	return &ParseError{Reason: fmt.Sprintf(format, a...)}
}

// Packet is the decoded representation of one datagram.
type Packet struct {
	Op    byte
	HType byte
	HLen  byte
	Hops  byte
	XID   uint32
	Secs  uint16
	// BroadcastFlag is the low bit of flags; replies are still unicast on
	// loopback transport, but the value is retained for test assertions.
	BroadcastFlag bool
	CIAddr        netip.Addr
	YIAddr        netip.Addr
	SIAddr        netip.Addr
	GIAddr        netip.Addr
	// CHAddr holds hlen bytes of the client hardware address.
	CHAddr []byte
	// Options maps option code -> raw value bytes.
	Options map[byte][]byte
}

// MessageType returns option 53, or 0 with an error when absent/invalid.
func (p *Packet) MessageType() (byte, error) {
	v, ok := p.Options[OptMessageType]
	if !ok || len(v) != 1 {
		return 0, parseError("option 53 (message type) missing or invalid")
	}
	return v[0], nil
}

// ClientID returns option 61 raw bytes (type prefix + identifier), or nil.
func (p *Packet) ClientID() []byte { return p.Options[OptClientID] }

// RequestedIP parses option 50, returning an invalid Addr when absent.
func (p *Packet) RequestedIP() (netip.Addr, bool) {
	v, ok := p.Options[OptRequestedIP]
	if !ok || len(v) != 4 {
		return netip.Addr{}, false
	}
	return byte4ToAddr(v), true
}

// ServerID parses option 54, returning an invalid Addr when absent.
func (p *Packet) ServerID() (netip.Addr, bool) {
	v, ok := p.Options[OptServerID]
	if !ok || len(v) != 4 {
		return netip.Addr{}, false
	}
	return byte4ToAddr(v), true
}

// Decode parses a UDP payload. It enforces op, magic cookie, minimum length,
// htype/hlen consistency and option TLV framing.
func Decode(b []byte) (*Packet, error) {
	if len(b) < minPacketLen {
		return nil, parseError("length %d below minimum %d", len(b), minPacketLen)
	}
	if len(b) > maxPacketLen {
		return nil, parseError("length %d above BOOTP maximum %d", len(b), maxPacketLen)
	}
	if b[0] != OpBootRequest && b[0] != OpBootReply {
		return nil, parseError("op=%d is neither BOOTREQUEST(1) nor BOOTREPLY(2)", b[0])
	}
	var cookie [4]byte
	copy(cookie[:], b[236:240])
	if cookie != MagicCookie {
		return nil, parseError("bad magic cookie %v", cookie[:])
	}
	p := &Packet{
		Op:    b[0],
		HType: b[1],
		HLen:  b[2],
		Hops:  b[3],
		XID:   binary.BigEndian.Uint32(b[4:8]),
		Secs:  binary.BigEndian.Uint16(b[8:10]),
	}
	flags := binary.BigEndian.Uint16(b[10:12])
	p.BroadcastFlag = flags&0x8000 != 0
	p.CIAddr = byte4ToAddr(b[12:16])
	p.YIAddr = byte4ToAddr(b[16:20])
	p.SIAddr = byte4ToAddr(b[20:24])
	p.GIAddr = byte4ToAddr(b[24:28])
	if p.HLen > 16 {
		return nil, parseError("hlen=%d exceeds chaddr field (16)", p.HLen)
	}
	// Ethernet (htype 1) is the expected lab media; hlen must agree.
	if p.HType == 1 && p.HLen != 6 {
		return nil, parseError("ethernet htype=1 requires hlen=6, got %d", p.HLen)
	}
	p.CHAddr = make([]byte, p.HLen)
	if p.HLen > 0 {
		copy(p.CHAddr, b[28:28+p.HLen])
	}
	// Clients with hlen 0 are rejected: chaddr is part of client identity.
	if p.HLen == 0 || allZero(p.CHAddr) {
		return nil, parseError("client hardware address is empty")
	}

	opts, err := decodeOptions(b[240:])
	if err != nil {
		return nil, err
	}
	p.Options = opts
	return p, nil
}

func decodeOptions(b []byte) (map[byte][]byte, error) {
	opts := make(map[byte][]byte)
	i := 0
	seenEnd := false
	for i < len(b) {
		code := b[i]
		i++
		switch code {
		case OptPad:
			continue
		case OptEnd:
			seenEnd = true
		}
		if seenEnd {
			// trailing bytes after END must be padding
			for ; i < len(b); i++ {
				if b[i] != OptPad && b[i] != OptEnd {
					return nil, parseError("non-padding byte after END option at offset %d", i)
				}
			}
			break
		}
		if i >= len(b) {
			return nil, parseError("option %d truncated before length", code)
		}
		l := int(b[i])
		i++
		if i+l > len(b) {
			return nil, parseError("option %d declares length %d but only %d bytes remain", code, l, len(b)-i)
		}
		val := make([]byte, l)
		copy(val, b[i:i+l])
		// First occurrence wins; duplicates are recorded nowhere (lab subset).
		if _, exists := opts[code]; !exists {
			opts[code] = val
		}
		i += l
	}
	if !seenEnd {
		return nil, parseError("options area missing END (255)")
	}
	return opts, nil
}

// Encode serializes a packet to a BOOTP datagram with magic cookie and TLVs.
// Callers normally build replies via ReplyFor; this is exported so the
// independent test harness can also synthesize client packets.
func (p *Packet) Encode() ([]byte, error) {
	buf := make([]byte, headerLen)
	buf[0] = p.Op
	buf[1] = p.HType
	buf[2] = p.HLen
	buf[3] = p.Hops
	binary.BigEndian.PutUint32(buf[4:8], p.XID)
	binary.BigEndian.PutUint16(buf[8:10], p.Secs)
	if p.BroadcastFlag {
		binary.BigEndian.PutUint16(buf[10:12], 0x8000)
	}
	putAddr(buf[12:16], p.CIAddr)
	putAddr(buf[16:20], p.YIAddr)
	putAddr(buf[20:24], p.SIAddr)
	putAddr(buf[24:28], p.GIAddr)
	if len(p.CHAddr) > 16 {
		return nil, errors.New("chaddr exceeds 16 bytes")
	}
	copy(buf[28:28+len(p.CHAddr)], p.CHAddr)
	buf = append(buf, MagicCookie[:]...)

	// Deterministic order for repeatable test vectors.
	for _, code := range optionCodesSorted(p.Options) {
		v := p.Options[code]
		if len(v) > 255 {
			return nil, fmt.Errorf("option %d value %d bytes exceeds 255", code, len(v))
		}
		buf = append(buf, code, byte(len(v)))
		buf = append(buf, v...)
	}
	buf = append(buf, OptEnd)
	return buf, nil
}

// Builder is a convenience constructor for client packets used by the local
// fixtures. It is deliberately in the same package as the decoder so tests
// build wire bytes through the same framing code — but assertions on semantics
// live entirely outside this package.
type Builder struct{ p *Packet }

// NewRequest starts a client BOOTREQUEST. htype/hlen default to Ethernet/6.
func NewRequest(xid uint32, chaddr net.HardwareAddr) *Builder {
	return &Builder{p: &Packet{
		Op:      OpBootRequest,
		HType:   1,
		HLen:    6,
		XID:     xid,
		CHAddr:  append([]byte(nil), chaddr...),
		Options: map[byte][]byte{},
	}}
}

// Secs sets the secs field.
func (b *Builder) Secs(s uint16) *Builder { b.p.Secs = s; return b }

// Broadcast sets the broadcast flag bit.
func (b *Builder) Broadcast(on bool) *Builder { b.p.BroadcastFlag = on; return b }

// CIAddr sets ciaddr (non-zero for renew/rebind).
func (b *Builder) CIAddr(a netip.Addr) *Builder { b.p.CIAddr = a; return b }

// Type sets option 53.
func (b *Builder) Type(t byte) *Builder { b.p.Options[OptMessageType] = []byte{t}; return b }

// ClientID sets option 61 from raw bytes (e.g. []byte{1} + mac).
func (b *Builder) ClientID(id []byte) *Builder {
	b.p.Options[OptClientID] = append([]byte(nil), id...)
	return b
}

// RequestedIP sets option 50.
func (b *Builder) RequestedIP(a netip.Addr) *Builder {
	b.p.Options[OptRequestedIP] = addrToByte4(a)
	return b
}

// ServerID sets option 54.
func (b *Builder) ServerID(a netip.Addr) *Builder {
	b.p.Options[OptServerID] = addrToByte4(a)
	return b
}

// Params sets option 55 (parameter request list).
func (b *Builder) Params(codes ...byte) *Builder {
	b.p.Options[OptParameterList] = append([]byte(nil), codes...)
	return b
}

// Message sets option 56 (human-readable text).
func (b *Builder) Message(s string) *Builder {
	b.p.Options[OptMessage] = []byte(s)
	return b
}

// Bytes encodes the built packet.
func (b *Builder) Bytes() []byte {
	raw, err := b.p.Encode()
	if err != nil {
		panic(err) // builder misuse is a programming error in fixtures
	}
	return raw
}

// Packet returns the underlying packet (read-modify scenarios in tests).
func (b *Builder) Packet() *Packet { return b.p }
