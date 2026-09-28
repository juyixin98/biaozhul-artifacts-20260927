package storage_test

import (
	"context"
	"fmt"
	"net/netip"
	"sync"
	"testing"
	"time"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/storage"
	"dhcp4lab/testfixture/testhelp"
)

func ident(idx byte) dhcp4.ClientIdentity {
	return dhcp4.ClientIdentity{HType: 1, CHAddr: [6]byte{2, 0, 0, 0, 0, idx}}
}

func xid(v uint32) [4]byte {
	return [4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)}
}

func ipStr(a netip.Addr) string {
	if !a.IsValid() {
		return "<none>"
	}
	return a.String()
}

// discover is a shorthand that fails the test on transport errors.
func discover(t *testing.T, s *testhelp.Setup, id dhcp4.ClientIdentity, x [4]byte) *storage.Outcome {
	t.Helper()
	out, err := s.Store.Discover(context.Background(), storage.DiscoverInput{
		XID: x, Identity: id, CHAddr: id.CHAddr, ClientID: id.OptionID,
	})
	if err != nil {
		t.Fatalf("Discover xid=%x: %v", x, err)
	}
	return out
}

func requestSelect(t *testing.T, s *testhelp.Setup, id dhcp4.ClientIdentity, x [4]byte, requested netip.Addr) *storage.Outcome {
	t.Helper()
	out, err := s.Store.Request(context.Background(), storage.RequestInput{
		XID: x, Kind: storage.ReqSelecting, Identity: id, CHAddr: id.CHAddr,
		ServerID: netip.MustParseAddr("192.0.2.1"), RequestedIP: requested,
	})
	if err != nil {
		t.Fatalf("Request(select) xid=%x: %v", x, err)
	}
	return out
}

// 1. OFFER is a reservation only: no leased row exists until REQUEST.
func TestOfferIsReservationNotLease(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(1)
	off := discover(t, s, id, xid(100))
	if off.Action != storage.ActOffer {
		t.Fatalf("action = %s, want offer", off.Action)
	}
	if off.LeaseState != storage.StateOffered {
		t.Fatalf("state = %s, want offered", off.LeaseState)
	}
	// No lease row may exist for the offered IP.
	lv, err := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if err != nil {
		t.Fatal(err)
	}
	if lv != nil {
		t.Fatalf("OFFER created a lease row (%s); reservation must not equal a lease", lv.State)
	}
	offer, err := s.Store.ActiveOffer(context.Background(), id)
	if err != nil || offer == nil {
		t.Fatalf("live offer missing: %v", err)
	}
	if offer.IP != off.LeaseIP {
		t.Fatalf("offer ip = %s, want %s", offer.IP, off.LeaseIP)
	}
}

// 2. Full SELECTING flow: DISCOVER -> OFFER -> REQUEST(50/54) -> ACK and a
// durable leased row.
func TestSelectingFlowCommitsLease(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(2)
	x1 := xid(200)
	off := discover(t, s, id, x1)
	if off.Action != storage.ActOffer {
		t.Fatalf("want offer, got %s (%s)", off.Action, off.Reason)
	}
	ack := requestSelect(t, s, id, x1, off.LeaseIP)
	if ack.Action != storage.ActACK {
		t.Fatalf("want ack, got %s reason=%s", ack.Action, ack.Reason)
	}
	if ack.LeaseState != storage.StateLeased {
		t.Fatalf("state = %s, want leased", ack.LeaseState)
	}
	if !ack.LeaseExpires.Equal(s.Clock.Now().Add(120 * time.Second)) {
		t.Fatalf("expiry = %s, want now+120s (%s)", ack.LeaseExpires, s.Clock.Now().Add(120*time.Second))
	}
	lv, err := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if err != nil {
		t.Fatal(err)
	}
	if lv == nil || lv.State != storage.StateLeased {
		t.Fatalf("persisted lease = %+v, want leased", lv)
	}
	// The offer must be consumed (no live reservation remains).
	o2, _ := s.Store.ActiveOffer(context.Background(), id)
	if o2 != nil {
		t.Fatalf("offer still live after commit: %s", o2.IP)
	}
}

