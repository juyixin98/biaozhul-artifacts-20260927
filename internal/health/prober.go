// Package health performs optional local liveness probing of next-hops.
//
// Policy (deliberately asymmetric and aligned with the service contract):
//
//   - A member that fails Failures consecutive TCP dials is marked DOWN
//     immediately. The next routing generation excludes it.
//   - Successful probes never mark a member UP. Recovery is an explicit,
//     versioned action (POST /admin/members/{id}/up), so operators control the
//     moment traffic shifts back and the resulting versioned reassignment is
//     recorded. This avoids flapping-induced migration storms.
//
// All probes target loopback/local synthetic listeners in this delivery; no
// real accounts or hosts are involved.
package health

import (
	"context"
	"log/slog"
	"net"
	"sync"
	"time"

	"flowrouter/internal/apperr"
	"flowrouter/internal/router"
)

// Prober dials members on a fixed cadence.
type Prober struct {
	rt       *router.Router
	interval time.Duration
	timeout  time.Duration
	failures int
	log      *slog.Logger

	mu      sync.Mutex
	strikes map[string]int // consecutive failures per member
	stopped chan struct{}
	wg      sync.WaitGroup

	// Dialer is overridable in tests so probing can be exercised without real
	// sockets and forced into precise up/down sequences.
	dialFn func(ctx context.Context, addr string, timeout time.Duration) error
}

// New constructs a Prober.
func New(rt *router.Router, interval, timeout time.Duration, failures int, log *slog.Logger) (*Prober, error) {
	if interval <= 0 || timeout <= 0 || failures < 1 {
		return nil, apperr.Invalid("BAD_PROBE_CONFIG",
			"interval/timeout must be > 0 and failures >= 1")
	}
	if log == nil {
		log = slog.Default()
	}
	p := &Prober{
		rt: rt, interval: interval, timeout: timeout, failures: failures,
		log: log, strikes: map[string]int{}, stopped: make(chan struct{}),
	}
	p.dialFn = p.defaultDial
	return p, nil
}

func (p *Prober) defaultDial(ctx context.Context, addr string, timeout time.Duration) error {
	d := net.Dialer{Timeout: timeout}
	conn, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return err
	}
	return conn.Close()
}

// DialFunc is the overridable dial primitive.
type DialFunc func(ctx context.Context, addr string, timeout time.Duration) error

// SetDialer replaces the dial implementation (used by tests to script
// reachability without real sockets).
func (p *Prober) SetDialer(fn DialFunc) {
	if fn != nil {
		p.dialFn = fn
	}
}

// Start launches the background loop.
func (p *Prober) Start() {
	p.wg.Add(1)
	go p.loop()
}

// Stop signals the loop and waits for it to exit.
func (p *Prober) Stop() {
	close(p.stopped)
	p.wg.Wait()
}

func (p *Prober) loop() {
	defer p.wg.Done()
	t := time.NewTicker(p.interval)
	defer t.Stop()
	p.once() // probe promptly instead of waiting one interval
	for {
		select {
		case <-p.stopped:
			return
		case <-t.C:
			p.once()
		}
	}
}

// once probes every member currently declared in the snapshot. Members with
// no address are skipped. Failure bookkeeping is keyed by member ID and reset
// on a successful probe (but success never brings a down member back up).
// RunOnce performs exactly one probe sweep synchronously. It is used by the
// background loop and is exported so tests (and one-off administrative
// diagnostics) can probe deterministically without waiting for a tick.
func (p *Prober) RunOnce() { p.once() }

func (p *Prober) once() {
	ctx, cancel := context.WithTimeout(context.Background(), p.timeout+100*time.Millisecond)
	defer cancel()
	for _, mi := range p.rt.Current().Members {
		if mi.Address == "" {
			continue
		}
		err := p.dialFn(ctx, mi.Address, p.timeout)
		if err != nil {
			p.registerFailure(mi.ID)
			continue
		}
		p.mu.Lock()
		p.strikes[mi.ID] = 0
		p.mu.Unlock()
	}
}

func (p *Prober) registerFailure(id string) {
	p.mu.Lock()
	p.strikes[id]++
	n := p.strikes[id]
	p.mu.Unlock()
	if n < p.failures {
		return
	}
	// Threshold crossed: transition to DOWN. Retry on CAS conflict with the
	// fresh version, because an admin/config change may have raced us; the
	// observed failure still applies to the current generation.
	for attempt := 0; attempt < 3; attempt++ {
		cur := p.rt.Current()
		if _, _, err := p.rt.SetDown(cur.Version, id, "probe_failed"); err != nil {
			if ae, ok := apperr.As(err); ok && ae.Code == "VERSION" {
				continue
			}
			// UNKNOWN_MEMBER means the member was removed mid-cycle; reset it.
			if ae, ok := apperr.As(err); ok && ae.Code == "UNKNOWN_MEMBER" {
				p.mu.Lock()
				delete(p.strikes, id)
				p.mu.Unlock()
				return
			}
			p.log.Warn("probe mark-down failed", "member", id, "err", err)
			return
		}
		p.log.Warn("member marked down after failed probes", "member", id, "strikes", n)
		return
	}
	p.log.Warn("probe mark-down gave up after version conflicts", "member", id)
}

// Strikes exposes consecutive failure counts (used by diagnostics/tests).
func (p *Prober) Strikes() map[string]int {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[string]int, len(p.strikes))
	for k, v := range p.strikes {
		out[k] = v
	}
	return out
}
