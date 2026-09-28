// Package dhcp4 implements the DHCPv4 wire model (RFC 2131 BOOTP message
// layout and option space) for the lab subset DISCOVER/OFFER/REQUEST/ACK/
// NAK/RELEASE. It deliberately contains no socket and no state: it only
// turns bytes into Packet values and back, with strict validation and
// typed parse errors so that callers never have to treat a malformed or
// unknown datagram as success.
package dhcp4

import (
	"errors"
	"fmt"
	"net/netip"
)

// BOOTP op codes (RFC 2131 §2).
const (
	OpBootRequest = 1
	OpBootReply   = 2
)

// MagicCookie marks the start of the vendor option area (RFC 2131 §3).
var MagicCookie = [4]byte{99, 130, 83, 99}

// MinMessageLen is the shortest legal BOOTP message: 236 fixed bytes plus
// the 4-byte magic cookie.
const MinMessageLen = 236 + 4

// MinReplyLen is the shortest length a BOOTP reply is padded to.
const MinReplyLen = 300

// MessageType is option 53 (RFC 2132 §9.6).
type MessageType uint8

const (
	MsgDiscover MessageType = 1
	MsgOffer    MessageType = 2
	MsgRequest  MessageType = 3
	MsgDecline  MessageType = 4
	MsgACK      MessageType = 5
	MsgNAK      MessageType = 6
	MsgRelease  MessageType = 7
	MsgInform   MessageType = 8
)

// String renders the protocol name, or a clearly-marked unknown value.
func (m MessageType) String() string {
	switch m {
	case MsgDiscover:
		return "DISCOVER"
	case MsgOffer:
		return "OFFER"
	case MsgRequest:
		return "REQUEST"
	case MsgDecline:
		return "DECLINE"
	case MsgACK:
		return "ACK"
	case MsgNAK:
		return "NAK"
	case MsgRelease:
		return "RELEASE"
	case MsgInform:
		return "INFORM"
	default:
		return fmt.Sprintf("UNKNOWN(%d)", uint8(m))
	}
}

// InSubset reports whether the message type is part of the implemented
// subset on the receive path.
func (m MessageType) InSubset() bool {
	switch m {
	case MsgDiscover, MsgRequest, MsgRelease:
		return true
	default:
		return false
	}
}

// OptionCode is a DHCP option number (RFC 2132).
type OptionCode uint8

const (
	OptPad              OptionCode = 0
	OptSubnetMask       OptionCode = 1
	OptRouter           OptionCode = 3
	OptDNSServer        OptionCode = 6
	OptHostname         OptionCode = 12
	OptRequestedIP      OptionCode = 50
	OptLeaseTime        OptionCode = 51
	OptMessageType      OptionCode = 53
	OptServerID         OptionCode = 54
	OptParameterRequest OptionCode = 55
	OptClientID         OptionCode = 61
	OptEnd              OptionCode = 255
)

// String names the options used by this subset.
func (o OptionCode) String() string {
	switch o {
	case OptPad:
		return "PAD"
	case OptSubnetMask:
		return "SUBNET_MASK"
	case OptRouter:
		return "ROUTER"
	case OptDNSServer:
		return "DNS_SERVER"
	case OptHostname:
		return "HOSTNAME"
	case OptRequestedIP:
		return "REQUESTED_IP"
	case OptLeaseTime:
		return "LEASE_TIME"
	case OptMessageType:
		return "MESSAGE_TYPE"
	case OptServerID:
		return "SERVER_ID"
	case OptParameterRequest:
		return "PARAMETER_REQUEST_LIST"
	case OptClientID:
		return "CLIENT_ID"
	case OptEnd:
		return "END"
	default:
		return fmt.Sprintf("OPTION(%d)", uint8(o))
	}
}

// Packet is one BOOTP/DHCP message. IPs are kept as netip.Addr (always
// IPv4); chaddr is exactly 6 bytes for Ethernet; Options is a map with
// the single-option code 53/50/51/54 values rendered in typed accessors.
type Packet struct {
	Op        uint8
	HType     uint8
	HLen      uint8
	Hops      uint8
	XID       [4]byte
	Secs      uint16
	Broadcast bool
	CIAddr    netip.Addr
	YIAddr    netip.Addr
	SIAddr    netip.Addr
	GIAddr    netip.Addr
	CHAddr    [6]byte
	SName     string
	File      string
	Options   map[OptionCode][]byte
}

