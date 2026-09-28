package storage

import (
	"context"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	dsn := "file:" + filepath.Join(t.TempDir(), "test.db")
	s, err := Open(dsn, true)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestReserveAndCommitUniqueAddress(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Unix(1_700_000_000, 0)
	identA := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0x01})
	identB := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0x02})

	rA, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: identA, XID: 1, IP: "127.10.0.2",
		NowNanos: now.UnixNano(), OfferExpNanos: now.Add(time.Minute).UnixNano(),
	})
	if err != nil || rA.Status != "ok" || rA.IP != "127.10.0.2" {
		t.Fatalf("reserve A: %+v err=%v", rA, err)
	}
	// B must fail to reserve the same address.
	rB, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: identB, XID: 2, IP: "127.10.0.2",
		NowNanos: now.UnixNano(), OfferExpNanos: now.Add(time.Minute).UnixNano(),
	})
	if err != nil {
		t.Fatalf("reserve B: %v", err)
	}
	if rB.Status != "lost_to_other" {
		t.Fatalf("B status=%s want lost_to_other", rB.Status)
	}

	// A commits atomically.
	cA, err := s.CommitAck(ctx, CommitAckInput{
		Identity: identA, XID: 3, IP: "127.10.0.2",
		NowNanos: now.UnixNano(), StartNanos: now.UnixNano(),
		EndNanos: now.Add(10 * time.Minute).UnixNano(),
	})
	if err != nil || cA.Status != "committed" {
		t.Fatalf("commit A: %+v err=%v", cA, err)
	}
	if cA.Lease == nil || cA.Lease.State != StateBound {
		t.Fatalf("lease not BOUND: %+v", cA.Lease)
	}
	// B still cannot reserve the bound address.
	rB2, _ := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: identB, XID: 4, IP: "127.10.0.2",
		NowNanos: now.UnixNano(), OfferExpNanos: now.Add(time.Minute).UnixNano(),
	})
	if rB2.Status != "lost_to_other" {
		t.Fatalf("B reserve bound ip status=%s", rB2.Status)
	}
}

func TestConcurrentContentionSingleAddress(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Unix(1_700_000_100, 0)

	const n = 24
	var wg sync.WaitGroup
	results := make([]string, n)
	errs := make([]error, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			mac := []byte{0x02, 0, 0, 0, byte(i >> 8), byte(i)}
			ident := IdentityFromCHAddr(mac)
			<-start
			r, err := s.ReserveOffer(ctx, ReserveOfferInput{
				Identity: ident, XID: uint32(i + 100), IP: "127.10.0.7",
				NowNanos: now.UnixNano(), OfferExpNanos: now.Add(time.Minute).UnixNano(),
			})
			results[i] = r.Status
			errs[i] = err
		}(i)
	}
	close(start)
	wg.Wait()

	winners, losers := 0, 0
	for i, st := range results {
		if errs[i] != nil {
			t.Fatalf("client %d error: %v", i, errs[i])
		}
		switch st {
		case "ok":
			winners++
		case "lost_to_other":
			losers++
		default:
			t.Fatalf("client %d unexpected status %q", i, st)
		}
	}
	if winners != 1 || losers != n-1 {
		t.Fatalf("winners=%d losers=%d (want exactly 1 winner)", winners, losers)
	}
	offered, bound, err := s.ActiveAddressCount(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if offered != 1 || bound != 0 {
		t.Fatalf("counts offered=%d bound=%d", offered, bound)
	}
}

