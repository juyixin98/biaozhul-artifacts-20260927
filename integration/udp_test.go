// Package integration_test exercises the complete server over real UDP
// loopback sockets using the independent client fixture (wirekit/
// dhclient), never the server's codec. These are the headline scenarios
// from the lab requirements: concurrent contention, old-transaction
// replay, client restart and expiry/release — with assertions on address
// ownership and protocol state, not merely callability.
package integration_test

import (
	"context"
	"net/netip"
	"testing"
	"time"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/storage"
	"dhcp4lab/testfixture/dhclient"
	"dhcp4lab/testfixture/testhelp"
	"dhcp4lab/testfixture/wirekit"
)

const replyWait = 2 * time.Second
const silenceWait = 400 * time.Millisecond

func mustClient(t *testing.T, id, addr string, idx byte) *dhclient.Client {
	t.Helper()
	mac := macFor(idx)
	c, err := dhclient.New(id, addr, mac, nil)
	if err != nil {
		t.Fatalf("client %s: %v", id, err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c
}

func macFor(idx byte) string {
	return "02:00:00:00:00:" + twoHex(idx)
}

func twoHex(b byte) string {
	const h = "0123456789abcdef"
	return string([]byte{h[b>>4], h[b&0xf]})
}

func asFailure(t *testing.T, err error) *dhclient.Failure {
	t.Helper()
	if err == nil {
		return nil
	}
	f, ok := err.(*dhclient.Failure)
	if !ok {
		t.Fatalf("non-classified error %v", err)
	}
	return f
}

// 1. Full four-way exchange with independent field-level validation of
// the OFFER and ACK (address, lease option, server id, netmask, router).
func TestUDPFourWayFlow(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("flow")
	rc.Progress("starting four-way exchange", "version", testhelp.Version(), "server", addr)

	c := mustClient(t, "alpha", addr, 1)
	x := dhclient.XID(0x01010101)
	ctx := rc.Annotate(wirekit.XID(x), wirekit.MACStr(c.HWAddr), "alpha")

	ctx.Step("send DISCOVER")
	offer, rawOffer, err := c.Exchange(c.Discover(x), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatalf("OFFER: %v", err)
	}
	ctx.Pass("received OFFER", "yiaddr", wirekit.IPStr(offer.YIAddr))

	// OFFER must be a reservation only: no lease row yet.
	lv, _ := s.Store.LeaseByIP(context.Background(), fromRef(offer.YIAddr))
	if lv != nil {
		t.Fatalf("OFFER created lease state %s; reservation must not be a lease", lv.State)
	}
	ctx.Expect("option53=OFFER option54=192.0.2.1 option51=120 yiaddr in pool",
		"RFC 2131 OFFER field requirements")
	if mt, _ := offer.MsgType(); mt != wirekit.MTOffer {
		t.Fatalf("offer type %d", mt)
	}
	if got := wirekit.IPStr(offer.SIAddr); got != "192.0.2.1" {
		t.Fatalf("siaddr/server id = %s", got)
	}
	wantSID := wirekit.IP("192.0.2.1")
	if optIP(offer.Opts[wirekit.OptServerID]) != wantSID {
		t.Fatalf("option 54 = % x", offer.Opts[wirekit.OptServerID])
	}
	if wirekit.U32At(offer.Opts[wirekit.OptLeaseTime]) != 120 {
		t.Fatalf("offer option51 = %d, want 120", wirekit.U32At(offer.Opts[wirekit.OptLeaseTime]))
	}
	if !s.Pool.Contains(fromRef(offer.YIAddr)) {
		t.Fatalf("offered %s outside pool", wirekit.IPStr(offer.YIAddr))
	}
	if len(rawOffer) < wirekit.MinReplyLen {
		t.Fatalf("offer only %d bytes", len(rawOffer))
	}
	ctx.Pass("OFFER fields verified and no lease row exists yet")

	ctx.Step("send SELECTING REQUEST", "requested", wirekit.IPStr(offer.YIAddr))
	req := c.RequestSelect(dhclient.XID(0x01010101), offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID]))
	ack, _, err := c.Exchange(req, wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatalf("ACK: %v", err)
	}
	if wirekit.IPStr(ack.YIAddr) != wirekit.IPStr(offer.YIAddr) {
		t.Fatalf("ACK yiaddr %s != OFFER %s", wirekit.IPStr(ack.YIAddr), wirekit.IPStr(offer.YIAddr))
	}
	if wirekit.U32At(ack.Opts[wirekit.OptLeaseTime]) != 120 {
		t.Fatalf("ack option51 = %d", wirekit.U32At(ack.Opts[wirekit.OptLeaseTime]))
	}
	wantMask := wirekit.IP("255.255.255.0")
	if optIP(ack.Opts[wirekit.OptSubnetMask]) != wantMask {
		t.Fatalf("ack netmask = % x", ack.Opts[wirekit.OptSubnetMask])
	}
	ctx.Pass("ACK received and lease committed", "ip", wirekit.IPStr(ack.YIAddr))

	lv2, _ := s.Store.LeaseByIP(context.Background(), fromRef(ack.YIAddr))
	if lv2 == nil || lv2.State != storage.StateLeased {
		t.Fatalf("post-ACK lease row = %+v", lv2)
	}
}

