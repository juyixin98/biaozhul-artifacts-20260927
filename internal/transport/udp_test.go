package transport

import (
	"context"
	"encoding/binary"
	"log/slog"
	"net"
	"net/netip"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"dhcpv4lab/internal/config"
	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/server"
	"dhcpv4lab/internal/storage"
)

type udpEnv struct {
	t     *testing.T
	srv   *server.Server
	udp   *UDP
	store *storage.Store
	clock *server.FakeClock
	cfg   config.Config
	addr  *net.UDPAddr
	runID string
}

func startUDPEnv(t *testing.T, poolStart, poolEnd string) *udpEnv {
	t.Helper()
	cfg := config.Default()
	cfg.Store.DSN = "file:" + filepath.Join(t.TempDir(), "udp.db")
	cfg.Store.Reset = true
	cfg.Server.UDPListen = "127.0.0.1:0"
	cfg.Server.HTTPListen = "127.0.0.1:0"
	cfg.Pool.RangeStart = poolStart
	cfg.Pool.RangeEnd = poolEnd
	cfg.Lease.LeaseTime = config.Duration{Duration: 20 * time.Second}
	cfg.Lease.T1 = config.Duration{Duration: 8 * time.Second}
	cfg.Lease.T2 = config.Duration{Duration: 16 * time.Second}
	cfg.Lease.OfferTTL = config.Duration{Duration: 5 * time.Second}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	st, err := storage.Open(cfg.Store.DSN, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	clk := server.NewFakeClock(time.Unix(1_700_000_000, 0))
	srv, err := server.New(cfg, st, clk)
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(testWriter{t}, &slog.HandlerOptions{Level: slog.LevelWarn}))
	udp, err := ListenUDP("127.0.0.1:0", srv, log, "udp-it", 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = udp.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = udp.Serve(ctx) }()

	// Give the serving goroutine a bound socket (ListenUDP binds synchronously).
	return &udpEnv{t: t, srv: srv, udp: udp, store: st, clock: clk, cfg: cfg,
		addr: udp.LocalAddr(), runID: "udp-it"}
}

type testWriter struct{ t *testing.T }

func (w testWriter) Write(p []byte) (int, error) {
	w.t.Logf("%s", p)
	return len(p), nil
}

func dialClient(t *testing.T) *net.UDPConn {
	t.Helper()
	// Bind an unconnected ephemeral loopback socket (no Dial — that would
	// require the peer address up front).
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func (e *udpEnv) send(t *testing.T, c *net.UDPConn, raw []byte) {
	t.Helper()
	if _, err := c.WriteToUDP(raw, e.addr); err != nil {
		t.Fatal(err)
	}
}

func readReply(t *testing.T, c *net.UDPConn) ([]byte, *net.UDPAddr) {
	t.Helper()
	if err := c.SetReadDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 1500)
	n, src, err := c.ReadFromUDP(buf)
	if err != nil {
		t.Fatalf("read reply: %v", err)
	}
	return append([]byte(nil), buf[:n]...), src
}