// 3. SELECTING REQUEST without a matching live OFFER -> NAK with the exact
// category; a request for an IP different from the offer -> NAK too.
func TestSelectingNAKCategories(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(3)

	// 3a. No offer at all.
	ack := requestSelect(t, s, id, xid(300), netip.MustParseAddr("192.0.2.11"))
	if ack.Action != storage.ActNAK || ack.Reason != storage.ReasonNoActiveOffer {
		t.Fatalf("got %s/%s, want NAK/%s", ack.Action, ack.Reason, storage.ReasonNoActiveOffer)
	}

	// 3b. Offer exists but the REQUEST names a different IP.
	off := discover(t, s, id, xid(301))
	other := netip.MustParseAddr("192.0.2.19")
	if other == off.LeaseIP {
		other = netip.MustParseAddr("192.0.2.18")
	}
	ack2 := requestSelect(t, s, id, xid(301), other)
	if ack2.Action != storage.ActNAK || ack2.Reason != storage.ReasonOfferForOtherIP {
		t.Fatalf("got %s/%s, want NAK/%s", ack2.Action, ack2.Reason, storage.ReasonOfferForOtherIP)
	}

	// 3c. Foreign server identifier -> protocol silence, not a NAK.
	off2 := discover(t, s, id, xid(302))
	out, err := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(302), Kind: storage.ReqSelecting, Identity: id, CHAddr: id.CHAddr,
		ServerID: netip.MustParseAddr("198.51.100.9"), RequestedIP: off2.LeaseIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Action != storage.ActDrop || out.Reason != storage.ReasonForeignServerID {
		t.Fatalf("got %s/%s, want drop/%s", out.Action, out.Reason, storage.ReasonForeignServerID)
	}
	if out.Reply != nil {
		t.Fatal("foreign-server REQUEST must not produce a reply")
	}
}

// 4. INIT-REBOOT after client restart: correct remembered IP -> ACK and
// lease timer restarts; wrong IP -> NAK; unknown client -> silence.
func TestInitReboot(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})

	// Seed a committed lease for id=4.
	id := ident(4)
	off := discover(t, s, id, xid(400))
	ack := requestSelect(t, s, id, xid(400), off.LeaseIP)
	if ack.Action != storage.ActACK {
		t.Fatalf("seed ACK failed: %s", ack.Reason)
	}

	// Client "reboots" later, remembers its IP: option 50, no 54.
	s.Clock.Advance(50 * time.Second)
	out, err := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(401), Kind: storage.ReqInitReboot, Identity: id, CHAddr: id.CHAddr,
		RequestedIP: off.LeaseIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Action != storage.ActACK {
		t.Fatalf("reboot correct ip: got %s/%s, want ACK", out.Action, out.Reason)
	}
	// Timer restarts from the reboot instant.
	if !out.LeaseExpires.Equal(s.Clock.Now().Add(120 * time.Second)) {
		t.Fatalf("reboot expiry = %s, want %s", out.LeaseExpires, s.Clock.Now().Add(120*time.Second))
	}

	// Wrong remembered IP -> NAK with the reboot-specific category.
	wrong := netip.MustParseAddr("192.0.2.19")
	if wrong == off.LeaseIP {
		wrong = netip.MustParseAddr("192.0.2.18")
	}
	out2, _ := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(402), Kind: storage.ReqInitReboot, Identity: id, CHAddr: id.CHAddr,
		RequestedIP: wrong,
	})
	if out2.Action != storage.ActNAK || out2.Reason != storage.ReasonWrongIPInitReboot {
		t.Fatalf("reboot wrong ip: got %s/%s", out2.Action, out2.Reason)
	}

	// Unknown client rebooting -> silent per RFC, never a success.
	idUnknown := ident(5)
	out3, _ := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(403), Kind: storage.ReqInitReboot, Identity: idUnknown, CHAddr: idUnknown.CHAddr,
		RequestedIP: wrong,
	})
	if out3.Action != storage.ActDrop || out3.Reason != storage.ReasonUnknownClientReboot {
		t.Fatalf("unknown reboot: got %s/%s, want drop/%s",
			out3.Action, out3.Reason, storage.ReasonUnknownClientReboot)
	}
}