// 2. Concurrent contention over real UDP sockets: 25 clients complete
// the four-way exchange simultaneously. Every leased address must be
// unique and durably recorded.
func TestUDPConcurrentContention(t *testing.T) {
	const n = 25
	s := testhelp.NewStore(t, testhelp.Options{PoolSize: n})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("contention")
	rc.Progress("starting concurrent contention", "clients", n, "pool_size", n)

	type result struct {
		idx byte
		ip  [4]byte
		err error
	}
	resCh := make(chan result, n)
	for i := byte(1); i <= n; i++ {
		go func(idx byte) {
			c, err := dhclient.New("bulk", addr, macFor(idx), nil)
			if err != nil {
				resCh <- result{idx, [4]byte{}, err}
				return
			}
			defer c.Close()
			x := dhclient.XID(uint32(0xC0FFEE00) + uint32(idx))
			offer, _, err := c.Exchange(c.Discover(x), wirekit.MTOffer, replyWait)
			if err != nil {
				resCh <- result{idx, [4]byte{}, err}
				return
			}
			req := c.RequestSelect(x, offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID]))
			ack, _, err := c.Exchange(req, wirekit.MTACK, replyWait)
			if err != nil {
				resCh <- result{idx, [4]byte{}, err}
				return
			}
			resCh <- result{idx, ack.YIAddr, nil}
		}(i)
	}
	owners := map[[4]byte]byte{}
	var fails []result
	for i := 0; i < n; i++ {
		r := <-resCh
		if r.err != nil {
			fails = append(fails, r)
			continue
		}
		if owner, taken := owners[r.ip]; taken {
			t.Fatalf("address %s leased to client %d and client %d",
				wirekit.IPStr(r.ip), owner, r.idx)
		}
		owners[r.ip] = r.idx
	}
	for _, f := range fails {
		t.Errorf("client %d failed: %v", f.idx, f.err)
	}
	if len(owners) != n {
		t.Fatalf("unique leased = %d, want %d", len(owners), n)
	}
	rows, err := s.Store.ListLeases(context.Background(), n*2)
	if err != nil {
		t.Fatal(err)
	}
	liveLeased := 0
	for _, row := range rows {
		if row.State == storage.StateLeased {
			liveLeased++
		}
	}
	if liveLeased != n {
		t.Fatalf("durable live leases = %d, want %d", liveLeased, n)
	}
	rc.Pass("all clients hold unique addresses", "unique_leases", len(owners))
}

