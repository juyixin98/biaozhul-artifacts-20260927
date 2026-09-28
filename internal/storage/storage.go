// Package storage is the durable DHCPv4 lease state machine. All
// state-changing decisions (OFFER reservation, REQUEST commit/renew,
// RELEASE, expiry) are made and their effects written inside a single
// SQLite BEGIN IMMEDIATE transaction so that address uniqueness and the
// reserved != leased distinction hold under concurrency and crashes.
//
// The package returns explicit, classified Outcome values. "I do not
// know" is modelled as Drop/NAK with a Reason, never as success.
package storage

import (
	"context"
	"database/sql"
	_ "embed"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"sync"
	"time"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/ippool"

	// Pure-Go SQLite: fixed-version dependency, no cgo requirement, so
	// the lab builds identically in restricted environments.
	_ "modernc.org/sqlite"
)

// Clock abstracts wall time so tests can deterministically expire offers
// and leases without sleeping.
type Clock interface {
	Now() time.Time
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now().UTC() }

// LeaseState values (the machine's persistent states).
type LeaseState string

const (
	StateOffered  LeaseState = "offered"
	StateLeased   LeaseState = "leased"
	StateReleased LeaseState = "released"
	StateExpired  LeaseState = "expired"
)

// RequestKind classifies a REQUEST before storage sees it.
type RequestKind string

const (
	// ReqSelecting: option 54 present (client picked a server).
	ReqSelecting RequestKind = "selecting"
	// ReqInitReboot: option 50 present, no option 54, ciaddr 0.
	ReqInitReboot RequestKind = "init-reboot"
	// ReqRenew: ciaddr set, no option 54 (RENEWING/REBINDING).
	ReqRenew RequestKind = "renew"
)

// Action is the classified result of processing one datagram.
type Action string

const (
	// ActOffer: answer DISCOVER with OFFER (reservation only).
	ActOffer Action = "offer"
	// ActACK: commit/confirm a lease and answer ACK.
	ActACK Action = "ack"
	// ActNAK: answer NAK (client must return to INIT).
	ActNAK Action = "nak"
	// ActDrop: protocol-correct silence; Reason says why.
	ActDrop Action = "drop"
	// ActPoolExhausted: DISCOVER cannot be served right now; no reply.
	ActPoolExhausted Action = "pool_exhausted"
	// ActReleased: RELEASE accepted; no reply per RFC 2131.
	ActReleased Action = "released"
)

// Stable failure categories (appear in Drop/NAK/error Reasons and tests).
const (
	ReasonNoActiveOffer       = "no_active_offer_for_server_id"
	ReasonOfferForOtherIP     = "offer_does_not_match_requested_ip"
	ReasonForeignServerID     = "server_id_is_not_this_server"
	ReasonWrongIPInitReboot   = "init_reboot_requested_ip_not_leased_to_client"
	ReasonUnknownClientReboot = "init_reboot_unknown_client_silent"
	ReasonRenewNoLease        = "renew_without_active_lease_silent"
	ReasonRenewAddrMismatch   = "renew_ciaddr_does_not_match_lease"
	ReasonPoolExhausted       = "pool_exhausted"
	ReasonReleaseNoMatch      = "release_ciaddr_not_leased_to_client"
	ReasonMalformed           = "malformed_message"
	ReasonDuplicate           = "duplicate_transaction_replayed"
)

// Outcome is the full result of one datagram, including the reply
// descriptor (if any) and the exact lease timestamps used.
type Outcome struct {
	Action Action
	// Reply describes the datagram to send; nil for drop/no-reply.
	Reply *ReplySpec
	// State summary (zero unless a lease was touched).
	LeaseIP      netip.Addr
	LeaseState   LeaseState
	LeaseExpires time.Time
	// Duplicate is true when an earlier reply for (identity,xid) was
	// resent verbatim and no state was changed.
	Duplicate bool
	Reason    string
}

// ReplySpec is everything needed to encode a wire reply.
type ReplySpec struct {
	Type         dhcp4.MessageType
	XID          [4]byte
	YIAddr       netip.Addr
	CHAddr       [6]byte
	ClientIDOpt  []byte
	LeaseSeconds uint32
	// OriginalExpires is the lease expiry carried by this reply. For
	// duplicate replies it is the ORIGINAL expiry, proving the replay did
	// not extend the lease.
	OriginalExpires time.Time
}

// DiscoverInput is a parsed DISCOVER.
type DiscoverInput struct {
	XID      [4]byte
	Identity dhcp4.ClientIdentity
	CHAddr   [6]byte
	ClientID []byte
}

// RequestInput is a parsed and classified REQUEST.
type RequestInput struct {
	XID         [4]byte
	Kind        RequestKind
	Identity    dhcp4.ClientIdentity
	CHAddr      [6]byte
	ClientID    []byte
	ServerID    netip.Addr // option 54 (selecting)
	RequestedIP netip.Addr // option 50 (selecting/init-reboot)
	CIAddr      netip.Addr // renew
}

