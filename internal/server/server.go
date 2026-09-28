// Package server implements the local DHCPv4 state-machine subset:
//
//	DISCOVER -> OFFER (reservation, not a lease)
//	REQUEST  -> ACK (selecting / renew-rebind / init-reboot) or NAK
//	RELEASE  -> silent state transition (no reply per RFC 2131)
//
// The core is transport-agnostic: Handle consumes a decoded packet plus a
// source descriptor and returns a wire reply or an explicit no-reply. The UDP
// and HTTP transports are thin adapters over it, so the same state machine is
// driven by raw-loopback fixtures and by the replay API.
package server

import (
	"crypto/sha256"
	"encoding/hex"
	"net/netip"
	"sync/atomic"
	"time"

	"dhcpv4lab/internal/config"
	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

// Clock abstracts time so tests can advance a virtual clock without sleeping.
type Clock interface {
	Now() time.Time
}

type wallClock struct{}

func (wallClock) Now() time.Time { return time.Now() }

// FakeClockSeed is the fixed virtual-clock origin used by test fixtures.
const FakeClockSeedUnix int64 = 1_700_000_000

// FakeClock is a manually advanced clock used by the HTTP test endpoints and
// deterministic tests. It is safe for concurrent use.
type FakeClock struct{ v atomic.Int64 }

// NewFakeClock seeds a fake clock at t.
func NewFakeClock(t time.Time) *FakeClock {
	f := &FakeClock{}
	f.v.Store(t.UnixNano())
	return f
}

// Now returns the virtual instant.
func (f *FakeClock) Now() time.Time { return time.Unix(0, f.v.Load()) }

// Advance moves the clock and returns the new instant.
func (f *FakeClock) Advance(d time.Duration) time.Time {
	return time.Unix(0, f.v.Add(int64(d)))
}

// Set jumps the clock to an absolute instant.
func (f *FakeClock) Set(t time.Time) time.Time {
	f.v.Store(t.UnixNano())
	return t
}

// Source describes where a datagram came from, for diagnostics.
type Source struct {
	RemoteAddr string
	RunID      string
}

// Outcome is the state machine result for one input.
type Outcome struct {
	Category   storage.FailureCategory
	Action     string // short verb: discover_offer, request_ack, request_nak, release_ok, ...
	Result     string // ok | duplicate_replay | rejected | ...
	Reason     string // stable machine-readable reason code
	Detail     string // human-readable elaboration
	Reply      []byte // wire bytes; nil when no reply is permitted
	AssignedIP netip.Addr
	OutType    byte
	Duplicate  bool
}

// Server bundles configuration, persistence, clock and counters.
type Server struct {
	cfg   config.Config
	store *storage.Store
	clock Clock
	pool  storage.AddrRange

	serverID netip.Addr
	netmask  netip.Addr
	router   netip.Addr
	dns      []netip.Addr

	accepted atomic.Uint64
	rejected atomic.Uint64
	replayed atomic.Uint64
}

// New constructs a state machine over an opened store.
func New(cfg config.Config, st *storage.Store, clock Clock) (*Server, error) {
	if clock == nil {
		clock = wallClock{}
	}
	start, err := parseAddr(cfg.Pool.RangeStart)
	if err != nil {
		return nil, err
	}
	end, err := parseAddr(cfg.Pool.RangeEnd)
	if err != nil {
		return nil, err
	}
	rng, err := storage.NewAddrRange(start, end)
	if err != nil {
		return nil, err
	}
	sid, err := parseAddr(cfg.Pool.ServerID)
	if err != nil {
		return nil, err
	}
	mask, err := parseAddr(cfg.Pool.Netmask)
	if err != nil {
		return nil, err
	}
	s := &Server{cfg: cfg, store: st, clock: clock, pool: rng, serverID: sid, netmask: mask}
	if cfg.Pool.Router != "" {
		if s.router, err = parseAddr(cfg.Pool.Router); err != nil {
			return nil, err
		}
	}
	for _, d := range cfg.Pool.DNS {
		a, err := parseAddr(d)
		if err != nil {
			return nil, err
		}
		s.dns = append(s.dns, a)
	}
	return s, nil
}

// Store exposes persistence for transports (sweeper, diagnostics).
func (s *Server) Store() *storage.Store { return s.store }

// Clock exposes the active clock (FakeClock when testMode configured).
func (s *Server) Clock() Clock { return s.clock }

// SetClock replaces the clock (test setup only, before serving traffic).
func (s *Server) SetClock(c Clock) { s.clock = c }

func parseAddr(v string) (netip.Addr, error) {
	a, err := netip.ParseAddr(v)
	if err != nil {
		return netip.Addr{}, err
	}
	return a.Unmap(), nil
}

// fingerprint hashes the semantic request fields that distinguish one
// operation from another for the SAME (identity, xid). The dedup key is
// (identity_id, xid, fingerprint), so chaddr/client-id are covered by the
// identity column and deliberately excluded here.
//
// Included: message type, ciaddr, option-50 requested-ip, option-54
// server-id. Excluded: secs (clients legitimately increment it on
// retransmission), broadcast flag (loopback delivery is always source-unicast),
// option-55 parameter list (a cosmetic retransmit difference must not bypass
// idempotency). Consequence: a same-xid retransmit with a bumped secs still
// collides and is answered from storage WITHOUT touching lease timings.
func fingerprint(p *dhcppacket.Packet, msgType byte) string {
	h := sha256.New()
	h.Write([]byte{msgType})
	write4 := func(a netip.Addr) {
		if a.IsValid() {
			b := a.As4()
			h.Write(b[:])
		} else {
			h.Write([]byte{0, 0, 0, 0})
		}
	}
	write4(p.CIAddr)
	if rip, ok := p.RequestedIP(); ok {
		write4(rip)
	} else {
		write4(netip.Addr{})
	}
	if sid, ok := p.ServerID(); ok {
		write4(sid)
	} else {
		write4(netip.Addr{})
	}
	sum := h.Sum(nil)
	return hex.EncodeToString(sum[:16])
}