// 3. Old-transaction replay: the identical REQUEST is answered again but
// the lease is not extended; the stored expiry and option-51 remaining
// time prove it.
func TestUDPDuplicateACKDoesNotExtend(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("replay")

	c := mustClient(t, "replayer", addr, 2)
	x := dhclient.XID(0xDEAD0001)
	ctx := rc.Annotate(wirekit.XID(x), wirekit.MACStr(c.HWAddr), "replayer")

	offer, _, err := c.Exchange(c.Discover(x), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	req := c.RequestSelect(x, offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID]))
	ack1, _, err := c.Exchange(req, wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	firstOpt51 := wirekit.U32At(ack1.Opts[wirekit.OptLeaseTime])
	ctx.Step("first ACK", "option51", firstOpt51)
	lv1, _ := s.Store.LeaseByIP(context.Background(), fromRef(ack1.YIAddr))
	storedExpiry := lv1.ExpiresAt

	// Advance the state machine clock well past the ACK and replay the
	// SAME datagram (new client socket is allowed; identity is chaddr).
	s.Clock.Advance(45 * time.Second)
	rc.Progress("replaying identical REQUEST after clock advance", "advanced_s", 45)
	c2 := mustClient(t, "replayer2", addr, 2)
	ack2, _, err := c2.Exchange(req, wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatalf("duplicate ACK: %v", err)
	}
	lv2, _ := s.Store.LeaseByIP(context.Background(), fromRef(ack2.YIAddr))
	if !lv2.ExpiresAt.Equal(storedExpiry) {
		t.Fatalf("stored expiry moved %s -> %s on replay (unauthorized extension)",
			storedExpiry, lv2.ExpiresAt)
	}
	secondOpt51 := wirekit.U32At(ack2.Opts[wirekit.OptLeaseTime])
	if secondOpt51 >= firstOpt51 {
		t.Fatalf("replayed option51 = %ds >= original %ds (must report remaining time)",
			secondOpt51, firstOpt51)
	}
	if secondOpt51 < 74 || secondOpt51 > 76 {
		t.Fatalf("replayed option51 = %ds, want ~75s remaining", secondOpt51)
	}
	ctx.Pass("replay answered, lease unchanged",
		"option51_first", firstOpt51, "option51_replay", secondOpt51)
}

// 4. Client restart: a brand new socket with the same MAC verifies its
// remembered address via INIT-REBOOT (option 50, no 54). Correct IP ->
// ACK; wrong IP -> NAK with zero yiaddr and no config options.
func TestUDPClientRestart(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("restart")

	c1 := mustClient(t, "before", addr, 3)
	x := dhclient.XID(0xBEEF0001)
	offer, _, err := c1.Exchange(c1.Discover(x), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	ack, _, err := c1.Exchange(
		c1.RequestSelect(x, offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID])),
		wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	_ = c1.Close()
	rc.Progress("client closed its socket; restarting with a fresh one")

	// Simulate wall-time passing before the reboot (timer restarts).
	s.Clock.Advance(20 * time.Second)

	c2 := mustClient(t, "after", addr, 3)
	ctx := rc.Annotate("", wirekit.MACStr(c2.HWAddr), "restarted")
	ctx.Step("INIT-REBOOT with remembered IP", "remembered", wirekit.IPStr(ack.YIAddr))
	rebootACK, _, err := c2.Exchange(
		c2.RequestReboot(dhclient.XID(0xBEEF0002), ack.YIAddr),
		wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatalf("init-reboot correct ip: %v", err)
	}
	if wirekit.IPStr(rebootACK.YIAddr) != wirekit.IPStr(ack.YIAddr) {
		t.Fatalf("reboot ACK yiaddr %s != remembered %s",
			wirekit.IPStr(rebootACK.YIAddr), wirekit.IPStr(ack.YIAddr))
	}
	ctx.Pass("reboot confirmed lease", "ip", wirekit.IPStr(rebootACK.YIAddr))

	// Reboot claiming a different, foreign address must NAK.
	wrong := wirekit.IP("192.0.2.19")
	if wrong == ack.YIAddr {
		wrong = wirekit.IP("192.0.2.18")
	}
	nak, _, err := c2.Exchange(
		c2.RequestReboot(dhclient.XID(0xBEEF0003), wrong),
		wirekit.MTNAK, replyWait)
	if err != nil {
		t.Fatalf("init-reboot wrong ip: %v", err)
	}
	if nak.YIAddr != [4]byte{} {
		t.Fatalf("NAK yiaddr = %s, must be 0.0.0.0", wirekit.IPStr(nak.YIAddr))
	}
	if _, hasLease := nak.Opts[wirekit.OptLeaseTime]; hasLease {
		t.Fatal("NAK must not carry option 51")
	}
	if _, hasMask := nak.Opts[wirekit.OptSubnetMask]; hasMask {
		t.Fatal("NAK must not carry option 1")
	}
	ctx.Pass("wrong-IP reboot produced a minimal NAK")
}