// Type returns option 53; ok is false if absent or malformed.
func (p *Packet) Type() (MessageType, bool) {
	v, ok := p.Options[OptMessageType]
	if !ok || len(v) != 1 {
		return 0, false
	}
	return MessageType(v[0]), true
}

// OptionIPv4 returns a 4-byte IPv4 option such as 50/54.
func (p *Packet) OptionIPv4(code OptionCode) (netip.Addr, bool) {
	v, ok := p.Options[code]
	if !ok || len(v) != 4 {
		return netip.Addr{}, false
	}
	return IPv4(v[0], v[1], v[2], v[3]), true
}

// LeaseDuration returns option 51 interpreted as seconds.
func (p *Packet) LeaseDuration() (uint32, bool) {
	v, ok := p.Options[OptLeaseTime]
	if !ok || len(v) != 4 {
		return 0, false
	}
	return uint32(v[0])<<24 | uint32(v[1])<<16 | uint32(v[2])<<8 | uint32(v[3]), true
}

// ClientID returns the raw option 61 value if present (len>=2 per RFC),
// else nil.
func (p *Packet) ClientID() []byte {
	v, ok := p.Options[OptClientID]
	if !ok || len(v) < 2 {
		return nil
	}
	return v
}

// IPv4 constructs an IPv4 netip.Addr from four bytes.
func IPv4(a, b, c, d byte) netip.Addr {
	return netip.AddrFrom4([4]byte{a, b, c, d})
}

// Addr4 unwraps an IPv4 address into four bytes; panics on non-IPv4,
// which is a programming error in this package.
func Addr4(a netip.Addr) [4]byte {
	if !a.Is4() {
		panic("dhcp4: not an IPv4 address: " + a.String())
	}
	return a.As4()
}

// ParseError is a classified message-parse/validation failure. Code is a
// stable machine-readable category; Reason is the human-readable detail.
type ParseError struct {
	Code   string
	Reason string
}

func (e *ParseError) Error() string { return e.Code + ": " + e.Reason }

func parseErr(code, format string, a ...any) error {
	return &ParseError{Code: code, Reason: fmt.Sprintf(format, a...)}
}

// Stable parse/validation error categories.
const (
	ErrTooShort          = "message_too_short"
	ErrBadCookie         = "bad_magic_cookie"
	ErrBadOp             = "bad_op"
	ErrBadHType          = "bad_htype_hlen"
	ErrTruncatedOption   = "truncated_option"
	ErrOptionTooLong     = "option_exceeds_message"
	ErrMissingMsgType    = "missing_message_type"
	ErrMalformedMsgType  = "malformed_message_type"
	ErrUnsupportedMsg    = "unsupported_message_type"
	ErrRelayNotSupported = "relay_not_supported"
	ErrBadServerID       = "malformed_server_id"
	ErrBadRequestedIP    = "malformed_requested_ip"
	ErrBadClientID       = "malformed_client_id"
	ErrBadCHAddr         = "missing_chaddr"
)

