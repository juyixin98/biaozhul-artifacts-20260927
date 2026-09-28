package server

import (
	"context"
	"net/netip"
	"path/filepath"
	"testing"
	"time"

	"dhcpv4lab/internal/config"
	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

type harness struct {
	t     *testing.T
	srv   *Server
	store *storage.Store
	clock *FakeClock
	cfg   config.Config
	runID string
}

func newHarness(t *testing.T, poolStart, poolEnd string) *harness {
	t.Helper()
	cfg := config.Default()
	cfg.Store.DSN = "file:" + filepath.Join(t.TempDir(), "h.db")
	cfg.Store.Reset = true
	cfg.Pool.RangeStart = poolStart
	cfg.Pool.RangeEnd = poolEnd
	cfg.Pool.ServerID = "127.0.0.1"
	cfg.Pool.Netmask = "255.0.0.0"
	cfg.Pool.Router = "127.0.0.1"
	cfg.Pool.DNS = []string{"127.0.0.1"}
	cfg.Lease.LeaseTime = config.Duration{Duration: 10 * time.Second}
	cfg.Lease.T1 = config.Duration{Duration: 4 * time.Second}
	cfg.Lease.T2 = config.Duration{Duration: 8 * time.Second}
	cfg.Lease.OfferTTL = config.Duration{Duration: 3 * time.Second}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("config invalid: %v", err)
	}
	st, err := storage.Open(cfg.Store.DSN, true)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	clk := NewFakeClock(time.Unix(1_700_000_000, 0))
	srv, err := New(cfg, st, clk)
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	return &harness{t: t, srv: srv, store: st, clock: clk, cfg: cfg, runID: "unit-run"}
}

func (h *harness) now() time.Time { return h.clock.Now() }

func (h *harness) send(p *dhcppacket.Packet) *Outcome {
	h.t.Helper()
	return h.srv.Handle(context.Background(), p, Source{RemoteAddr: "127.0.0.1:0", RunID: h.runID})
}

// client is a synthetic client identity with MAC, optional client-id and a
// running xid counter. Packets are built through the shared wire builder but
// every expectation below is asserted independently by the test.
type client struct {
	n   int
	mac [6]byte
	cid []byte
}

func newClient(id byte) *client {
	c := &client{mac: [6]byte{0x02, 0, 0, 0, 0, id}}
	c.cid = append([]byte{1}, c.mac[:]...)
	return c
}

func (c *client) hardwareAddr() []byte { return c.mac[:] }

func (c *client) nextXID() uint32 {
	c.n++
	return uint32(int(c.mac[5])<<24 | c.n)
}

func (c *client) discover(h *harness) *Outcome {
	b := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgDiscover).ClientID(c.cid).
		Params(dhcppacket.OptSubnetMask, dhcppacket.OptRouter, dhcppacket.OptDNSServer)
	return h.send(b.Packet())
}

func (c *client) discoverRaw(h *harness) (*dhcppacket.Packet, *Outcome) {
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgDiscover).ClientID(c.cid).
		Params(dhcppacket.OptSubnetMask, dhcppacket.OptRouter, dhcppacket.OptDNSServer).
		Packet()
	return p, h.send(p)
}

func (c *client) requestSelect(h *harness, ip string) *Outcome {
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).
		RequestedIP(mustAddr(ip)).ServerID(mustAddr("127.0.0.1")).
		Packet()
	return h.send(p)
}

// requestSelectXID reuses an explicit xid so a retransmission is byte-equal.
func (c *client) requestSelectXID(h *harness, xid uint32, ip string) *Outcome {
	p := dhcppacket.NewRequest(xid, c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).
		RequestedIP(mustAddr(ip)).ServerID(mustAddr("127.0.0.1")).
		Packet()
	return h.send(p)
}

func (c *client) requestReboot(h *harness, ip string) *Outcome {
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).
		RequestedIP(mustAddr(ip)).
		Packet()
	return h.send(p)
}

func (c *client) requestRenew(h *harness, ip string) *Outcome {
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).
		CIAddr(mustAddr(ip)).
		Packet()
	return h.send(p)
}

func (c *client) release(h *harness, ip string) *Outcome {
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRelease).ClientID(c.cid).
		CIAddr(mustAddr(ip)).
		Packet()
	return h.send(p)
}

func mustAddr(s string) netip.Addr {
	a, err := netip.ParseAddr(s)
	if err != nil {
		panic(err)
	}
	return a.Unmap()
}

func decodeReply(t *testing.T, o *Outcome, wantType byte) *dhcppacket.Packet {
	t.Helper()
	if o == nil || len(o.Reply) == 0 {
		t.Fatalf("expected reply type %d, got outcome=%+v", wantType, o)
	}
	p, err := dhcppacket.Decode(o.Reply)
	if err != nil {
		t.Fatalf("decode reply: %v", err)
	}
	mt, err := p.MessageType()
	if err != nil || mt != wantType {
		t.Fatalf("reply type=%d want %d err=%v", mt, wantType, err)
	}
	return p
}

func (h *harness) activeLease(t *testing.T, c *client) *storage.Lease {
	t.Helper()
	ident := storage.IdentityFromClientID(c.cid)
	l, err := h.store.ActiveLease(context.Background(), ident.Key)
	if err != nil {
		t.Fatalf("active lease: %v", err)
	}
	return l
}