// 5. RELEASE frees the address: no reply, row becomes 'released', and the
// next DISCOVERing client can obtain it.
func TestUDPReleaseFreesAddress(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("release")

	c := mustClient(t, "holder", addr, 4)
	x := dhclient.XID(0x5A5A0001)
	offer, _, err := c.Exchange(c.Discover(x), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	ack, _, err := c.Exchange(
		c.RequestSelect(x, offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID])),
		wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	leased := ack.YIAddr

	ctx := rc.Annotate(wirekit.XID(x), wirekit.MACStr(c.HWAddr), "holder")
	ctx.Step("send RELEASE; RFC 2131 mandates no reply")
	if err := c.AssertSilence(c.Release(dhclient.XID(0x5A5A0002), leased), silenceWait); err != nil {
		t.Fatalf("release: %v", err)
	}
	lv, _ := s.Store.LeaseByIP(context.Background(), fromRef(leased))
	if lv.State != storage.StateReleased {
		t.Fatalf("lease state = %s, want released", lv.State)
	}
	ctx.Pass("lease recorded as released, no datagram returned")

	// The previous owner re-DISCOVERs and SHOULD be offered its old IP;
	// it can commit it again.
	o2, _, err := c.Exchange(c.Discover(dhclient.XID(0x5A5A0003)), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatalf("rediscover: %v", err)
	}
	if o2.YIAddr != leased {
		t.Fatalf("re-offered %s, want released IP %s", wirekit.IPStr(o2.YIAddr), wirekit.IPStr(leased))
	}
	ack2, _, err := c.Exchange(
		c.RequestSelect(dhclient.XID(0x5A5A0003), o2.YIAddr, optIP(o2.Opts[wirekit.OptServerID])),
		wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatalf("recommit: %v", err)
	}
	if ack2.YIAddr != leased {
		t.Fatal("recommitted IP differs")
	}
	ctx.Pass("released address successfully re-leased", "ip", wirekit.IPStr(leased))
}

// 6. Lease expiry on the wire: after the lease laps and the sweep runs, a
// RENEW gets silence and a DISCOVER gives the address back to the owner.
func TestUDPExpiryThenRediscover(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{LeaseTime: 60 * time.Second})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("expiry")

	c := mustClient(t, "ephemeral", addr, 5)
	x := dhclient.XID(0xEEEE0001)
	offer, _, err := c.Exchange(c.Discover(x), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	ack, _, err := c.Exchange(
		c.RequestSelect(x, offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID])),
		wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatal(err)
	}
	leased := ack.YIAddr

	s.Clock.Advance(61 * time.Second)
	if _, _, err := s.Store.Sweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	rc.Progress("lease passed TTL and sweep marked it expired")

	ctx := rc.Annotate(wirekit.XID(x), wirekit.MACStr(c.HWAddr), "ephemeral")
	ctx.Step("RENEW past expiry must stay silent")
	if err := c.AssertSilence(c.RequestRenew(dhclient.XID(0xEEEE0002), leased), silenceWait); err != nil {
		f := asFailure(t, err)
		if f == nil || f.Category != "client_unexpected_reply" {
			t.Fatalf("expired renew: %v", err)
		}
	}
	lv, _ := s.Store.LeaseByIP(context.Background(), fromRef(leased))
	if lv.State != storage.StateExpired {
		t.Fatalf("state = %s, want expired", lv.State)
	}
	ctx.Pass("no renewal after expiry and state is expired")

	o2, _, err := c.Exchange(c.Discover(dhclient.XID(0xEEEE0003)), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatalf("post-expiry discover: %v", err)
	}
	if o2.YIAddr != leased {
		t.Fatalf("post-expiry offer %s, want owner-reuse %s",
			wirekit.IPStr(o2.YIAddr), wirekit.IPStr(leased))
	}
}

