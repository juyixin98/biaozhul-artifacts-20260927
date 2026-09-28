package server

import (
	"context"
	"sync"
	"testing"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

// 1. Full DORA lifecycle: OFFER reserves (not leases); ACK commits atomically.
func TestDORA_Lifecycle(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x11)

	o := c.discover(h)
	if o.Category != storage.CatOK || o.OutType != dhcppacket.MsgOffer {
		t.Fatalf("discover outcome: %+v", o)
	}
	if !o.AssignedIP.IsValid() || o.AssignedIP.String() != "127.10.0.2" {
		t.Fatalf("offer ip=%v want lowest pool address 127.10.0.2", o.AssignedIP)
	}
	offered := decodeReply(t, o, dhcppacket.MsgOffer)
	if offered.YIAddr.String() != "127.10.0.2" {
		t.Fatalf("offer yiaddr=%s", offered.YIAddr)
	}
	// OFFER must be a reservation, NOT a bound lease.
	l := h.activeLease(t, c)
	if l == nil || l.State != storage.StateOffered {
		t.Fatalf("after offer state=%+v want OFFERED", l)
	}
	if l.BoundAt != 0 || l.Ends != 0 {
		t.Fatalf("reservation carries lease timestamps: bound=%d ends=%d", l.BoundAt, l.Ends)
	}

	ack := c.requestSelect(h, "127.10.0.2")
	if ack.Category != storage.CatOK || ack.OutType != dhcppacket.MsgAck {
		t.Fatalf("select ack outcome: %+v", ack)
	}
	ackPkt := decodeReply(t, ack, dhcppacket.MsgAck)
	if ackPkt.YIAddr.String() != "127.10.0.2" {
		t.Fatalf("ack yiaddr=%s", ackPkt.YIAddr)
	}
	if lt, ok := ackPkt.Options[dhcppacket.OptLeaseTime]; !ok || len(lt) != 4 {
		t.Fatalf("ack missing lease time option: %x", lt)
	}
	l = h.activeLease(t, c)
	if l == nil || l.State != storage.StateBound {
		t.Fatalf("after ack state=%+v want BOUND", l)
	}
	if l.Ends != h.now().Add(h.cfg.Lease.LeaseTime.Duration).UnixNano() {
		t.Fatalf("ends=%d want %d", l.Ends, h.now().Add(h.cfg.Lease.LeaseTime.Duration).UnixNano())
	}
}

// 2. Selecting REQUEST for a never-offered address is NAKed, not ACKed.
func TestRequestWithoutOfferGetsNAK(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x22)
	o := c.requestSelect(h, "127.10.0.5")
	if o.Category != storage.CatNAK || o.OutType != dhcppacket.MsgNak {
		t.Fatalf("expected NAK, got %+v", o)
	}
	if o.Reason != "nak_no_valid_offer" {
		t.Fatalf("reason=%s", o.Reason)
	}
	nak := decodeReply(t, o, dhcppacket.MsgNak)
	if nak.YIAddr.String() != "0.0.0.0" {
		t.Fatalf("NAK must carry yiaddr=0, got %s", nak.YIAddr)
	}
	if _, hasLease := nak.Options[dhcppacket.OptLeaseTime]; hasLease {
		t.Fatal("NAK must not carry lease-time option")
	}
	if l := h.activeLease(t, c); l != nil {
		t.Fatalf("NAK created state: %+v", l)
	}
}

// 3. REQUEST naming a different server-id is silently ignored (server selection).
func TestRequestOtherServerSilentlyIgnored(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x33)
	if o := c.discover(h); o.Category != storage.CatOK {
		t.Fatalf("discover: %+v", o)
	}
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).
		RequestedIP(mustAddr("127.10.0.2")).ServerID(mustAddr("127.9.9.9")).
		Packet()
	o := h.send(p)
	if o.Category != storage.CatNoReply || o.Reason != "not_selected_server" {
		t.Fatalf("expected silent ignore, got %+v", o)
	}
	if o.Reply != nil {
		t.Fatal("silent-ignore must not produce a reply")
	}
	// Reservation remains OFFERED (untouched by the foreign-server REQUEST).
	if l := h.activeLease(t, c); l == nil || l.State != storage.StateOffered {
		t.Fatalf("reservation changed: %+v", l)
	}
}