// 5. RENEWING: matching ciaddr extends the lease; no lease -> silence;
// ciaddr disagreeing with the lease -> NAK.
func TestRenewSemantics(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(6)
	off := discover(t, s, id, xid(500))
	ack := requestSelect(t, s, id, xid(500), off.LeaseIP)
	firstExpiry := ack.LeaseExpires

	s.Clock.Advance(60 * time.Second)
	out, err := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(501), Kind: storage.ReqRenew, Identity: id, CHAddr: id.CHAddr,
		CIAddr: off.LeaseIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Action != storage.ActACK {
		t.Fatalf("renew: got %s/%s, want ACK", out.Action, out.Reason)
	}
	if !out.LeaseExpires.After(firstExpiry) {
		t.Fatalf("renew did not extend expiry: %s <= %s", out.LeaseExpires, firstExpiry)
	}

	// Renew for an address the client does not hold -> NAK.
	out2, _ := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(502), Kind: storage.ReqRenew, Identity: id, CHAddr: id.CHAddr,
		CIAddr: netip.MustParseAddr("192.0.2.19"),
	})
	if out2.Action != storage.ActNAK || out2.Reason != storage.ReasonRenewAddrMismatch {
		t.Fatalf("renew mismatch: got %s/%s", out2.Action, out2.Reason)
	}

	// Unknown client renewal -> silence with the exact category.
	idU := ident(7)
	out3, _ := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(503), Kind: storage.ReqRenew, Identity: idU, CHAddr: idU.CHAddr,
		CIAddr: netip.MustParseAddr("192.0.2.10"),
	})
	if out3.Action != storage.ActDrop || out3.Reason != storage.ReasonRenewNoLease {
		t.Fatalf("renew no lease: got %s/%s, want drop/%s", out3.Action, out3.Reason, storage.ReasonRenewNoLease)
	}
}

// 6. Duplicate DISCOVER replay returns the same OFFER and must NOT move
// the reservation's expiry.
func TestDuplicateDiscoverDoesNotExtend(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(8)
	x := xid(600)
	first := discover(t, s, id, x)
	firstExpiry := first.LeaseExpires

	s.Clock.Advance(10 * time.Second)
	second := discover(t, s, id, x)
	if !second.Duplicate {
		t.Fatal("repeated DISCOVER not flagged duplicate")
	}
	if second.LeaseIP != first.LeaseIP {
		t.Fatalf("duplicate offered %s, want %s", second.LeaseIP, first.LeaseIP)
	}
	if !second.LeaseExpires.Equal(firstExpiry) {
		t.Fatalf("duplicate moved expiry: %s -> %s (reservation extended!)",
			firstExpiry, second.LeaseExpires)
	}
}

// 7. A NEW DISCOVER transaction supersedes the previous reservation but
// re-serves the same address while it is still free.
func TestNewDiscoverSupersedesOldOffer(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(9)
	o1 := discover(t, s, id, xid(700))
	o2 := discover(t, s, id, xid(701))
	if o2.LeaseIP != o1.LeaseIP {
		t.Fatalf("new DISCOVER moved address %s -> %s (SHOULD stay consistent)",
			o1.LeaseIP, o2.LeaseIP)
	}
	offers, err := s.Store.ActiveOffer(context.Background(), id)
	if err != nil || offers == nil {
		t.Fatalf("live offer: %v", err)
	}
	// Only one live offer row; the first must be superseded.
	if offers.IP != o2.LeaseIP {
		t.Fatalf("live offer %s, want newest %s", offers.IP, o2.LeaseIP)
	}
}

// 8. Duplicate ACK replay answers again but cannot extend the lease.
func TestDuplicateACKDoesNotExtendLease(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(10)
	x := xid(800)
	off := discover(t, s, id, x)
	ack := requestSelect(t, s, id, x, off.LeaseIP)
	grantedExpiry := ack.LeaseExpires

	s.Clock.Advance(45 * time.Second)
	replay := requestSelect(t, s, id, x, off.LeaseIP)
	if !replay.Duplicate {
		t.Fatal("repeated REQUEST not flagged duplicate")
	}
	if replay.Action != storage.ActACK {
		t.Fatalf("duplicate action = %s, want ACK", replay.Action)
	}
	if !replay.LeaseExpires.Equal(grantedExpiry) {
		t.Fatalf("duplicate ACK changed expiry %s -> %s (unauthorized extension!)",
			grantedExpiry, replay.LeaseExpires)
	}
	// The carried option-51 must report the REMAINING time (~75s), not a
	// fresh 120s lease.
	if got := replay.Reply.LeaseSeconds; got < 74 || got > 76 {
		t.Fatalf("duplicate ACK lease option = %ds, want ~75s remaining", got)
	}
	// The durable row's expiry is unchanged as well.
	lv, _ := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if !lv.ExpiresAt.Equal(grantedExpiry) {
		t.Fatalf("stored expiry moved: %s -> %s", grantedExpiry, lv.ExpiresAt)
	}
}