func expectNoReply(t *testing.T, c *net.UDPConn) {
	t.Helper()
	if err := c.SetReadDeadline(time.Now().Add(400 * time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 1500)
	n, _, err := c.ReadFromUDP(buf)
	if err == nil {
		t.Fatalf("expected silence, got %d bytes: %x", n, buf[:n])
	}
}

func macAt(id byte) net.HardwareAddr { return net.HardwareAddr{0x02, 0, 0, 0, 0, id} }
func cidAt(id byte) []byte           { return append([]byte{1}, macAt(id)...) }

func TestUDP_DORAOverRealSocket(t *testing.T) {
	env := startUDPEnv(t, "127.20.0.2", "127.20.0.20")
	c := dialClient(t)
	defer c.Close()

	xid := uint32(0x444f5241)
	disc := dhcppacket.NewRequest(xid, macAt(1)).
		Type(dhcppacket.MsgDiscover).ClientID(cidAt(1)).
		Params(dhcppacket.OptSubnetMask, dhcppacket.OptRouter).Bytes()
	env.send(t, c, disc)
	offerRaw, src := readReply(t, c)
	if src.IP.String() != "127.0.0.1" {
		t.Fatalf("offer source ip=%s want 127.0.0.1", src.IP)
	}
	offer, err := dhcppacket.Decode(offerRaw)
	if err != nil {
		t.Fatal(err)
	}
	if mt, _ := offer.MessageType(); mt != dhcppacket.MsgOffer || offer.XID != xid {
		t.Fatalf("offer type/xid: %d %d", mt, offer.XID)
	}
	if offer.YIAddr.String() != "127.20.0.2" {
		t.Fatalf("offer yiaddr=%s", offer.YIAddr)
	}
	sid, ok := offer.ServerID()
	if !ok || sid.String() != "127.0.0.1" {
		t.Fatalf("offer server-id=%v ok=%v", sid, ok)
	}
	if !bytesEqual(offer.CHAddr, macAt(1)) {
		t.Fatal("offer chaddr not mirrored")
	}

	req := dhcppacket.NewRequest(xid+1, macAt(1)).
		Type(dhcppacket.MsgRequest).ClientID(cidAt(1)).
		RequestedIP(mustParse("127.20.0.2")).ServerID(mustParse("127.0.0.1")).Bytes()
	env.send(t, c, req)
	ackRaw, _ := readReply(t, c)
	ack, _ := dhcppacket.Decode(ackRaw)
	if mt, _ := ack.MessageType(); mt != dhcppacket.MsgAck {
		t.Fatalf("expected ACK got %d", mt)
	}
	if ack.YIAddr.String() != "127.20.0.2" {
		t.Fatalf("ack yiaddr=%s", ack.YIAddr)
	}
	if lt := ack.Options[dhcppacket.OptLeaseTime]; binary.BigEndian.Uint32(lt) != 20 {
		t.Fatalf("lease time=%d want 20", binary.BigEndian.Uint32(lt))
	}

	// Retransmit the exact same REQUEST: must replay the same ACK, no extension.
	lease, _ := env.store.ActiveLease(context.Background(),
		storage.IdentityFromClientID(cidAt(1)).Key)
	endsBefore := lease.Ends
	env.clock.Advance(3_000_000_000)
	env.send(t, c, req)
	replayRaw, _ := readReply(t, c)
	if string(replayRaw) != string(ackRaw) {
		t.Fatal("retransmitted REQUEST produced different ACK bytes")
	}
	lease, _ = env.store.ActiveLease(context.Background(),
		storage.IdentityFromClientID(cidAt(1)).Key)
	if lease.Ends != endsBefore {
		t.Fatalf("retransmission extended lease: %d -> %d", endsBefore, lease.Ends)
	}
}

func TestUDP_MalformedDatagramSilent(t *testing.T) {
	env := startUDPEnv(t, "127.20.0.2", "127.20.0.5")
	c := dialClient(t)
	defer c.Close()
	env.send(t, c, []byte("this is not a dhcp packet"))
	expectNoReply(t, c)

	// Well-formed BOOTP but unsupported message type also yields no success.
	other := dhcppacket.NewRequest(7, macAt(9)).Type(dhcppacket.MsgDecline).
		ClientID(cidAt(9)).Bytes()
	env.send(t, c, other)
	expectNoReply(t, c)
}

func TestUDP_ConcurrentContention(t *testing.T) {
	// 4 clients race a 4-address pool end to end; every address must be
	// handed to exactly one client and every offer must commit exactly once.
	env := startUDPEnv(t, "127.21.0.2", "127.21.0.5")
	const n = 4
	type clientResult struct {
		id       byte
		offerIP  string
		offerOK  bool
		poolFull bool
		ackOK    bool
	}
	res := make([]clientResult, n)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := byte(0x10 + i)
			cc := dialClient(t)
			defer cc.Close()
			<-start
			r := clientResult{id: id}
			disc := dhcppacket.NewRequest(uint32(1000+i), macAt(id)).
				Type(dhcppacket.MsgDiscover).ClientID(cidAt(id)).Bytes()
			env.send(t, cc, disc)
			raw, _ := readReply(t, cc)
			p, err := dhcppacket.Decode(raw)
			if err != nil {
				t.Errorf("client %d offer decode: %v", id, err)
				return
			}
			mt, _ := p.MessageType()
			if mt == dhcppacket.MsgOffer {
				r.offerOK = true
				r.offerIP = p.YIAddr.String()
				req := dhcppacket.NewRequest(uint32(2000+i), macAt(id)).
					Type(dhcppacket.MsgRequest).ClientID(cidAt(id)).
					RequestedIP(p.YIAddr).ServerID(mustParse("127.0.0.1")).Bytes()
				env.send(t, cc, req)
				ackRaw, _ := readReply(t, cc)
				ack, aerr := dhcppacket.Decode(ackRaw)
				if aerr != nil {
					t.Errorf("ack decode: %v", aerr)
					return
				}
				if amt, _ := ack.MessageType(); amt == dhcppacket.MsgAck {
					r.ackOK = true
				}
			}
			res[i] = r
		}(i)
	}
	close(start)
	wg.Wait()

	got := map[string]int{}
	bound := 0
	for _, r := range res {
		if r.offerOK {
			got[r.offerIP]++
			if r.ackOK {
				bound++
			}
		}
	}
	if len(got) != n {
		t.Fatalf("distinct offered addresses=%v want exactly %d", got, n)
	}
	for ip, count := range got {
		if count != 1 {
			t.Fatalf("address %s offered %d times", ip, count)
		}
	}
	if bound != n {
		t.Fatalf("bound leases=%d want %d", bound, n)
	}
	_, nbound, err := env.store.ActiveAddressCount(context.Background())
	if err != nil || nbound != n {
		t.Fatalf("bound count=%d err=%v want %d", nbound, err, n)
	}
}

