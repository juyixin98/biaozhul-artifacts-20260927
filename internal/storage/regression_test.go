package storage_test

import (
	"context"
	"net/netip"
	"testing"
	"time"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/storage"
	"dhcp4lab/testfixture/testhelp"
)

// 17. CRITICAL regression: a RELEASE with ciaddr=0.0.0.0 must be a
// classified drop, never a panic that can kill the process.
func TestReleaseZeroCIAddrNoPanic(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(40)
	out, err := func() (o *storage.Outcome, err error) {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("Release(ciaddr=0) panicked: %v", r)
			}
		}()
		return s.Store.Release(context.Background(), storage.ReleaseInput{
			XID: xid(9000), Identity: id, CHAddr: id.CHAddr,
			CIAddr: netip.Addr{}, // zero/unspecified
		})
	}()
	if err != nil {
		t.Fatal(err)
	}
	if out.Action != storage.ActDrop {
		t.Fatalf("action = %s, want drop", out.Action)
	}
	if out.Reason == "" {
		t.Fatal("drop must carry a classified reason")
	}
	if !contains(out.Reason, storage.ReasonMalformed) {
		t.Fatalf("reason = %q, want it to include %q", out.Reason, storage.ReasonMalformed)
	}
}

// 18. A stale NAK dedup row must not poison a later valid REQUEST: after a
// NAK (no offer), a DISCOVER creates an offer for the same xid, and the
// REQUEST now commits with an ACK.
func TestNAKDedupDoesNotPoisonLaterValidRequest(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(41)
	x := xid(9100)

	early := requestSelect(t, s, id, x, netip.MustParseAddr("192.0.2.10"))
	if early.Action != storage.ActNAK {
		t.Fatalf("early = %s, want NAK", early.Action)
	}
	off := discover(t, s, id, x)
	if off.Action != storage.ActOffer {
		t.Fatalf("discover: %s", off.Action)
	}
	late := requestSelect(t, s, id, x, off.LeaseIP)
	if late.Action != storage.ActACK {
		t.Fatalf("late = %s/%s, want ACK (stale NAK poisoned a valid REQUEST)",
			late.Action, late.Reason)
	}
}

// 19. OFFER generation: replay an OLD DISCOVER xid after a NEWER DISCOVER
// superseded its offer (even with the SAME address). The old xid's
// reservation is dead, so the replay must be processed afresh and must
// NOT re-emit an OFFER tied to the dead row carrying a stale expiry.
func TestOfferGenerationStaleAfterNewerDiscover(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(42)
	x1 := xid(9200)
	x2 := xid(9201)

	o1 := discover(t, s, id, x1)
	if o1.Action != storage.ActOffer {
		t.Fatalf("o1: %s", o1.Action)
	}
	o2 := discover(t, s, id, x2) // supersedes O1, same address preferred
	if o2.LeaseIP != o1.LeaseIP {
		t.Fatalf("new discover moved address %s -> %s", o1.LeaseIP, o2.LeaseIP)
	}
	// Time passes so a fresh reservation has a distinct deadline.
	s.Clock.Advance(5 * time.Second)
	// Replay the OLD xid. O1 is superseded; this is a fresh transaction
	// that supersedes O2 and reserves a current offer. It must NOT be a
	// duplicate carrying O1's original expiry.
	replay := discover(t, s, id, x1)
	if replay.Duplicate {
		t.Fatal("replayed superseded xid flagged duplicate (dead offer generation)")
	}
	if replay.Action != storage.ActOffer {
		t.Fatalf("replay action = %s", replay.Action)
	}
	if !replay.LeaseExpires.After(o1.LeaseExpires) {
		t.Fatalf("replay expiry %s not after original %s (stale expiry replayed)",
			replay.LeaseExpires, o1.LeaseExpires)
	}
}

// 20. ACK generation: after the lease granted by an old ACK ends and the
// client later re-acquires the SAME address under a new xid/lease row,
// replaying the OLD xid must not resurrect a phantom ACK for the dead
// lease — it is a fresh transaction and must NAK (no live offer for it).
func TestACKGenerationStaleAfterLeaseEndedAndReacquired(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	id := ident(43)
	x1 := xid(9300)

	o1 := discover(t, s, id, x1)
	if requestSelect(t, s, id, x1, o1.LeaseIP).Action != storage.ActACK {
		t.Fatal("first commit failed")
	}
	// End the first lease explicitly.
	if _, err := s.Store.Release(context.Background(), storage.ReleaseInput{
		XID: xid(9301), Identity: id, CHAddr: id.CHAddr, CIAddr: o1.LeaseIP,
	}); err != nil {
		t.Fatal(err)
	}
	// Re-acquire the SAME address under a NEW generation.
	x2 := xid(9302)
	o2 := discover(t, s, id, x2)
	if o2.LeaseIP != o1.LeaseIP {
		t.Fatalf("re-acquire offered different address: %s vs %s",
			o2.LeaseIP, o1.LeaseIP)
	}
	if requestSelect(t, s, id, x2, o2.LeaseIP).Action != storage.ActACK {
		t.Fatal("second commit failed")
	}
	// Replay the OLD xid. The lease its ACK granted (L1) is released even
	// though a newer live lease (L2) for the same IP exists.
	replay := requestSelect(t, s, id, x1, o1.LeaseIP)
	if replay.Duplicate {
		t.Fatal("old xid ACK replayed via newer lease generation (phantom ACK)")
	}
	if replay.Action != storage.ActNAK {
		t.Fatalf("old xid replay = %s/%s, want fresh NAK (no live offer for x1)",
			replay.Action, replay.Reason)
	}
	// The live L2 lease must be untouched.
	lv, _ := s.Store.LeaseByIP(context.Background(), o1.LeaseIP)
	if lv.State != storage.StateLeased {
		t.Fatalf("live L2 lease disturbed: state=%s", lv.State)
	}
}

// 21. The adapter must drop a RELEASE with ciaddr=0 before it reaches
// the store, and must never emit a reply.
func TestAdapterReleaseZeroCIAddrDrop(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	x := xid(9400)
	var chaddr [6]byte = [6]byte{2, 0, 0, 0, 0, 44}
	pkt := &dhcp4.Packet{
		Op: dhcp4.OpBootRequest, HType: 1, HLen: 6, XID: x,
		CHAddr:  chaddr,
		Options: map[dhcp4.OptionCode][]byte{dhcp4.OptMessageType: {byte(dhcp4.MsgRelease)}},
		// CIAddr left zero
	}
	dec, err := s.Server.Handle(context.Background(), pkt)
	if err != nil {
		t.Fatal(err)
	}
	if dec.Outcome.Action != storage.ActDrop || !contains(dec.Outcome.Reason, storage.ReasonMalformed) {
		t.Fatalf("adapter release ciaddr=0 = %s/%s, want drop/malformed",
			dec.Outcome.Action, dec.Outcome.Reason)
	}
	if dec.ReplyBytes != nil {
		t.Fatal("malformed RELEASE must not produce a reply")
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