func TestRenewalDoesNotChangeOwner(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	t0 := time.Unix(1_700_000_200, 0)
	ident := IdentityFromClientID(append([]byte{1}, 2, 3, 4, 5, 6))
	r, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: ident, XID: 1, IP: "127.10.0.3",
		NowNanos: t0.UnixNano(), OfferExpNanos: t0.Add(time.Minute).UnixNano(),
	})
	if err != nil || r.Status != "ok" {
		t.Fatalf("reserve: %+v %v", r, err)
	}
	c1, err := s.CommitAck(ctx, CommitAckInput{
		Identity: ident, XID: 2, IP: "127.10.0.3",
		NowNanos: t0.UnixNano(), StartNanos: t0.UnixNano(),
		EndNanos: t0.Add(10 * time.Minute).UnixNano(),
	})
	if err != nil || c1.Status != "committed" {
		t.Fatalf("commit: %+v %v", c1, err)
	}
	firstEnd := c1.Lease.Ends

	t1 := t0.Add(5 * time.Minute)
	c2, err := s.CommitAck(ctx, CommitAckInput{
		Identity: ident, XID: 3, IP: "127.10.0.3",
		NowNanos: t1.UnixNano(), StartNanos: t1.UnixNano(),
		EndNanos: t1.Add(10 * time.Minute).UnixNano(), RenewExisting: true,
	})
	if err != nil || c2.Status != "renewed" {
		t.Fatalf("renew: %+v %v", c2, err)
	}
	if c2.Lease.Ends <= firstEnd {
		t.Fatalf("renew did not extend ends: %d <= %d", c2.Lease.Ends, firstEnd)
	}
	if c2.Lease.RenewCount != 1 {
		t.Fatalf("renew_count=%d want 1", c2.Lease.RenewCount)
	}
	if c2.Lease.IdentityID != ident.Key {
		t.Fatal("owner changed after renew")
	}
}

func TestSweepExpiredOfferAndLease(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	t0 := time.Unix(1_700_000_300, 0)
	a := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0xaa})
	b := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0xbb})
	if _, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: a, XID: 1, IP: "127.10.0.4",
		NowNanos: t0.UnixNano(), OfferExpNanos: t0.Add(30 * time.Second).UnixNano(),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: b, XID: 2, IP: "127.10.0.5",
		NowNanos: t0.UnixNano(), OfferExpNanos: t0.Add(time.Hour).UnixNano(),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CommitAck(ctx, CommitAckInput{
		Identity: b, XID: 3, IP: "127.10.0.5",
		NowNanos: t0.UnixNano(), StartNanos: t0.UnixNano(),
		EndNanos: t0.Add(2 * time.Minute).UnixNano(),
	}); err != nil {
		t.Fatal(err)
	}

	// At t0+1min only A's offer is stale.
	changes, err := s.SweepExpired(ctx, t0.Add(time.Minute).UnixNano())
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 1 || changes[0].IP != "127.10.0.4" || changes[0].From != StateOffered {
		t.Fatalf("offer sweep changes=%+v", changes)
	}
	// Address returns to the pool.
	l, _ := s.ActiveLeaseAtIP(ctx, "127.10.0.4")
	if l != nil {
		t.Fatalf("expired offer still active: %+v", l)
	}
	// Lease survives until its own deadline.
	if l, _ := s.ActiveLeaseAtIP(ctx, "127.10.0.5"); l == nil || l.State != StateBound {
		t.Fatalf("bound lease vanished early")
	}
	changes2, _ := s.SweepExpired(ctx, t0.Add(3*time.Minute).UnixNano())
	if len(changes2) != 1 || changes2[0].From != StateBound {
		t.Fatalf("lease sweep changes=%+v", changes2)
	}
	if l, _ := s.ActiveLeaseAtIP(ctx, "127.10.0.5"); l != nil {
		t.Fatal("expired lease still active")
	}
}

func TestReleaseIdempotentAndOwnerChecked(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	t0 := time.Unix(1_700_000_400, 0)
	owner := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0x0c})
	stranger := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0x0d})
	if _, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: owner, XID: 1, IP: "127.10.0.6",
		NowNanos: t0.UnixNano(), OfferExpNanos: t0.Add(time.Minute).UnixNano(),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CommitAck(ctx, CommitAckInput{
		Identity: owner, XID: 2, IP: "127.10.0.6",
		NowNanos: t0.UnixNano(), StartNanos: t0.UnixNano(),
		EndNanos: t0.Add(10 * time.Minute).UnixNano(),
	}); err != nil {
		t.Fatal(err)
	}
	if released, gone, notOwner, err := s.ReleaseLease(ctx, stranger.Key, "127.10.0.6", t0.UnixNano()); err != nil || released || gone || !notOwner {
		t.Fatalf("stranger release: released=%v gone=%v notOwner=%v err=%v", released, gone, notOwner, err)
	}
	if l, _ := s.ActiveLeaseAtIP(ctx, "127.10.0.6"); l == nil || l.State != StateBound {
		t.Fatal("stranger release mutated the lease")
	}
	if released, _, _, err := s.ReleaseLease(ctx, owner.Key, "127.10.0.6", t0.UnixNano()); err != nil || !released {
		t.Fatalf("owner release: %v %v", released, err)
	}
	if l, _ := s.ActiveLeaseAtIP(ctx, "127.10.0.6"); l != nil {
		t.Fatal("lease still active after release")
	}
	// Duplicate release: idempotent, marked alreadyGone.
	if released, gone, notOwner, err := s.ReleaseLease(ctx, owner.Key, "127.10.0.6", t0.UnixNano()); err != nil || released || !gone || notOwner {
		t.Fatalf("dup release: released=%v gone=%v notOwner=%v err=%v", released, gone, notOwner, err)
	}
}

