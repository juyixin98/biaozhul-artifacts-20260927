package dhcppacket

import (
	"net"
	"net/netip"
	"sort"
)

// ReplyFor builds a BOOTREPLY envelope mirroring a client request, then lets
// the caller set message-specific fields. The server uses this for all
// OFFER/ACK/NAK answers.
func ReplyFor(req *Packet, msgType byte) *Packet {
	rep := &Packet{
		Op:            OpBootReply,
		HType:         req.HType,
		HLen:          req.HLen,
		Hops:          req.Hops,
		XID:           req.XID,
		Secs:          req.Secs,
		BroadcastFlag: req.BroadcastFlag,
		SIAddr:        req.SIAddr,
		GIAddr:        req.GIAddr,
		CHAddr:        append([]byte(nil), req.CHAddr...),
		Options:       map[byte][]byte{OptMessageType: {msgType}},
	}
	return rep
}

// SetUInt32 stores a 4-byte big-endian option (lease time, T1, T2).
func (p *Packet) SetUInt32(code byte, v uint32) {
	b := make([]byte, 4)
	binaryBigEndianPut32(b, v)
	p.Options[code] = b
}

// SetIP stores a 4-byte IPv4 option.
func (p *Packet) SetIP(code byte, a netip.Addr) { p.Options[code] = addrToByte4(a) }

// SetIPList stores a concatenated list of IPv4 option values.
func (p *Packet) SetIPList(code byte, addrs []netip.Addr) {
	var b []byte
	for _, a := range addrs {
		b = append(b, addrToByte4(a)...)
	}
	p.Options[code] = b
}

func optionCodesSorted(m map[byte][]byte) []byte {
	codes := make([]byte, 0, len(m))
	for c := range m {
		codes = append(codes, c)
	}
	sort.Slice(codes, func(i, j int) bool { return codes[i] < codes[j] })
	return codes
}

func binaryBigEndianPut32(b []byte, v uint32) {
	b[0] = byte(v >> 24)
	b[1] = byte(v >> 16)
	b[2] = byte(v >> 8)
	b[3] = byte(v)
}

func byte4ToAddr(b []byte) netip.Addr {
	if len(b) != 4 {
		return netip.Addr{}
	}
	return netip.AddrFrom4([4]byte{b[0], b[1], b[2], b[3]})
}

func addrToByte4(a netip.Addr) []byte {
	a = a.Unmap()
	if !a.Is4() {
		return nil
	}
	v := a.As4()
	return v[:]
}

func putAddr(dst []byte, a netip.Addr) {
	if a.IsValid() {
		a = a.Unmap()
		if a.Is4() {
			v := a.As4()
			copy(dst, v[:])
		}
	}
}

func allZero(b []byte) bool {
	for _, x := range b {
		if x != 0 {
			return false
		}
	}
	return true
}

// ParseMAC is a thin wrapper so callers do not need the net package directly.
func ParseMAC(s string) (net.HardwareAddr, error) { return net.ParseMAC(s) }