// 4. Duplicate DISCOVER and duplicate REQUEST replay identical replies and do
// not extend any unauthorized lease window.
func TestDuplicatePacketsReplayWithoutExtension(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x44)

	// DISCOVER once, then resend the exact same packet bytes.
	p1, first := c.discoverRaw(h)
	if first.Category != storage.CatOK || first.Duplicate {
		t.Fatalf("first discover: %+v", first)
	}
	firstExp := h.activeLease(t, c).OfferExp

	h.clock.Advance(1_000_000_000) // +1s wall time; retransmit
	replay := h.send(p1)
	if !replay.Duplicate || replay.Reason != "identical_retransmission" {
		t.Fatalf("retransmit not recognized as duplicate: %+v", replay)
	}
	if string(replay.Reply) != string(first.Reply) {
		t.Fatal("replayed OFFER bytes differ from original")
	}
	secondExp := h.activeLease(t, c).OfferExp
	if secondExp != firstExp {
		t.Fatalf("retransmitted DISCOVER moved offer expiry %d -> %d", firstExp, secondExp)
	}

	// Commit, then retransmit the identical SELECTING REQUEST.
	var xid uint32
	c.n++
	xid = uint32(int(c.mac[5])<<24 | c.n)
	ack := c.requestSelectXID(h, xid, "127.10.0.2")
	if ack.Category != storage.CatOK {
		t.Fatalf("ack: %+v", ack)
	}
	lease := h.activeLease(t, c)
	ends1 := lease.Ends
	renew1 := lease.RenewCount

	h.clock.Advance(2_000_000_000)
	ackReplay := c.requestSelectXID(h, xid, "127.10.0.2")
	if !ackReplay.Duplicate {
		t.Fatalf("retransmitted REQUEST must be a replay, got %+v", ackReplay)
	}
	if string(ackReplay.Reply) != string(ack.Reply) {
		t.Fatal("replayed ACK bytes differ from original")
	}
	lease = h.activeLease(t, c)
	if lease.Ends != ends1 || lease.RenewCount != renew1 {
		t.Fatalf("duplicate REQUEST extended lease: ends %d->%d renew %d->%d",
			ends1, lease.Ends, renew1, lease.RenewCount)
	}
}

// 4b. A same-xid retransmission that only bumps the secs field must still be
// recognized as a duplicate and must not extend the committed lease.
func TestRetransmitWithBumpedSecsStillDedup(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x45)
	if o := c.discover(h); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	xid := c.nextXID()
	req := dhcppacket.NewRequest(xid, c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).
		RequestedIP(mustAddr("127.10.0.2")).ServerID(mustAddr("127.0.0.1")).Packet()
	ack := h.send(req)
	if ack.Category != storage.CatOK {
		t.Fatalf("ack: %+v", ack)
	}
	ends1 := h.activeLease(t, c).Ends

	h.clock.Advance(2_000_000_000)
	retrans := dhcppacket.NewRequest(xid, c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).Secs(9).
		RequestedIP(mustAddr("127.10.0.2")).ServerID(mustAddr("127.0.0.1")).Packet()
	o := h.send(retrans)
	if !o.Duplicate {
		t.Fatalf("secs-bumped same-xid REQUEST must dedup: %+v", o)
	}
	if ends2 := h.activeLease(t, c).Ends; ends2 != ends1 {
		t.Fatalf("dedup with changed secs extended lease: %d -> %d", ends1, ends2)
	}
}

// 5. Renewal (ciaddr, no server-id) extends the lease; INIT-REBOOT does not.
func TestRenewalExtendsButRebootDoesNot(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x55)
	if o := c.discover(h); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	if o := c.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatalf("select: %+v", o)
	}
	l0 := h.activeLease(t, c)
	ends0 := l0.Ends

	// Advance 5s (half the 10s lease); renew extends from "now".
	h.clock.Advance(5_000_000_000)
	ren := c.requestRenew(h, "127.10.0.2")
	if ren.Category != storage.CatOK || ren.Reason != "lease_renewed" {
		t.Fatalf("renew outcome: %+v", ren)
	}
	l1 := h.activeLease(t, c)
	if l1.Ends <= ends0 {
		t.Fatalf("renew did not extend: %d <= %d", l1.Ends, ends0)
	}
	if l1.RenewCount != 1 {
		t.Fatalf("renew count=%d", l1.RenewCount)
	}

	// INIT-REBOOT (fresh client stack, no server-id, option 50) confirms the
	// lease with ORIGINAL boundaries — no extension.
	endsAfterRenew := l1.Ends
	h.clock.Advance(1_000_000_000)
	boot := c.requestReboot(h, "127.10.0.2")
	if boot.Category != storage.CatOK || boot.Reason != "reboot_confirmed_existing_lease" {
		t.Fatalf("init-reboot outcome: %+v", boot)
	}
	l2 := h.activeLease(t, c)
	if l2.Ends != endsAfterRenew {
		t.Fatalf("INIT-REBOOT moved lease end: %d -> %d", endsAfterRenew, l2.Ends)
	}
}

