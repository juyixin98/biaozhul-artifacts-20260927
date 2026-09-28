// Package testhelp contains shared helpers for the lab's tests: a
// controllable clock, isolated in-memory SQLite stores and loopback UDP
// wiring. It is test-only code.
package testhelp

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net"
	"net/netip"
	"sync"
	"testing"
	"time"

	"dhcp4lab/internal/config"
	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/ippool"
	"dhcp4lab/internal/server"
	"dhcp4lab/internal/storage"
	"dhcp4lab/internal/udpserver"
	"dhcp4lab/internal/version"

	"dhcp4lab/testfixture/testlog"
)

// FakeClock is a controllable Clock.
type FakeClock struct {
	mu sync.Mutex
	t  time.Time
}

// NewFakeClock starts at a fixed instant.
func NewFakeClock() *FakeClock {
	t, _ := time.Parse(time.RFC3339, "2026-09-28T00:00:00Z")
	return &FakeClock{t: t}
}

// Now returns the controlled time.
func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

// Advance moves the clock and returns the new time.
func (c *FakeClock) Advance(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
	return c.t
}

// Setup configures an isolated in-memory store + adapter.
type Setup struct {
	Cfg     config.Config
	Store   *storage.Store
	Server  *server.Server
	Clock   *FakeClock
	Pool    *ippool.Pool
	Cleanup func()
}

// Options tweaks a setup.
type Options struct {
	PoolSize  int
	LeaseTime time.Duration
	OfferTTL  time.Duration
	Clock     storage.Clock
	ServerID  string
}

// NewStore builds an isolated in-memory store+server for a test. Each
// setup gets its own SQLite in-memory namespace so parallel tests never
// share rows.
func NewStore(t *testing.T, opt Options) *Setup {
	t.Helper()
	cfg := config.Default()
	if opt.ServerID == "" {
		opt.ServerID = cfg.ServerID
	}
	if opt.PoolSize == 0 {
		opt.PoolSize = 11
	}
	if opt.LeaseTime == 0 {
		opt.LeaseTime = 120 * time.Second
	}
	if opt.OfferTTL == 0 {
		opt.OfferTTL = 30 * time.Second
	}
	start := netip.MustParseAddr("192.0.2.10")
	end := netip.AddrFrom4([4]byte{192, 0, 2, byte(10 + opt.PoolSize - 1)})
	pool, err := ippool.New(start, end)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	name := "mem" + randName(12)
	db, err := storage.OpenDB(context.Background(),
		"file:"+name+"?mode=memory&cache=shared")
	if err != nil {
		t.Fatalf("opendb: %v", err)
	}
	sid := netip.MustParseAddr(opt.ServerID)
	var fc *FakeClock
	clk := opt.Clock
	if clk == nil {
		fc = NewFakeClock()
		clk = fc
	} else if asFc, ok := clk.(*FakeClock); ok {
		fc = asFc
	}
	st, err := storage.New(context.Background(), storage.Options{
		DB: db, Pool: pool, ServerID: sid,
		LeaseTime: opt.LeaseTime, OfferTTL: opt.OfferTTL, Clock: clk,
	})
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	params, err := server.ParamsFromConfig(cfg)
	if err != nil {
		t.Fatalf("params: %v", err)
	}
	srv, err := server.New(st, params, nil)
	if err != nil {
		t.Fatalf("server: %v", err)
	}
	s := &Setup{Cfg: cfg, Store: st, Server: srv, Clock: fc, Pool: pool}
	s.Cleanup = func() {
		_ = st.Close()
		_ = db.Close()
	}
	t.Cleanup(s.Cleanup)
	return s
}

func randName(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// RunID returns a unique run correlation id for a test.
func RunID(t *testing.T) string {
	return "test-" + t.Name() + "-" + randName(4)
}

// Log builds a structured test logger.
func Log(t *testing.T) *testlog.Logger {
	return testlog.New(t, RunID(t))
}

// StartUDP binds the adapter to a free loopback UDP port and returns its
// address. Malformed datagrams are dropped at the boundary, exactly as in
// the production main wiring.
func (s *Setup) StartUDP(t *testing.T) string {
	t.Helper()
	addr := freeLoopbackUDP(t)
	ln := udpserver.New(addr, func(ctx context.Context, dg []byte) []byte {
		pkt, perr := dhcp4.Unmarshal(dg)
		if perr != nil {
			return nil
		}
		dec, err := s.Server.Handle(ctx, pkt)
		if err != nil {
			t.Errorf("adapter: %v", err)
			return nil
		}
		return dec.ReplyBytes
	}, nil)
	if err := ln.Listen(); err != nil {
		t.Fatalf("listen: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = ln.Serve(ctx) }()
	t.Cleanup(func() {
		cancel()
		_ = ln.Close()
	})
	return ln.LocalAddr()
}

func freeLoopbackUDP(t *testing.T) string {
	t.Helper()
	ln, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	addr := ln.LocalAddr().String()
	_ = ln.Close()
	return addr
}

// FreeLoopbackTCP returns a free loopback TCP port address.
func FreeLoopbackTCP(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("free tcp port: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

// Version returns the implementation version (for log correlation).
func Version() string { return version.Server }