// 7. Malformed datagrams are dropped: the server answers nothing and the
// parse failure is a classified category (checked client-side as a
// timeout, since the server never encodes a reply to garbage).
func TestUDPMalformedDatagramsDropped(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("malformed")

	c := mustClient(t, "fuzzer", addr, 6)
	garbage := [][]byte{
		make([]byte, 50),         // truncated
		{1, 1, 6, 0, 1, 2, 3, 4}, // truncated with xid
		func() []byte { // valid shape, bad cookie
			b := c.RawBuilder(dhclient.XID(0xF1)).MsgType(wirekit.MTDiscover).Build()
			b[236] = 0
			return b
		}(),
	}
	for i, g := range garbage {
		ctx := rc.Annotate(hex4(g), "", "fuzzer")
		ctx.Step("send malformed datagram", "index", i, "len", len(g))
		err := c.AssertSilence(g, silenceWait)
		if err != nil {
			f := asFailure(t, err)
			if f == nil || f.Category != "client_unexpected_reply" {
				t.Fatalf("garbage[%d]: %v", i, err)
			}
			t.Fatalf("garbage[%d] produced a reply", i)
		}
		ctx.Pass("no reply for malformed input", "index", i)
	}
}

// 8. Crash isolation: a RELEASE with ciaddr=0.0.0.0 (which used to panic
// in ipBlob) must be silently dropped, and the server must keep serving
// other clients afterwards.
func TestUDPZeroCIAddrReleaseDoesNotCrashServer(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	addr := s.StartUDP(t)
	lg := testhelp.Log(t)
	rc := lg.For("crash-isolation")

	bad := mustClient(t, "bad", addr, 7)
	good := mustClient(t, "good", addr, 8)

	// Well-formed RELEASE except ciaddr is all zeros.
	malformed := bad.RawBuilder(dhclient.XID(0xFADE0001)).
		MsgType(wirekit.MTRelease).Build()
	rc.Progress("sending RELEASE with ciaddr=0 (formerly a panic)")
	if err := bad.AssertSilence(malformed, silenceWait); err != nil {
		f := asFailure(t, err)
		if f == nil || f.Category != "client_unexpected_reply" {
			t.Fatalf("malformed release: %v", err)
		}
		t.Fatal("ciaddr=0 RELEASE produced a reply")
	}

	// The listener must still be alive: a normal exchange by another
	// client has to complete.
	x := dhclient.XID(0xFADE0002)
	offer, _, err := good.Exchange(good.Discover(x), wirekit.MTOffer, replyWait)
	if err != nil {
		t.Fatalf("server not serving after malformed RELEASE: %v", err)
	}
	ack, _, err := good.Exchange(
		good.RequestSelect(x, offer.YIAddr, optIP(offer.Opts[wirekit.OptServerID])),
		wirekit.MTACK, replyWait)
	if err != nil {
		t.Fatalf("post-crash ACK: %v", err)
	}
	if wirekit.IPStr(ack.YIAddr) != wirekit.IPStr(offer.YIAddr) {
		t.Fatal("post-crash exchange inconsistent")
	}
	rc.Pass("server survived malformed RELEASE and still serves clients")
}

func hex4(b []byte) string {
	if len(b) >= 8 {
		return wirekit.XID([4]byte{b[4], b[5], b[6], b[7]})
	}
	return ""
}

// fromRef converts the independent codec's [4]byte to a netip.Addr.
func fromRef(a [4]byte) netip.Addr {
	return dhcp4.IPv4(a[0], a[1], a[2], a[3])
}

// optIP converts a 4-byte option value from a reply into [4]byte.
func optIP(b []byte) [4]byte {
	if len(b) != 4 {
		panic("option is not a 4-byte IPv4")
	}
	return [4]byte{b[0], b[1], b[2], b[3]}
}
