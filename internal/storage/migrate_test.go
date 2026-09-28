package storage_test

import (
	"context"
	"database/sql"
	"net/netip"
	"path/filepath"
	"testing"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/ippool"
	"dhcp4lab/internal/storage"
	"dhcp4lab/testfixture/testhelp"

	_ "modernc.org/sqlite"
)

// TestMigratesRepliesRefColumns creates a database with the ORIGINAL
// replies table (no ref_kind/ref_id), writes a row through it, then opens
// it with the current code. The additive migration must add the columns
// and the state machine must then work end to end.
func TestMigratesRepliesRefColumns(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "old.db")
	dsn := "file:" + dbPath + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)"

	// Build a legacy replies table (pre-ref-tracking columns).
	legacy, err := sql.Open("sqlite", dsn+"&_txlock=immediate")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Exec(`CREATE TABLE replies (
	    id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL,
	    client_key TEXT NOT NULL, xid BLOB NOT NULL, recv_type TEXT NOT NULL,
	    action TEXT NOT NULL, reply_type TEXT NOT NULL DEFAULT '',
	    offered_ip BLOB, lease_expires INTEGER NOT NULL DEFAULT 0,
	    reason TEXT NOT NULL DEFAULT '', reply_bytes BLOB)`); err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Exec(
		`INSERT INTO replies(created_at,client_key,xid,recv_type,action)
		 VALUES(1,'legacy-client',X'01020304','DISCOVER','offer')`); err != nil {
		t.Fatal(err)
	}
	_ = legacy.Close()

	// Open with current code: migration should add ref_kind/ref_id.
	pool, err := ippool.New(netip.MustParseAddr("192.0.2.10"), netip.MustParseAddr("192.0.2.20"))
	if err != nil {
		t.Fatal(err)
	}
	fc := testhelp.NewFakeClock()
	db, err := storage.OpenDB(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	st, err := storage.New(context.Background(), storage.Options{
		DB: db, Pool: pool, ServerID: netip.MustParseAddr("192.0.2.1"),
		LeaseTime: 120_000_000_000, OfferTTL: 30_000_000_000, Clock: fc,
	})
	if err != nil {
		t.Fatalf("open/migrate legacy db: %v", err)
	}
	defer st.Close()

	// Confirm columns exist and the legacy row defaulted to ref_kind=''.
	var refKind string
	if err := db.QueryRow(
		`SELECT ref_kind FROM replies WHERE client_key='legacy-client'`).Scan(&refKind); err != nil {
		t.Fatalf("read migrated row: %v", err)
	}
	if refKind != "" {
		t.Fatalf("legacy ref_kind = %q, want empty", refKind)
	}

	// New transactions must populate ref tracking and still work.
	id := dhcp4.ClientIdentity{HType: 1, CHAddr: [6]byte{2, 0, 0, 0, 0, 91}}
	x := xid(9500)
	o, err := st.Discover(context.Background(), storage.DiscoverInput{
		XID: x, Identity: id, CHAddr: id.CHAddr})
	if err != nil {
		t.Fatal(err)
	}
	if o.Action != storage.ActOffer {
		t.Fatalf("discover after migrate: %s", o.Action)
	}
	ack, err := st.Request(context.Background(), storage.RequestInput{
		XID: x, Kind: storage.ReqSelecting, Identity: id, CHAddr: id.CHAddr,
		ServerID: netip.MustParseAddr("192.0.2.1"), RequestedIP: o.LeaseIP})
	if err != nil {
		t.Fatal(err)
	}
	if ack.Action != storage.ActACK {
		t.Fatalf("request after migrate: %s/%s", ack.Action, ack.Reason)
	}
}