// ReleaseInput is a parsed RELEASE.
type ReleaseInput struct {
	XID      [4]byte
	Identity dhcp4.ClientIdentity
	CHAddr   [6]byte
	ClientID []byte
	CIAddr   netip.Addr
}

// LeaseView is a stored lease row.
type LeaseView struct {
	IP        netip.Addr
	State     LeaseState
	ClientKey string
	ExpiresAt time.Time
	UpdatedAt time.Time
	CreatedAt time.Time
}

// Store is the durable state machine.
type Store struct {
	db     *sql.DB
	pool   *ippool.Pool
	clock  Clock
	server netip.Addr

	leaseTime time.Duration
	offerTTL  time.Duration

	now    func() time.Time
	closeM sync.Once
}

// Options configures a Store.
type Options struct {
	DB        *sql.DB
	Pool      *ippool.Pool
	ServerID  netip.Addr
	LeaseTime time.Duration
	OfferTTL  time.Duration
	Clock     Clock
}

//go:embed schema.sql
var schemaSQL string

// OpenDB opens a SQLite database with the pragmas this package relies on:
// WAL, busy timeout, foreign keys and immediate-transaction locking.
func OpenDB(ctx context.Context, dsn string) (*sql.DB, error) {
	if dsn == "" || dsn == ":memory:" {
		dsn = "file::memory:?cache=shared"
	}
	if !strings.Contains(dsn, "_txlock") {
		sep := "?"
		if strings.Contains(dsn, "?") {
			sep = "&"
		}
		dsn += sep + "_txlock=immediate"
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// A small pool: writers are serialised by IMMEDIATE anyway. Keeping
	// multiple connections lets an expiry sweep run while a transaction
	// is preparing, while busy_timeout serialises the actual writes.
	db.SetMaxOpenConns(4)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	pragmas := []string{
		"busy_timeout=5000",
		"journal_mode=WAL",
		"foreign_keys=ON",
	}
	for _, p := range pragmas {
		if _, err := db.ExecContext(ctx, "PRAGMA "+p); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("pragma %s: %w", p, err)
		}
	}
	return db, nil
}

// New prepares schema and returns a Store.
func New(ctx context.Context, opt Options) (*Store, error) {
	if opt.DB == nil {
		return nil, errors.New("storage: nil DB")
	}
	if opt.Pool == nil {
		return nil, errors.New("storage: nil pool")
	}
	if !opt.ServerID.Is4() {
		return nil, errors.New("storage: server id must be IPv4")
	}
	if opt.LeaseTime <= 0 || opt.OfferTTL <= 0 {
		return nil, errors.New("storage: lease/offer durations must be > 0")
	}
	clk := opt.Clock
	if clk == nil {
		clk = realClock{}
	}
	s := &Store{
		db:        opt.DB,
		pool:      opt.Pool,
		clock:     clk,
		server:    opt.ServerID,
		leaseTime: opt.LeaseTime,
		offerTTL:  opt.OfferTTL,
	}
	s.now = func() time.Time { return s.clock.Now() }
	if _, err := opt.DB.ExecContext(ctx, schemaSQL); err != nil {
		return nil, fmt.Errorf("storage: schema: %w", err)
	}
	if err := s.migrate(ctx); err != nil {
		return nil, err
	}
	if err := s.checkSchema(ctx); err != nil {
		return nil, err
	}
	return s, nil
}

// migrate applies additive, idempotent schema upgrades for databases
// created before the columns existed.
func (s *Store) migrate(ctx context.Context) error {
	additions := []struct {
		table, col, decl string
	}{
		{"replies", "ref_kind", "TEXT NOT NULL DEFAULT ''"},
		{"replies", "ref_id", "INTEGER NOT NULL DEFAULT 0"},
	}
	for _, a := range additions {
		var n int
		if err := s.db.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?`,
			a.table, a.col).Scan(&n); err != nil {
			return fmt.Errorf("storage: migrate check %s.%s: %w", a.table, a.col, err)
		}
		if n == 0 {
			if _, err := s.db.ExecContext(ctx,
				fmt.Sprintf("ALTER TABLE %s ADD COLUMN %s %s", a.table, a.col, a.decl)); err != nil {
				return fmt.Errorf("storage: migrate add %s.%s: %w", a.table, a.col, err)
			}
		}
	}
	return nil
}

func (s *Store) checkSchema(ctx context.Context) error {
	var name string
	err := s.db.QueryRowContext(ctx,
		`SELECT name FROM sqlite_master WHERE type='table' AND name='leases'`).Scan(&name)
	if err != nil {
		return fmt.Errorf("storage: schema check: %w", err)
	}
	return nil
}

// Close releases the database.
func (s *Store) Close() error {
	var err error
	s.closeM.Do(func() { err = s.db.Close() })
	return err
}

// DB exposes the handle for diagnostics/admin use.
func (s *Store) DB() *sql.DB { return s.db }