// 9. Duplicate NAK is repeated (a client that retries the same bad
// selecting request must keep being told to re-DISCOVER).
func TestDuplicateNAKRepeated(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(11)
	x := xid(900)
	first := requestSelect(t, s, id, x, netip.MustParseAddr("192.0.2.12"))
	if first.Action != storage.ActNAK {
		t.Fatalf("first = %s, want NAK", first.Action)
	}
	second := requestSelect(t, s, id, x, netip.MustParseAddr("192.0.2.12"))
	if !second.Duplicate || second.Action != storage.ActNAK {
		t.Fatalf("replay = %s duplicate=%v, want duplicate NAK", second.Action, second.Duplicate)
	}
}

// 10. OFFER expiry: after offer_ttl the reservation is gone and the
// address can be reserved for another client.
func TestOfferExpiryReleasesAddress(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{OfferTTL: 30 * time.Second})
	a := ident(12)
	o1 := discover(t, s, a, xid(1000))

	b := ident(13)
	// Before expiry: B must not get A's reserved address.
	o2 := discover(t, s, b, xid(1001))
	if o2.LeaseIP == o1.LeaseIP {
		t.Fatal("address handed out while first OFFER still live")
	}

	// Expire offers (touch path: a new DISCOVER sweeps first).
	s.Clock.Advance(31 * time.Second)
	o3 := discover(t, s, b, xid(1002))
	// B's reservation was superseded by its own new DISCOVER; either way
	// the previously reserved address must now be assignable to someone.
	o4 := discover(t, s, a, xid(1003))
	_ = o3
	if o4.Action != storage.ActOffer {
		t.Fatalf("post-expiry discover for A: %s", o4.Action)
	}
	// A can commit its original address again now that the old offer lapsed.
	ack := requestSelect(t, s, a, xid(1003), o4.LeaseIP)
	if ack.Action != storage.ActACK {
		t.Fatalf("commit after offer expiry: %s/%s", ack.Action, ack.Reason)
	}
}

// 11. Lease expiry: after lease_time the row becomes 'expired', renew/
// reboot are silent (client must re-DISCOVER), and the address is free.
func TestLeaseExpiry(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{LeaseTime: 120 * time.Second})
	id := ident(14)
	off := discover(t, s, id, xid(1100))
	ack := requestSelect(t, s, id, xid(1100), off.LeaseIP)
	if ack.Action != storage.ActACK {
		t.Fatal(ack.Reason)
	}
	s.Clock.Advance(121 * time.Second)

	// Sweep explicitly, exercising the reaper path used on idle systems.
	if _, _, err := s.Store.Sweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	lv, _ := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if lv == nil || lv.State != storage.StateExpired {
		t.Fatalf("state after ttl = %+v, want expired", lv)
	}
	// Renewing an expired lease is silent (not a phantom extension).
	out, _ := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(1101), Kind: storage.ReqRenew, Identity: id, CHAddr: id.CHAddr,
		CIAddr: off.LeaseIP,
	})
	if out.Action != storage.ActDrop || out.Reason != storage.ReasonRenewNoLease {
		t.Fatalf("expired renew: %s/%s", out.Action, out.Reason)
	}
	// INIT-REBOOT after expiry is likewise silent.
	out2, _ := s.Store.Request(context.Background(), storage.RequestInput{
		XID: xid(1102), Kind: storage.ReqInitReboot, Identity: id, CHAddr: id.CHAddr,
		RequestedIP: off.LeaseIP,
	})
	if out2.Action != storage.ActDrop {
		t.Fatalf("expired reboot: %s, want drop", out2.Action)
	}
	// The original client re-DISCOVERing after expiry SHOULD get its old
	// address back (it is the client's last leased address and now free).
	oSame := discover(t, s, id, xid(1103))
	if oSame.LeaseIP != off.LeaseIP {
		t.Fatalf("previous owner not re-offered its freed address: got %s, want %s",
			oSame.LeaseIP, off.LeaseIP)
	}
	// A different client gets a genuinely free address (not one with a
	// live lease); uniqueness is the guarantee, not cursor position.
	id2 := ident(15)
	oNew := discover(t, s, id2, xid(1104))
	if oNew.LeaseIP == oSame.LeaseIP {
		t.Fatalf("new client handed the same address %s that is now re-offered", oNew.LeaseIP)
	}
	if !s.Pool.Contains(oNew.LeaseIP) {
		t.Fatalf("offered %s outside pool", oNew.LeaseIP)
	}
}