func TestUDP_PoolExhaustionThenReleaseReallocates(t *testing.T) {
	// 2-address pool, 3 clients: exactly two OFFERs and one explicit
	// pool-exhaustion silence; after one RELEASE the third client is served.
	env := startUDPEnv(t, "127.22.0.2", "127.22.0.3")
	clients := make([]*net.UDPConn, 3)
	for i := range clients {
		clients[i] = dialClient(t)
		defer clients[i].Close()
	}
	start := make(chan struct{})
	offers := make([]string, 3)
	silenced := make([]bool, 3)
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := byte(0x30 + i)
			<-start
			disc := dhcppacket.NewRequest(uint32(3000+i), macAt(id)).
				Type(dhcppacket.MsgDiscover).ClientID(cidAt(id)).Bytes()
			env.send(t, clients[i], disc)
			raw, src := readReplyOrTimeout(t, clients[i], 700*time.Millisecond)
			if raw == nil {
				silenced[i] = true
				return
			}
			p, err := dhcppacket.Decode(raw)
			if err != nil {
				t.Errorf("offer decode: %v from %v", err, src)
				return
			}
			if mt, _ := p.MessageType(); mt != dhcppacket.MsgOffer {
				t.Errorf("client %d expected OFFER got %d", id, mt)
				return
			}
			offers[i] = p.YIAddr.String()
		}(i)
	}
	close(start)
	wg.Wait()

	winners := 0
	for _, ip := range offers {
		if ip != "" {
			winners++
		}
	}
	silentN := 0
	for _, s := range silenced {
		if s {
			silentN++
		}
	}
	if winners != 2 || silentN != 1 {
		t.Fatalf("winners=%d silent=%d (want 2 and 1)", winners, silentN)
	}

	// Both winners commit.
	var ownerIdx int
	for i := range offers {
		if offers[i] == "" {
			continue
		}
		id := byte(0x30 + i)
		req := dhcppacket.NewRequest(uint32(3100+i), macAt(id)).
			Type(dhcppacket.MsgRequest).ClientID(cidAt(id)).
			RequestedIP(mustParse(offers[i])).ServerID(mustParse("127.0.0.1")).Bytes()
		env.send(t, clients[i], req)
		ackRaw, _ := readReply(t, clients[i])
		ack, _ := dhcppacket.Decode(ackRaw)
		if mt, _ := ack.MessageType(); mt != dhcppacket.MsgAck {
			t.Fatalf("winner %d commit failed type=%d", i, mt)
		}
		ownerIdx = i
	}

	// Owner releases its address; server stays silent.
	ownerIP := offers[ownerIdx]
	rel := dhcppacket.NewRequest(uint32(3200)+uint32(ownerIdx), macAt(byte(0x30+ownerIdx))).
		Type(dhcppacket.MsgRelease).ClientID(cidAt(byte(0x30 + ownerIdx))).
		CIAddr(mustParse(ownerIP)).Bytes()
	env.send(t, clients[ownerIdx], rel)
	expectNoReply(t, clients[ownerIdx])

	// The previously silenced client retries DISCOVER and must now win the
	// freed address.
	var loserIdx int
	for i, s := range silenced {
		if s {
			loserIdx = i
		}
	}
	id := byte(0x30 + loserIdx)
	retry := dhcppacket.NewRequest(uint32(3300), macAt(id)).
		Type(dhcppacket.MsgDiscover).ClientID(cidAt(id)).Bytes()
	env.send(t, clients[loserIdx], retry)
	raw, _ := readReply(t, clients[loserIdx])
	p, _ := dhcppacket.Decode(raw)
	if mt, _ := p.MessageType(); mt != dhcppacket.MsgOffer || p.YIAddr.String() != ownerIP {
		t.Fatalf("reallocation offer type=%d ip=%s want %s", mt, p.YIAddr, ownerIP)
	}
}

func readReplyOrTimeout(t *testing.T, c *net.UDPConn, d time.Duration) ([]byte, *net.UDPAddr) {
	t.Helper()
	if err := c.SetReadDeadline(time.Now().Add(d)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 1500)
	n, src, err := c.ReadFromUDP(buf)
	if err != nil {
		return nil, nil
	}
	return append([]byte(nil), buf[:n]...), src
}

func mustParse(s string) netip.Addr {
	a, err := netip.ParseAddr(s)
	if err != nil {
		panic("bad ip " + s)
	}
	return a.Unmap()
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
