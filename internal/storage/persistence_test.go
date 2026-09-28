package storage_test

import (
	"context"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/ippool"
	"dhcp4lab/internal/storage"
	"dhcp4lab/testfixture/testhelp"
)

// TestPersistenceAcrossRestart commits a lease, closes the database file,
// reopens it with a fresh Store, and verifies the lease is still live with
// its original expiry — and that a renew after restart extends it.
func TestPersistenceAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "dhcp4lab.db")
	dsn := "file:" + dbPath + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)"

	pool, err := ippool.New(netip.MustParseAddr("192.0.2.10"), netip.MustParseAddr("192.0.2.20"))
	if err != nil {
		t.Fatal(err)
	}
	sid := netip.MustParseAddr("192.0.2.1")

	fc := testhelp.NewFakeClock()
	open := func() (*storage.Store, func()) {
		db, err := storage.OpenDB(context.Background(), dsn)
		if err != nil {
			t.Fatalf("open: %v", err)
		}
		st, err := storage.New(context.Background(), storage.Options{
			DB: db, Pool: pool, ServerID: sid,
			LeaseTime: 120 * time.Second, OfferTTL: 30 * time.Second, Clock: fc,
		})
		if err != nil {
			t.Fatalf("new: %v", err)
		}
		return st, func() { _ = st.Close() }
	}

	st1, close1 := open()
	id := dhcp4.ClientIdentity{HType: 1, CHAddr: [6]byte{2, 0, 0, 0, 0, 77}}
	x := xid(5000)
	off, err := st1.Discover(context.Background(), storage.DiscoverInput{
		XID: x, Identity: id, CHAddr: id.CHAddr,
	})
	if err != nil {
		t.Fatal(err)
	}
	if off.Action != storage.ActOffer {
		t.Fatalf("discover: %s", off.Action)
	}
	ack, err := st1.Request(context.Background(), storage.RequestInput{
		XID: x, Kind: storage.ReqSelecting, Identity: id, CHAddr: id.CHAddr,
		ServerID: sid, RequestedIP: off.LeaseIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	if ack.Action != storage.ActACK {
		t.Fatalf("request: %s", ack.Reason)
	}
	leasedAt := fc.Now()
	close1()

	// "Restart" with a brand new Store on the same file.
	st2, close2 := open()
	defer close2()
	lv, err := st2.LeaseByIP(context.Background(), off.LeaseIP)
	if err != nil {
		t.Fatalf("query after restart: %v", err)
	}
	if lv == nil || lv.State != storage.StateLeased {
		t.Fatalf("lease after restart = %+v, want live leased", lv)
	}
	wantExpiry := leasedAt.Add(120 * time.Second)
	if !lv.ExpiresAt.Equal(wantExpiry) {
		t.Fatalf("expiry after restart = %s, want %s", lv.ExpiresAt, wantExpiry)
	}

	// A renew after restart must work against the recovered state.
	fc.Advance(40 * time.Second)
	renew, err := st2.Request(context.Background(), storage.RequestInput{
		XID: xid(5001), Kind: storage.ReqRenew, Identity: id, CHAddr: id.CHAddr,
		CIAddr: off.LeaseIP,
	})
	if err != nil {
		t.Fatal(err)
	}
	if renew.Action != storage.ActACK {
		t.Fatalf("renew after restart: %s/%s", renew.Action, renew.Reason)
	}

	// A discover after restart must insert an events row successfully.
	// This specifically guards against primary-key counters that reset
	// per-process and collide with persisted ids.
	if _, err := st2.Discover(context.Background(), storage.DiscoverInput{
		XID: xid(5002), Identity: id, CHAddr: id.CHAddr,
	}); err != nil {
		t.Fatalf("discover after restart (events insert): %v", err)
	}
}

// TestPersistenceFreshProcess re-runs the lease-recovery phase in a
// freshly executed test binary (via TestMain re-exec), so all package
// globals — including any in-process id counters — start at zero just
// like a real deployment restart. It catches bugs an in-process
// "reopen the DB" test cannot.
func TestPersistenceFreshProcess(t *testing.T) {
	if os.Getenv("DHCP4LAB_FRESH_PROC") == "1" {
		persistFreshProcessRecovery(t)
		return
	}
	dbPath := filepath.Join(t.TempDir(), "fresh.db")
	dsn := "file:" + dbPath + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)"

	// Phase 1: seed a lease in this process.
	pool, _ := ippool.New(netip.MustParseAddr("192.0.2.10"), netip.MustParseAddr("192.0.2.20"))
	sid := netip.MustParseAddr("192.0.2.1")
	fc := testhelp.NewFakeClock()
	db, err := storage.OpenDB(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	st, err := storage.New(context.Background(), storage.Options{
		DB: db, Pool: pool, ServerID: sid,
		LeaseTime: 120 * time.Second, OfferTTL: 30 * time.Second, Clock: fc,
	})
	if err != nil {
		t.Fatal(err)
	}
	id := dhcp4.ClientIdentity{HType: 1, CHAddr: [6]byte{2, 0, 0, 0, 0, 88}}
	x := xid(6000)
	o, err := st.Discover(context.Background(), storage.DiscoverInput{
		XID: x, Identity: id, CHAddr: id.CHAddr})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Request(context.Background(), storage.RequestInput{
		XID: x, Kind: storage.ReqSelecting, Identity: id, CHAddr: id.CHAddr,
		ServerID: sid, RequestedIP: o.LeaseIP}); err != nil {
		t.Fatal(err)
	}
	_ = st.Close()
	_ = db.Close()

	// Phase 2: a brand new process recovers and mutates (inserts events).
	cmd := exec.Command(os.Args[0], "-test.run=^TestPersistenceFreshProcess$", "-test.count=1")
	cmd.Env = append(os.Environ(),
		"DHCP4LAB_FRESH_PROC=1",
		"DHCP4LAB_DSN="+dsn,
		"DHCP4LAB_IP="+o.LeaseIP.String())
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("fresh-process recovery failed: %v\n%s", err, out)
	}
}

func persistFreshProcessRecovery(t *testing.T) {
	dsn := os.Getenv("DHCP4LAB_DSN")
	leased := netip.MustParseAddr(os.Getenv("DHCP4LAB_IP"))
	pool, _ := ippool.New(netip.MustParseAddr("192.0.2.10"), netip.MustParseAddr("192.0.2.20"))
	sid := netip.MustParseAddr("192.0.2.1")
	fc := testhelp.NewFakeClock()
	db, err := storage.OpenDB(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	st, err := storage.New(context.Background(), storage.Options{
		DB: db, Pool: pool, ServerID: sid,
		LeaseTime: 120 * time.Second, OfferTTL: 30 * time.Second, Clock: fc,
	})
	if err != nil {
		t.Fatal(err)
	}
	id := dhcp4.ClientIdentity{HType: 1, CHAddr: [6]byte{2, 0, 0, 0, 0, 88}}
	lv, err := st.LeaseByIP(context.Background(), leased)
	if err != nil || lv == nil || lv.State != storage.StateLeased {
		t.Fatalf("recovered lease = %+v err=%v", lv, err)
	}
	// This mutation inserts events/replies rows; a reset in-process PK
	// counter would fail here with a UNIQUE constraint error.
	if _, err := st.Discover(context.Background(), storage.DiscoverInput{
		XID: xid(6001), Identity: id, CHAddr: id.CHAddr}); err != nil {
		t.Fatalf("discover in fresh process: %v", err)
	}
}
