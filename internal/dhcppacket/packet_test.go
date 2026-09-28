package dhcppacket

import (
	"bytes"
	"encoding/binary"
	"net"
	"net/netip"
	"testing"
)

// buildRawVector constructs a packet byte-by-byte WITHOUT using the package
// builder/encoder. These vectors are the independent ground truth the decoder
// is checked against.
func buildRawVector(t *testing.T, xid uint32, mac net.HardwareAddr, msgType byte, extraOpts ...[]byte) []byte {
	t.Helper()
	buf := make([]byte, 300)
	buf[0] = OpBootRequest
	buf[1] = 1 // ethernet
	buf[2] = 6
	copy(buf[4:8], be32(xid))
	binary.BigEndian.PutUint16(buf[8:10], 7) // secs
	binary.BigEndian.PutUint16(buf[10:12], 0)
	copy(buf[28:34], mac)
	copy(buf[236:240], MagicCookie[:])
	pos := 240
	buf[pos] = OptMessageType
	buf[pos+1] = 1
	buf[pos+2] = msgType
	pos += 3
	for _, o := range extraOpts {
		copy(buf[pos:], o)
		pos += len(o)
	}
	buf[pos] = OptEnd
	return buf[:pos+1]
}

func be32(v uint32) []byte {
	b := make([]byte, 4)
	binary.BigEndian.PutUint32(b, v)
	return b
}

func opt50(ip string) []byte {
	a := netip.MustParseAddr(ip).As4()
	return append([]byte{OptRequestedIP, 4}, a[:]...)
}

func opt54(ip string) []byte {
	a := netip.MustParseAddr(ip).As4()
	return append([]byte{OptServerID, 4}, a[:]...)
}

func TestDecodeGroundTruthDiscover(t *testing.T) {
	mac, _ := ParseMAC("02:00:00:00:00:11")
	raw := buildRawVector(t, 0x11223344, mac, MsgDiscover)
	p, err := Decode(raw)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if p.Op != OpBootRequest || p.HType != 1 || p.HLen != 6 {
		t.Fatalf("header wrong: op=%d htype=%d hlen=%d", p.Op, p.HType, p.HLen)
	}
	if p.XID != 0x11223344 {
		t.Fatalf("xid=%x", p.XID)
	}
	if p.Secs != 7 {
		t.Fatalf("secs=%d", p.Secs)
	}
	if !bytes.Equal(p.CHAddr, mac) {
		t.Fatalf("chaddr=% x want % x", p.CHAddr, mac)
	}
	mt, err := p.MessageType()
	if err != nil || mt != MsgDiscover {
		t.Fatalf("message type=%d err=%v", mt, err)
	}
}

func TestDecodeReadsRequestOptions(t *testing.T) {
	mac, _ := ParseMAC("02:00:00:00:00:22")
	raw := buildRawVector(t, 55, mac, MsgRequest,
		opt50("127.10.0.5"), opt54("127.0.0.1"))
	p, err := Decode(raw)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	rip, ok := p.RequestedIP()
	if !ok || rip.String() != "127.10.0.5" {
		t.Fatalf("requested ip=%v ok=%v", rip, ok)
	}
	sid, ok := p.ServerID()
	if !ok || sid.String() != "127.0.0.1" {
		t.Fatalf("server id=%v ok=%v", sid, ok)
	}
}