// Unmarshal parses one BOOTP message. It validates framing and the
// options needed by the subset, but does NOT interpret protocol state.
func Unmarshal(b []byte) (*Packet, error) {
	if len(b) < MinMessageLen {
		return nil, parseErr(ErrTooShort, "got %d bytes, need at least %d", len(b), MinMessageLen)
	}
	if b[0] != OpBootRequest && b[0] != OpBootReply {
		return nil, parseErr(ErrBadOp, "op=%d, want 1 (BOOTREQUEST) or 2 (BOOTREPLY)", b[0])
	}
	p := &Packet{
		Op:    b[0],
		HType: b[1],
		HLen:  b[2],
		Hops:  b[3],
	}
	copy(p.XID[:], b[4:8])
	p.Secs = uint16(b[8])<<8 | uint16(b[9])
	p.Broadcast = b[10]&0x80 != 0
	// b[11], b[12..15] flags/ciaddr-reserved handled below
	p.CIAddr = readAddr(b, 12)
	p.YIAddr = readAddr(b, 20)
	p.SIAddr = readAddr(b, 24)
	p.GIAddr = readAddr(b, 28)
	// chaddr field is 16 bytes; Ethernet uses the first 6.
	if p.HType == 1 && p.HLen == 6 {
		copy(p.CHAddr[:], b[44:50])
		if p.CHAddr == [6]byte{} {
			return nil, parseErr(ErrBadCHAddr, "htype Ethernet but chaddr is all zeros")
		}
	} else {
		return nil, parseErr(ErrBadHType, "only Ethernet (htype=1,hlen=6) is supported, got htype=%d hlen=%d", p.HType, p.HLen)
	}
	p.SName = nulTrim(b[48 : 48+64])
	p.File = nulTrim(b[108 : 108+128])

	if [4]byte{b[236], b[237], b[238], b[239]} != MagicCookie {
		return nil, parseErr(ErrBadCookie, "vendor magic cookie is % x", b[236:240])
	}

	p.Options = map[OptionCode][]byte{}
	i := 240
	for i < len(b) {
		code := OptionCode(b[i])
		i++
		if code == OptPad {
			continue
		}
		if code == OptEnd {
			break
		}
		if i >= len(b) {
			return nil, parseErr(ErrTruncatedOption, "option %s has no length byte", code)
		}
		l := int(b[i])
		i++
		if i+l > len(b) {
			return nil, parseErr(ErrOptionTooLong, "option %s length %d runs past message (%d bytes)", code, l, len(b))
		}
		// First occurrence wins, matching most DHCP stacks; duplicate
		// options are not meaningful in this subset.
		if _, seen := p.Options[code]; !seen {
			v := make([]byte, l)
			copy(v, b[i:i+l])
			p.Options[code] = v
		}
		i += l
	}

	if err := validateSubset(p); err != nil {
		return nil, err
	}
	return p, nil
}

// validateSubset checks the semantic option shapes the server relies on.
func validateSubset(p *Packet) error {
	mt, ok := p.Options[OptMessageType]
	if !ok {
		return parseErr(ErrMissingMsgType, "option 53 absent")
	}
	if len(mt) != 1 {
		return parseErr(ErrMalformedMsgType, "option 53 has %d bytes, want 1", len(mt))
	}
	t := MessageType(mt[0])
	if !t.InSubset() {
		return parseErr(ErrUnsupportedMsg, "type %s is outside the implemented subset", t)
	}
	if v, ok := p.Options[OptServerID]; ok && len(v) != 4 {
		return parseErr(ErrBadServerID, "option 54 has %d bytes, want 4", len(v))
	}
	if v, ok := p.Options[OptRequestedIP]; ok && len(v) != 4 {
		return parseErr(ErrBadRequestedIP, "option 50 has %d bytes, want 4", len(v))
	}
	if v, ok := p.Options[OptClientID]; ok && len(v) < 2 {
		return parseErr(ErrBadClientID, "option 61 must be >=2 bytes (type+value), got %d", len(v))
	}
	// Relay-agent forwarding (giaddr set) is outside this local subset:
	// reject it explicitly rather than answering as if the client were on
	// the local link.
	if p.GIAddr.IsValid() {
		return parseErr(ErrRelayNotSupported, "giaddr=%s; relayed BOOTP is not supported", p.GIAddr)
	}
	return nil
}