// 12. RELEASE ends the lease and frees the address; a second RELEASE is a
// classified no-op, never reported as a successful release.
func TestReleaseSemantics(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(16)
	off := discover(t, s, id, xid(1200))
	if requestSelect(t, s, id, xid(1200), off.LeaseIP).Action != storage.ActACK {
		t.Fatal("commit failed")
	}
	rel, err := s.Store.Release(context.Background(), storage.ReleaseInput{
		XID: xid(1201), Identity: id, CHAddr: id.CHAddr, CIAddr: off.LeaseIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	if rel.Action != storage.ActReleased || rel.LeaseState != storage.StateReleased {
		t.Fatalf("release = %s/%s", rel.Action, rel.LeaseState)
	}
	if rel.Reply != nil {
		t.Fatal("RELEASE must not carry a reply")
	}
	lv, _ := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if lv.State != storage.StateReleased {
		t.Fatalf("stored state = %s, want released", lv.State)
	}

	// Repeating the RELEASE is a mismatch: distinct category, not success.
	rel2, _ := s.Store.Release(context.Background(), storage.ReleaseInput{
		XID: xid(1202), Identity: id, CHAddr: id.CHAddr, CIAddr: off.LeaseIP,
	})
	if rel2.Action != storage.ActDrop || rel2.Reason != storage.ReasonReleaseNoMatch {
		t.Fatalf("second release = %s/%s, want drop/%s",
			rel2.Action, rel2.Reason, storage.ReasonReleaseNoMatch)
	}

	// Another client RELEASEing an address it never held is also a mismatch.
	id3 := ident(17)
	rel3, _ := s.Store.Release(context.Background(), storage.ReleaseInput{
		XID: xid(1203), Identity: id3, CHAddr: id3.CHAddr,
		CIAddr: netip.MustParseAddr("192.0.2.10"),
	})
	if rel3.Action != storage.ActDrop || rel3.Reason != storage.ReasonReleaseNoMatch {
		t.Fatalf("foreign release = %s/%s", rel3.Action, rel3.Reason)
	}
}

// 13. Concurrent contention: many clients complete the four-way exchange
// simultaneously. Every ACK must name a unique address; no reservation or
// lease uniqueness guarantee may be violated.
func TestConcurrentContention(t *testing.T) {
	const n = 40
	s := testhelp.NewStore(t, testhelp.Options{PoolSize: n})

	var wg sync.WaitGroup
	errs := make(chan error, n)
	leased := make(chan netip.Addr, n)
	for i := 1; i <= n; i++ {
		wg.Add(1)
		go func(i byte) {
			defer wg.Done()
			id := ident(i)
			x := xid(2000 + uint32(i))
			off, err := s.Store.Discover(context.Background(), storage.DiscoverInput{
				XID: x, Identity: id, CHAddr: id.CHAddr,
			})
			if err != nil {
				errs <- fmt.Errorf("client %d discover: %w", i, err)
				return
			}
			if off.Action != storage.ActOffer {
				errs <- fmt.Errorf("client %d discover action %s", i, off.Action)
				return
			}
			ack, err := s.Store.Request(context.Background(), storage.RequestInput{
				XID: x, Kind: storage.ReqSelecting, Identity: id, CHAddr: id.CHAddr,
				ServerID: netip.MustParseAddr("192.0.2.1"), RequestedIP: off.LeaseIP,
			})
			if err != nil {
				errs <- fmt.Errorf("client %d request: %w", i, err)
				return
			}
			if ack.Action != storage.ActACK {
				errs <- fmt.Errorf("client %d got %s/%s", i, ack.Action, ack.Reason)
				return
			}
			leased <- ack.LeaseIP
		}(byte(i))
	}
	wg.Wait()
	close(errs)
	close(leased)
	for e := range errs {
		if e != nil {
			t.Error(e)
		}
	}
	seen := map[netip.Addr]int{}
	for a := range leased {
		seen[a]++
	}
	if len(seen) != n {
		t.Fatalf("unique leased addresses = %d, want %d (collision or loss)", len(seen), n)
	}
	for a, count := range seen {
		if count > 1 {
			t.Fatalf("address %s leased %d times", a, count)
		}
	}
}

// 14. Pool exhaustion reports a distinct action and never invents an
// address outside the pool.
func TestPoolExhaustion(t *testing.T) {
	const n = 3
	s := testhelp.NewStore(t, testhelp.Options{PoolSize: n})
	ips := map[netip.Addr]bool{}
	for i := byte(1); i <= n; i++ {
		id := ident(i)
		x := xid(3000 + uint32(i))
		off := discover(t, s, id, x)
		if off.Action != storage.ActOffer {
			t.Fatalf("client %d: %s", i, off.Action)
		}
		if requestSelect(t, s, id, x, off.LeaseIP).Action != storage.ActACK {
			t.Fatal("commit failed")
		}
		ips[off.LeaseIP] = true
	}
	if len(ips) != n {
		t.Fatalf("setup leases = %d", len(ips))
	}
	extra := discover(t, s, ident(99), xid(3999))
	if extra.Action != storage.ActPoolExhausted || extra.Reason != storage.ReasonPoolExhausted {
		t.Fatalf("exhaustion = %s/%s", extra.Action, extra.Reason)
	}
	if extra.Reply != nil || extra.LeaseIP.IsValid() {
		t.Fatal("exhaustion must not produce an address or reply")
	}
}

// 15. An old xid replayed AFTER its lease expired is treated as a fresh
// (failing) transaction, not a reason to resurrect the lease.
func TestOldTransactionAfterExpiry(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(18)
	x := xid(4000)
	off := discover(t, s, id, x)
	if requestSelect(t, s, id, x, off.LeaseIP).Action != storage.ActACK {
		t.Fatal("commit failed")
	}
	s.Clock.Advance(121 * time.Second)
	if _, _, err := s.Store.Sweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	out := requestSelect(t, s, id, x, off.LeaseIP)
	if out.Action != storage.ActNAK {
		t.Fatalf("stale xid replay = %s/%s, want fresh NAK (no live offer)",
			out.Action, out.Reason)
	}
	lv, _ := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if lv.State == storage.StateLeased {
		t.Fatal("stale replay resurrected an expired lease")
	}
}

// 16. A client that already holds a live lease and re-DISCOVERs SHOULD be
// offered its CURRENT address; re-selecting it must not double-allocate.
func TestRediscoverWhileLeasedKeepsAddress(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(19)
	x1 := xid(4100)
	off := discover(t, s, id, x1)
	if requestSelect(t, s, id, x1, off.LeaseIP).Action != storage.ActACK {
		t.Fatal("commit failed")
	}

	x2 := xid(4101)
	o2 := discover(t, s, id, x2)
	if o2.LeaseIP != off.LeaseIP {
		t.Fatalf("re-discover while leased offered %s, want current %s",
			o2.LeaseIP, off.LeaseIP)
	}
	// Another client still cannot reserve the leased address.
	idOther := ident(20)
	o3 := discover(t, s, idOther, xid(4102))
	if o3.LeaseIP == off.LeaseIP {
		t.Fatal("another client reserved a live-leased address")
	}
	// Re-selecting keeps exactly one live lease for the client/IP.
	if requestSelect(t, s, id, x2, o2.LeaseIP).Action != storage.ActACK {
		t.Fatal("re-select failed")
	}
	lv, _ := s.Store.LeaseByIP(context.Background(), off.LeaseIP)
	if lv.State != storage.StateLeased {
		t.Fatalf("lease state after re-select = %s", lv.State)
	}
}