func TestSameIdentityCanReacquireSameIPAfterExpiry(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	t0 := time.Unix(1_700_000_500, 0)
	ident := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0xf1})
	ip := "127.10.0.9"

	// First lifecycle: offer -> commit.
	if _, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: ident, XID: 1, IP: ip,
		NowNanos: t0.UnixNano(), OfferExpNanos: t0.Add(time.Minute).UnixNano(),
	}); err != nil {
		t.Fatal(err)
	}
	if c, err := s.CommitAck(ctx, CommitAckInput{
		Identity: ident, XID: 2, IP: ip,
		NowNanos: t0.UnixNano(), StartNanos: t0.UnixNano(),
		EndNanos: t0.Add(time.Minute).UnixNano(),
	}); err != nil || c.Status != "committed" {
		t.Fatalf("commit: %+v %v", c, err)
	}
	// Lease expires and is swept.
	if _, err := s.SweepExpired(ctx, t0.Add(2*time.Minute).UnixNano()); err != nil {
		t.Fatal(err)
	}
	// Same identity re-DISCOVERs and must be able to reserve the SAME address.
	r2, err := s.ReserveOffer(ctx, ReserveOfferInput{
		Identity: ident, XID: 3, IP: ip,
		NowNanos:      t0.Add(2 * time.Minute).UnixNano(),
		OfferExpNanos: t0.Add(3 * time.Minute).UnixNano(),
	})
	if err != nil {
		t.Fatalf("re-reserve: %v", err)
	}
	if r2.Status != "ok" {
		t.Fatalf("same identity cannot reacquire same ip after expiry: status=%s", r2.Status)
	}
	l, _ := s.ActiveLeaseAtIP(ctx, ip)
	if l == nil || l.State != StateOffered || l.IdentityID != ident.Key {
		t.Fatalf("re-offered row wrong: %+v", l)
	}
	// And it commits again.
	c2, err := s.CommitAck(ctx, CommitAckInput{
		Identity: ident, XID: 4, IP: ip,
		NowNanos:   t0.Add(2 * time.Minute).UnixNano(),
		StartNanos: t0.Add(2 * time.Minute).UnixNano(),
		EndNanos:   t0.Add(3 * time.Minute).UnixNano(),
	})
	if err != nil || c2.Status != "committed" {
		t.Fatalf("re-commit: %+v %v", c2, err)
	}
}

func TestTransactionDedupReplay(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	ident := IdentityFromCHAddr([]byte{0x02, 0, 0, 0, 0, 0x0e})
	rec := TransactionRecord{
		IdentityID: ident.Key, XID: 777, Phase: "OFFER", Fingerprint: "fp1",
		InType: "DISCOVER", OutType: "OFFER", AssignedIP: "127.10.0.8",
		Reply: []byte{1, 2, 3}, LeaseEndsAt: 99, CreatedAt: 5,
	}
	inserted, prior, err := s.SaveTransaction(ctx, rec)
	if err != nil || !inserted || prior != nil {
		t.Fatalf("first save inserted=%v prior=%v err=%v", inserted, prior, err)
	}
	inserted2, prior2, err := s.SaveTransaction(ctx, rec)
	if err != nil || inserted2 || prior2 == nil {
		t.Fatalf("duplicate save inserted=%v prior=%v err=%v", inserted2, prior2, err)
	}
	if prior2.AssignedIP != "127.10.0.8" || string(prior2.Reply) != string([]byte{1, 2, 3}) {
		t.Fatalf("replayed record mismatch: %+v", prior2)
	}
	got, _ := s.LookupTransaction(ctx, ident.Key, 777, "fp1")
	if got == nil || got.OutType != "OFFER" {
		t.Fatalf("lookup failed: %+v", got)
	}
}