// 6. Renewal of an expired/non-owned lease is NAKed and forces re-DISCOVER.
func TestRenewExpiredLeaseNAK(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.10")
	c := newClient(0x66)
	if o := c.discover(h); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	if o := c.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	// Past lease end.
	h.clock.Advance(11_000_000_000)
	o := c.requestRenew(h, "127.10.0.2")
	if o.Category != storage.CatNAK || o.Reason != "nak_lease_expired" {
		t.Fatalf("expired renew: %+v", o)
	}
	// Sweep frees the address; a new DISCOVER can take it.
	if _, err := h.srv.Sweep(context.Background(), h.runID); err != nil {
		t.Fatal(err)
	}
	if l := h.activeLease(t, c); l != nil {
		t.Fatalf("expired lease still active: %+v", l)
	}
}

// 7. Concurrent DISCOVER for a small pool: unique addresses, no duplicates.
func TestConcurrentDiscoverUniqueAllocation(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.4") // 3 addresses
	const n = 6
	results := make([]*Outcome, n)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c := newClient(byte(0x70 + i))
			<-start
			results[i] = c.discover(h)
		}(i)
	}
	close(start)
	wg.Wait()

	assigned := map[string]bool{}
	oks, fulls := 0, 0
	for i, o := range results {
		switch {
		case o.Category == storage.CatOK:
			oks++
			ip := o.AssignedIP.String()
			if assigned[ip] {
				t.Fatalf("address %s assigned twice (client %d)", ip, i)
			}
			assigned[ip] = true
		case o.Category == storage.CatPoolFull:
			fulls++
		default:
			t.Fatalf("client %d unexpected outcome: %+v", i, o)
		}
	}
	if oks != 3 || fulls != 3 {
		t.Fatalf("oks=%d fulls=%d (want 3 each)", oks, fulls)
	}
	off, bound, _ := h.store.ActiveAddressCount(context.Background())
	if off != 3 || bound != 0 {
		t.Fatalf("counts offered=%d bound=%d", off, bound)
	}
}

// 8. Two clients racing DISCOVER then REQUEST for the same offer: loser NAK.
func TestSelectingContentionLoserNAK(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.3")
	a := newClient(0x81)
	b := newClient(0x82)

	oa := a.discover(h)
	if oa.AssignedIP.String() != "127.10.0.2" {
		t.Fatalf("A offer: %v", oa.AssignedIP)
	}
	ob := b.discover(h)
	if ob.AssignedIP.String() != "127.10.0.3" {
		t.Fatalf("B offer: %v", ob.AssignedIP)
	}
	// A commits .2; B then tries to SELECT .2 which it was never offered.
	if o := a.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatalf("A commit: %+v", o)
	}
	o := b.requestSelect(h, "127.10.0.2")
	if o.Category != storage.CatNAK || o.Reason != "nak_address_taken" {
		t.Fatalf("B cross-request: %+v", o)
	}
	// B's own offer is still reservable/committable.
	if o := b.requestSelect(h, "127.10.0.3"); o.Category != storage.CatOK {
		t.Fatalf("B commit own offer: %+v", o)
	}
}

// 9. Expired OFFER releases the address; a stale REQUEST against it is NAKed.
func TestExpiredOfferThenStaleRequestNAK(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.5")
	c := newClient(0x91)
	if o := c.discover(h); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	h.clock.Advance(4_000_000_000) // offer ttl is 3s
	if _, err := h.srv.Sweep(context.Background(), h.runID); err != nil {
		t.Fatal(err)
	}
	o := c.requestSelect(h, "127.10.0.2")
	if o.Category != storage.CatNAK {
		t.Fatalf("stale request after offer expiry: %+v", o)
	}
}

// 10. RELEASE returns the address to the pool and is owner-checked; a stranger
// cannot release, the owner's duplicate release is idempotent.
func TestReleaseFlow(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.4")
	owner := newClient(0xa1)
	stranger := newClient(0xa2)
	if o := owner.discover(h); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	if o := owner.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	// Stranger cannot release .2.
	o := stranger.release(h, "127.10.0.2")
	if o.Category != storage.CatNoReply || o.Reason != "release_not_owner" {
		t.Fatalf("stranger release: %+v", o)
	}
	if l := h.activeLease(t, owner); l == nil || l.State != storage.StateBound {
		t.Fatal("stranger release mutated lease")
	}
	// Owner releases: silent, state -> RELEASED, address back in pool.
	o = owner.release(h, "127.10.0.2")
	if o.Category != storage.CatOK || o.Reply != nil {
		t.Fatalf("owner release should be ok with no reply: %+v", o)
	}
	if l := h.activeLease(t, owner); l != nil {
		t.Fatalf("lease active after release: %+v", l)
	}
	// The released address is re-offerable to a new client.
	if o := stranger.discover(h); o.Category != storage.CatOK || o.AssignedIP.String() != "127.10.0.2" {
		t.Fatalf("reuse after release: %+v", o)
	}
}

