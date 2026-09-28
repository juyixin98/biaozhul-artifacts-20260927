package dhcp4_test

import (
	"encoding/hex"
	"testing"

	"dhcp4lab/internal/dhcp4"

	"dhcp4lab/testfixture/wirekit"
)

// TestCodecRoundTrip builds messages with the INDEPENDENT wirekit builder
// and parses them with the implementation parser, then re-marshals with
// the implementation and re-parses with the independent reference
// parser — a cross-codec check, not self-validation.
func TestCodecRoundTrip(t *testing.T) {
	xid := [4]byte{0x10, 0x22, 0x33, 0x44}
	mac := wirekit.MAC("02:00:00:00:00:01")
	cases := []struct {
		name string
		raw  []byte
		mt   dhcp4.MessageType
	}{
		{
			name: "discover",
			raw: wirekit.NewRequest(xid, mac).MsgType(wirekit.MTDiscover).
				ClientID([]byte{1, 2, 3, 4, 5, 6, 7}).
				ParamRequest(wirekit.OptSubnetMask, wirekit.OptRouter).Build(),
			mt: dhcp4.MsgDiscover,
		},
		{
			name: "request_select",
			raw: wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
				RequestedIP(wirekit.IP("192.0.2.10")).
				ServerID(wirekit.IP("192.0.2.1")).Build(),
			mt: dhcp4.MsgRequest,
		},
		{
			name: "release",
			raw: wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRelease).
				CIAddr(wirekit.IP("192.0.2.10")).Build(),
			mt: dhcp4.MsgRelease,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, err := dhcp4.Unmarshal(tc.raw)
			if err != nil {
				t.Fatalf("impl parse: %v", err)
			}
			got, ok := p.Type()
			if !ok || got != tc.mt {
				t.Fatalf("type = %v ok=%v, want %v", got, ok, tc.mt)
			}
			if p.XID != xid {
				t.Fatalf("xid = %x, want %x", p.XID, xid)
			}
			if p.CHAddr != mac {
				t.Fatalf("chaddr = %x, want %x", p.CHAddr, mac)
			}

			// Implementation re-encodes (as a reply), reference parser
			// must still accept it.
			p.Op = dhcp4.OpBootReply
			out, err := p.Marshal()
			if err != nil {
				t.Fatalf("impl marshal: %v", err)
			}
			if len(out) < wirekit.MinReplyLen {
				t.Fatalf("reply %d bytes < %d minimum", len(out), wirekit.MinReplyLen)
			}
			ref, err := wirekit.Parse(out)
			if err != nil {
				t.Fatalf("reference parser rejects impl reply: %v", err)
			}
			if ref.Op != wirekit.OpBootReply {
				t.Fatalf("ref op = %d, want BOOTREPLY", ref.Op)
			}
			if mt, _ := ref.MsgType(); mt != byte(tc.mt) {
				t.Fatalf("ref type = %d, want %d", mt, tc.mt)
			}
			if [4]byte(ref.XID) != xid {
				t.Fatalf("ref xid mismatch: %x", ref.XID)
			}
		})
	}
}