func TestDecodeRejectsMalformed(t *testing.T) {
	mac, _ := ParseMAC("02:00:00:00:00:33")
	base := buildRawVector(t, 1, mac, MsgDiscover)

	cases := []struct {
		name   string
		mutate func(b []byte) []byte
	}{
		{"too_short", func(b []byte) []byte { return b[:10] }},
		{"bad_op", func(b []byte) []byte { c := clone(b); c[0] = 9; return c }},
		{"bad_cookie", func(b []byte) []byte { c := clone(b); c[236] ^= 0xFF; return c }},
		{"hlen_too_big", func(b []byte) []byte { c := clone(b); c[2] = 20; return c }},
		{"ethernet_hlen_5", func(b []byte) []byte { c := clone(b); c[2] = 5; return c }},
		{"zero_chaddr", func(b []byte) []byte {
			c := clone(b)
			for i := 28; i < 34; i++ {
				c[i] = 0
			}
			return c
		}},
		{"option_overrun", func(b []byte) []byte {
			c := clone(b)
			// option claims 200 bytes but only ten zero bytes + END follow.
			head := append([]byte{}, c[:243]...)
			head = append(head, OptRouter, 200)
			tail := make([]byte, 10)
			tail = append(tail, OptEnd)
			return append(head, tail...)
		}},
		{"missing_end", func(b []byte) []byte { return b[:len(b)-1] }},
		{"junk_after_end", func(b []byte) []byte { c := clone(b); c = append(c, 7, 0); return c }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bad := tc.mutate(base)
			if _, err := Decode(bad); err == nil {
				t.Fatalf("expected ParseError for %s", tc.name)
			} else if pe, ok := err.(*ParseError); !ok {
				t.Fatalf("error type %T, want *ParseError", err)
			} else if pe.Reason == "" {
				t.Fatalf("empty parse error reason")
			}
		})
	}
}

func TestMissingMessageType(t *testing.T) {
	buf := make([]byte, 241)
	buf[0] = OpBootRequest
	buf[1] = 1
	buf[2] = 6
	copy(buf[4:8], be32(9))
	copy(buf[28:34], []byte{1, 2, 3, 4, 5, 6})
	copy(buf[236:240], MagicCookie[:])
	buf[240] = OptEnd
	p, err := Decode(buf)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, err := p.MessageType(); err == nil {
		t.Fatal("expected error for missing option 53")
	}
}

func TestRoundTripBuilder(t *testing.T) {
	mac, _ := ParseMAC("02:aa:bb:cc:dd:ee")
	reqIP := netip.MustParseAddr("127.10.0.9")
	sid := netip.MustParseAddr("127.0.0.1")
	raw := NewRequest(0x99887766, mac).
		Type(MsgRequest).RequestedIP(reqIP).ServerID(sid).
		ClientID(append([]byte{1}, mac...)).Params(OptSubnetMask, OptRouter, OptDNSServer).
		Bytes()
	p, err := Decode(raw)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if p.XID != 0x99887766 {
		t.Fatalf("xid mismatch")
	}
	if rip, _ := p.RequestedIP(); rip != reqIP {
		t.Fatalf("requested ip mismatch: %v", rip)
	}
	if got, _ := p.ServerID(); got != sid {
		t.Fatalf("server id mismatch: %v", got)
	}
	if cid := p.ClientID(); len(cid) != 7 || cid[0] != 1 {
		t.Fatalf("client id wrong: % x", cid)
	}
}

func TestReplyForMirrorsEnvelope(t *testing.T) {
	mac, _ := ParseMAC("02:00:00:00:be:ef")
	req := NewRequest(42, mac).Broadcast(true).Type(MsgDiscover).Packet()
	rep := ReplyFor(req, MsgOffer)
	if rep.Op != OpBootReply || rep.XID != 42 || !rep.BroadcastFlag {
		t.Fatalf("reply envelope not mirrored")
	}
	if !bytes.Equal(rep.CHAddr, mac) {
		t.Fatalf("reply chaddr differs")
	}
	raw, err := rep.Encode()
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	dec, err := Decode(raw)
	if err != nil {
		t.Fatalf("decoded reply failed: %v", err)
	}
	if dec.Op != OpBootReply {
		t.Fatalf("decoded op wrong")
	}
	if mt, _ := dec.MessageType(); mt != MsgOffer {
		t.Fatalf("decoded type wrong: %d", mt)
	}
}

func clone(b []byte) []byte { return append([]byte(nil), b...) }