// 12. A client that reboots with a DIFFERENT client-id on the same MAC must
// not inherit the prior identity's lease: INIT-REBOOT is NAKed.
func TestRebootWithDifferentClientIDNotInherited(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.5")
	c := newClient(0xc1)
	if o := c.discover(h); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	if o := c.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatal(o)
	}
	// Same MAC, different option 61 -> different canonical identity.
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).
		ClientID(append([]byte{2}, c.hardwareAddr()...)). // different client-id type/value
		RequestedIP(mustAddr("127.10.0.2")).Packet()
	o := h.send(p)
	if o.Category != storage.CatNAK || o.Reason != "nak_unknown_lease_on_reboot" {
		t.Fatalf("identity change on reboot must not inherit lease: %+v", o)
	}
	// Original identity's lease is untouched.
	if l := h.activeLease(t, c); l == nil || l.State != storage.StateBound {
		t.Fatalf("original lease mutated: %+v", l)
	}
}

// 14. After a client's lease EXPIRES, the SAME client may run DORA again and
// legitimately receive the same address (historical rows must not block it).
func TestSameClientReacquiresSameIPAfterExpiry(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.5")
	c := newClient(0xe1)
	if o := c.discover(h); o.Category != storage.CatOK || o.AssignedIP.String() != "127.10.0.2" {
		t.Fatalf("discover: %+v", o)
	}
	if o := c.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatalf("commit: %+v", o)
	}
	// Lease is 10s; advance and sweep to expire.
	h.clock.Advance(11_000_000_000)
	if _, err := h.srv.Sweep(context.Background(), h.runID); err != nil {
		t.Fatal(err)
	}
	if l := h.activeLease(t, c); l != nil {
		t.Fatalf("lease should be gone: %+v", l)
	}
	// Fresh DORA (new xids) — same client explicitly prefers .2.
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgDiscover).ClientID(c.cid).
		RequestedIP(mustAddr("127.10.0.2")).Packet()
	o := h.send(p)
	if o.Category != storage.CatOK || o.AssignedIP.String() != "127.10.0.2" {
		t.Fatalf("same client cannot re-offer its expired ip: %+v", o)
	}
	// Selecting against the fresh offer commits.
	if o := c.requestSelect(h, "127.10.0.2"); o.Category != storage.CatOK {
		t.Fatalf("re-commit: %+v", o)
	}
}

// 13. DISCOVER with an explicit requested-ip that is free reserves exactly it.
func TestDiscoverHonorsFreeRequestedIP(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.20")
	c := newClient(0xd1)
	p := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgDiscover).ClientID(c.cid).
		RequestedIP(mustAddr("127.10.0.9")).Packet()
	o := h.send(p)
	if o.Category != storage.CatOK || o.AssignedIP.String() != "127.10.0.9" {
		t.Fatalf("requested free ip not honored: %+v", o)
	}
	// Requesting an occupied address falls back to the lowest free one.
	c2 := newClient(0xd2)
	p2 := dhcppacket.NewRequest(c2.nextXID(), c2.hardwareAddr()).
		Type(dhcppacket.MsgDiscover).ClientID(c2.cid).
		RequestedIP(mustAddr("127.10.0.9")).Packet()
	o2 := h.send(p2)
	if o2.Category != storage.CatOK || o2.AssignedIP.String() != "127.10.0.2" {
		t.Fatalf("occupied requested ip should fall back: %+v", o2)
	}
}

// 11. Malformed and unsupported messages get explicit failure categories.
func TestMalformedAndUnsupportedCategories(t *testing.T) {
	h := newHarness(t, "127.10.0.2", "127.10.0.4")
	c := newClient(0xb1)

	inform := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgInform).ClientID(c.cid).CIAddr(mustAddr("127.10.0.2")).Packet()
	o := h.send(inform)
	if o.Category != storage.CatUnsupported || o.Reason != "unsupported_message_type" {
		t.Fatalf("INFORM: %+v", o)
	}

	bad := dhcppacket.NewRequest(c.nextXID(), c.hardwareAddr()).
		Type(dhcppacket.MsgRequest).ClientID(c.cid).Packet()
	// No server-id, no ciaddr, no requested-ip -> malformed framing.
	o = h.send(bad)
	if o.Category != storage.CatNAK || o.Reason != "nak_malformed_request" {
		t.Fatalf("bare REQUEST: %+v", o)
	}
}