// TestParseFailures asserts the exact stable category for each class of
// malformed datagram. The server must reject these, never parse them as a
// valid message.
func TestParseFailures(t *testing.T) {
	xid := [4]byte{1, 2, 3, 4}
	mac := wirekit.MAC("02:00:00:00:00:09")
	valid := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTDiscover).Build()

	good, err := dhcp4.Unmarshal(valid)
	if err != nil {
		t.Fatalf("baseline valid packet failed: %v", err)
	}
	if _, ok := good.Type(); !ok {
		t.Fatal("baseline missing type")
	}

	cases := []struct {
		name     string
		mutate   func(b []byte) []byte
		wantCode string
	}{
		{"truncated", func(b []byte) []byte { return b[:100] }, dhcp4.ErrTooShort},
		{"bad_cookie", func(b []byte) []byte { b[236] ^= 0xFF; return b }, dhcp4.ErrBadCookie},
		{"bad_op", func(b []byte) []byte { b[0] = 9; return b }, dhcp4.ErrBadOp},
		{"unsupported_type", func(b []byte) []byte {
			p := wirekit.FromPacket(mustParseRef(t, b)).MsgType(wirekit.MTInform).Build()
			return p
		}, dhcp4.ErrUnsupportedMsg},
		{"missing_type", func(b []byte) []byte {
			p := wirekit.FromPacket(mustParseRef(t, b)).DeleteOption(wirekit.OptMsgType).Build()
			return p
		}, dhcp4.ErrMissingMsgType},
		{"bad_htype", func(b []byte) []byte { b[1] = 6; return b }, dhcp4.ErrBadHType},
		{"option_overrun", func(b []byte) []byte {
			// overwrite the first option after cookie: code 53, length 200
			b[240], b[241] = 53, 200
			return b
		}, dhcp4.ErrOptionTooLong},
		{"zero_chaddr", func(b []byte) []byte {
			for i := 44; i < 50; i++ {
				b[i] = 0
			}
			return b
		}, dhcp4.ErrBadCHAddr},
		{"relay_giaddr", func(b []byte) []byte {
			return wirekit.FromPacket(mustParseRef(t, b)).
				GIAddr(wirekit.IP("192.0.2.254")).Build()
		}, dhcp4.ErrRelayNotSupported},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b := append([]byte(nil), valid...)
			b = tc.mutate(b)
			_, err := dhcp4.Unmarshal(b)
			if err == nil {
				t.Fatalf("expected parse error %s, got success", tc.wantCode)
			}
			pe, ok := err.(*dhcp4.ParseError)
			if !ok {
				t.Fatalf("error type %T, want *ParseError", err)
			}
			if pe.Code != tc.wantCode {
				t.Fatalf("category = %s, want %s (detail: %s)", pe.Code, tc.wantCode, pe.Reason)
			}
		})
	}
}

func mustParseRef(t *testing.T, b []byte) *wirekit.Packet {
	t.Helper()
	p, err := wirekit.Parse(b)
	if err != nil {
		t.Fatalf("reference parse: %v", err)
	}
	return p
}

// TestIdentityKey documents the XID+identity association keying.
func TestIdentityKey(t *testing.T) {
	mac := wirekit.MAC("02:00:00:00:00:0a")
	raw := wirekit.NewRequest([4]byte{9, 9, 9, 9}, mac).MsgType(wirekit.MTDiscover).Build()
	p, err := dhcp4.Unmarshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	id := dhcp4.IdentityOf(p)
	if got := id.Key(); got != "mac:1:02:00:00:00:00:0a" {
		t.Fatalf("chaddr key = %q", got)
	}

	raw2 := wirekit.NewRequest([4]byte{9, 9, 9, 9}, mac).MsgType(wirekit.MTDiscover).
		ClientID([]byte{0x01, 0xaa, 0xbb}).Build()
	p2, _ := dhcp4.Unmarshal(raw2)
	id2 := dhcp4.IdentityOf(p2)
	if got := id2.Key(); got != "oid:01aabb" {
		t.Fatalf("option61 key = %q, want oid:01aabb", got)
	}
	if id.Key() == id2.Key() {
		t.Fatal("chaddr and option61 identities must not collide")
	}
}

// TestHexOfBaseline keeps a human-readable reference capture handy for
// debugging replay scripts.
func TestHexOfBaseline(t *testing.T) {
	b := wirekit.NewRequest([4]byte{0xab, 0xcd, 0, 1}, wirekit.MAC("02:00:00:00:00:ff")).
		MsgType(wirekit.MTDiscover).Build()
	if len(hex.EncodeToString(b)) == 0 {
		t.Fatal("empty capture")
	}
}
