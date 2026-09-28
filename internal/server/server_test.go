package server_test

import (
	"testing"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/server"
	"dhcp4lab/internal/storage"
	"dhcp4lab/testfixture/wirekit"
)

func pkt(t *testing.T, b []byte) *dhcp4.Packet {
	t.Helper()
	p, err := dhcp4.Unmarshal(b)
	if err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return p
}

func TestClassifyRequest(t *testing.T) {
	xid := [4]byte{1, 2, 3, 4}
	mac := wirekit.MAC("02:00:00:11:11:11")
	ip := wirekit.IP("192.0.2.10")
	sid := wirekit.IP("192.0.2.1")

	tests := []struct {
		name string
		raw  []byte
		want storage.RequestKind
	}{
		{"selecting",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
				RequestedIP(ip).ServerID(sid).Build(),
			storage.ReqSelecting},
		{"init_reboot",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
				RequestedIP(ip).Build(),
			storage.ReqInitReboot},
		{"renew",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
				CIAddr(ip).Build(),
			storage.ReqRenew},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			kind, err := server.ClassifyRequest(pkt(t, tc.raw))
			if err != nil {
				t.Fatalf("classify: %v", err)
			}
			if kind != tc.want {
				t.Fatalf("kind = %s, want %s", kind, tc.want)
			}
		})
	}
}

func TestClassifyRequestMalformed(t *testing.T) {
	xid := [4]byte{1, 2, 3, 4}
	mac := wirekit.MAC("02:00:00:22:22:22")
	ip := wirekit.IP("192.0.2.10")
	sid := wirekit.IP("192.0.2.1")

	tests := []struct {
		name string
		raw  []byte
	}{
		{"server_id_without_50",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).ServerID(sid).Build()},
		{"server_id_with_ciaddr",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
				RequestedIP(ip).ServerID(sid).CIAddr(ip).Build()},
		{"50_with_ciaddr",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
				RequestedIP(ip).CIAddr(ip).Build()},
		{"nothing",
			wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).Build()},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := server.ClassifyRequest(pkt(t, tc.raw)); err == nil {
				t.Fatal("expected malformed classification error")
			}
		})
	}
}