// Marshal encodes a BOOTP reply (or request, for fixtures). Options are
// emitted in a fixed canonical order; the message is zero-padded to at
// least MinReplyLen bytes.
func (p *Packet) Marshal() ([]byte, error) {
	if p.Op != OpBootRequest && p.Op != OpBootReply {
		return nil, fmt.Errorf("dhcp4: cannot marshal op=%d", p.Op)
	}
	if p.HType == 0 {
		p.HType = 1
	}
	if p.HLen == 0 {
		p.HLen = 6
	}
	b := make([]byte, MinReplyLen)
	b[0] = p.Op
	b[1] = p.HType
	b[2] = p.HLen
	b[3] = p.Hops
	copy(b[4:8], p.XID[:])
	b[8] = byte(p.Secs >> 8)
	b[9] = byte(p.Secs)
	if p.Broadcast {
		b[10] = 0x80
	}
	putAddr(b[12:16], p.CIAddr)
	putAddr(b[16:20], netip.Addr{})
	putAddr(b[20:24], p.YIAddr)
	putAddr(b[24:28], p.SIAddr)
	putAddr(b[28:32], p.GIAddr)
	copy(b[44:50], p.CHAddr[:])
	copy(b[48:112], []byte(p.SName))
	copy(b[108:236], []byte(p.File))
	copy(b[236:240], MagicCookie[:])

	// Options are emitted in a fixed canonical order: 53 first, then
	// configuration options, then identification options.
	ordered := make([]struct {
		code OptionCode
		v    []byte
	}, 0, len(p.Options)+1)
	if mt, ok := p.Type(); ok {
		ordered = append(ordered, struct {
			code OptionCode
			v    []byte
		}{OptMessageType, []byte{byte(mt)}})
	}
	for _, code := range []OptionCode{
		OptSubnetMask, OptRouter, OptDNSServer, OptLeaseTime,
		OptServerID, OptRequestedIP, OptClientID, OptHostname,
		OptParameterRequest,
	} {
		if v, ok := p.Options[code]; ok && len(v) > 0 {
			ordered = append(ordered, struct {
				code OptionCode
				v    []byte
			}{code, v})
		}
	}
	optLen := 1 // END
	for _, o := range ordered {
		optLen += 2 + len(o.v)
	}
	needed := 240 + optLen
	if len(b) < needed {
		b = append(b, make([]byte, needed-len(b))...)
	}
	pos := 240
	for _, o := range ordered {
		b[pos] = byte(o.code)
		b[pos+1] = byte(len(o.v))
		copy(b[pos+2:], o.v)
		pos += 2 + len(o.v)
	}
	b[pos] = byte(OptEnd)
	pos++
	return b[:max(pos, MinReplyLen)], nil
}

func readAddr(b []byte, off int) netip.Addr {
	v := b[off : off+4]
	if v[0]|v[1]|v[2]|v[3] == 0 {
		return netip.Addr{}
	}
	return IPv4(v[0], v[1], v[2], v[3])
}

func putAddr(b []byte, a netip.Addr) {
	if !a.IsValid() {
		return
	}
	v := Addr4(a)
	copy(b, v[:])
}

func nulTrim(b []byte) string {
	for i, c := range b {
		if c == 0 {
			return string(b[:i])
		}
	}
	return string(b)
}

// ClientIdentity is the RFC 2131 §4.2 client identifier: option 61 when
// present, otherwise the htype/chaddr pair.
type ClientIdentity struct {
	// OptionID is the raw option-61 value (type byte + value) when the
	// client used option 61; nil for chaddr-based identity.
	OptionID []byte
	HType    uint8
	CHAddr   [6]byte
}

// IdentityOf extracts the client identity from a parsed request.
func IdentityOf(p *Packet) ClientIdentity {
	id := ClientIdentity{HType: p.HType, CHAddr: p.CHAddr}
	if v := p.ClientID(); v != nil {
		id.OptionID = append([]byte(nil), v...)
	}
	return id
}

// Key renders a stable comparable identity string. Option-61 values get a
// namespace prefix that can never collide with the chaddr form.
func (c ClientIdentity) Key() string {
	if c.OptionID != nil {
		return "oid:" + fmt.Sprintf("%x", c.OptionID)
	}
	return fmt.Sprintf("mac:%d:%02x:%02x:%02x:%02x:%02x:%02x", c.HType,
		c.CHAddr[0], c.CHAddr[1], c.CHAddr[2], c.CHAddr[3], c.CHAddr[4], c.CHAddr[5])
}

// IsZero reports an unset identity (no option-61 and zero chaddr).
func (c ClientIdentity) IsZero() bool {
	return c.OptionID == nil && c.CHAddr == [6]byte{}
}

// ErrNotIPv4 is returned by helpers handed a non-IPv4 value.
var ErrNotIPv4 = errors.New("address is not IPv4")
