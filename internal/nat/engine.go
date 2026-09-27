// Package nat is the stateful NAPT network model.
//
// It is a pure model: it consumes packet *metadata*, never touches a socket or
// the host network, and emits translated 5-tuples plus classified decisions.
// Time is supplied by the replayed trace and kept behind a monotonic
// watermark, so a packet stamped in the past cannot move time backwards or
// revive an expired mapping.
package nat

import (
	"context"
	"fmt"
	"net"
	"sync"
	"time"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/portalloc"
	"natlab/internal/storage"
)

// tombstoneRetain is how long a closed mapping's endpoint is remembered so a
// late return is reported as mapping_expired instead of no_matching_mapping.
const tombstoneRetain = 5 * time.Minute

// Engine evaluates packets for one replay run. It is safe for concurrent use:
// Process serializes internally, so concurrent first packets from the same
// flow produce exactly one mapping while distinct flows get distinct ports.
type Engine struct {
	cfg   config.Config
	runID string
	store storage.Store

	mu sync.Mutex

	tcpPool *portalloc.Pool
	udpPool *portalloc.Pool

	flows map[flowKey]*mapping
	ports map[portKey]*mapping

	watermark time.Time
	stats     model.Stats
}

// New builds an engine with an empty connection table and registers the run
// header in the store.
func New(runID string, cfg config.Config, st storage.Store) (*Engine, error) {
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("nat: %w", err)
	}
	e := &Engine{
		cfg: cfg, runID: runID, store: st,
		tcpPool: portalloc.NewPool(model.TCP, cfg.TCPPortMin, cfg.TCPPortMax,
			cfg.ReuseCooldown.Duration),
		udpPool: portalloc.NewPool(model.UDP, cfg.UDPPortMin, cfg.UDPPortMax,
			cfg.ReuseCooldown.Duration),
		flows: map[flowKey]*mapping{},
		ports: map[portKey]*mapping{},
	}
	ctx := context.Background()
	if err := st.UpsertRun(ctx, storage.RunInfo{ID: runID, CreatedAt: time.Now()}); err != nil {
		return nil, fmt.Errorf("nat: register run: %w", err)
	}
	return e, nil
}

// Stats returns a point-in-time copy of the counters.
func (e *Engine) Stats() model.Stats {
	e.mu.Lock()
	defer e.mu.Unlock()
	s := e.stats
	s.ActiveMappings = len(e.flows)
	return s
}

// Watermark returns the current monotonic model clock.
func (e *Engine) Watermark() time.Time {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.watermark
}

// Snapshots returns the active mappings ordered by external port.
func (e *Engine) Snapshots() []model.MappingView {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]model.MappingView, 0, len(e.flows))
	for _, m := range e.flows {
		out = append(out, m.snapshot())
	}
	return out
}

// Result is everything one Process call produces. Expired lists mappings the
// advancing clock lazily reaped while evaluating this packet (normally 0 or 1).
type Result struct {
	Decision model.Decision
	Expired  []model.MappingView
}

// Process evaluates one packet. Policy rejections are returned in Result with
// a nil error; a non-nil error is reserved for compute/persistence failures
// (and a compute_failure decision is still logged when possible).
func (e *Engine) Process(ctx context.Context, p model.Packet) (Result, error) {
	e.mu.Lock()
	defer e.mu.Unlock()

	now := p.ObservedAt
	rollback := !now.After(e.watermark) && !now.Equal(e.watermark) && !e.watermark.IsZero()
	effNow := now
	if e.watermark.After(now) {
		effNow = e.watermark // past timestamp: evaluate at the monotonic clock
	}

	// Advance the clock (only forward) before the lazy sweep so lifecycle
	// events logged during expiry carry the watermark at which they expired.
	if effNow.After(e.watermark) {
		e.watermark = effNow
		_ = e.store.SetWatermark(ctx, e.runID, effNow)
	}
	expired := e.sweepLocked(effNow)
	if rollback {
		e.stats.ClockRollbacks++
	}

	d := model.Decision{
		RunID: e.runID, Seq: p.Seq, Label: p.Label, At: now,
		Pre: p.Tuple, Watermark: e.watermark,
	}

	// ---- input validation (does not depend on state) ----
	if reason, why := validate(p); reason != "" {
		d.Verdict, d.Reason, d.Class, d.Rationale =
			model.Reject, reason, model.ClassOf(reason), why
		e.stats.RejectedInput++
		return e.finish(ctx, Result{Decision: d, Expired: expired})
	}

	var (
		res Result
		err error
	)
	if p.Direction == model.Outbound {
		res, err = e.processOutboundLocked(ctx, p, effNow, d)
	} else {
		res, err = e.processInboundLocked(ctx, p, effNow, d)
	}
	res.Expired = expired
	if err != nil {
		// Compute failure: record what we can and surface the distinct error.
		d.Verdict, d.Reason, d.Class = model.ComputeFailure, model.ReasonStorageFailure, model.ClassCompute
		d.Rationale = "persistence failure while applying decision: " + err.Error()
		e.stats.ComputeFailures++
		_ = e.store.AppendDecision(ctx, d)
		return Result{Decision: d, Expired: expired}, model.ErrComputeFailure
	}
	return e.finish(ctx, res)
}

func (e *Engine) finish(ctx context.Context, r Result) (Result, error) {
	if perr := e.store.AppendDecision(ctx, r.Decision); perr != nil {
		// Logging failure is itself a compute failure distinct from policy.
		r.Decision.Verdict = model.ComputeFailure
		r.Decision.Reason = model.ReasonStorageFailure
		r.Decision.Class = model.ClassCompute
		e.stats.ComputeFailures++
		return r, model.ErrComputeFailure
	}
	return r, nil
}

// ---------------- validation ----------------

func validate(p model.Packet) (model.RejectReason, string) {
	if p.ObservedAt.IsZero() {
		return model.ReasonInvalidInput, "observed_at is required (zero timestamp)"
	}
	if p.Direction != model.Outbound && p.Direction != model.Inbound {
		return model.ReasonInvalidInput,
			fmt.Sprintf("direction must be outbound|inbound, got %q", p.Direction)
	}
	if p.Fragmented {
		return model.ReasonFragmentDropped,
			"IP fragment presented: the model accepts reassembled input only"
	}
	t := p.Tuple
	if t.Proto != model.TCP && t.Proto != model.UDP {
		return model.ReasonProtocolUnsupport,
			fmt.Sprintf("protocol %q not supported (TCP/UDP only)", t.Proto)
	}
	if bad := checkEndpoint(t.SrcIP, t.SrcPort, "src"); bad != "" {
		return model.ReasonInvalidInput, bad
	}
	if bad := checkEndpoint(t.DstIP, t.DstPort, "dst"); bad != "" {
		return model.ReasonInvalidInput, bad
	}
	if t.SrcIP == t.DstIP && t.SrcPort == t.DstPort {
		return model.ReasonInvalidInput, "src and dst endpoint are identical"
	}
	if t.Proto == model.TCP {
		if !p.TCP.Any() {
			return model.ReasonInvalidInput, "TCP packet must set at least one of SYN/ACK/FIN/RST"
		}
		if p.TCP.SYN && p.TCP.FIN {
			return model.ReasonInvalidInput, "illegal TCP flag combination SYN+FIN"
		}
		if p.TCP.SYN && p.TCP.RST {
			return model.ReasonInvalidInput, "illegal TCP flag combination SYN+RST"
		}
		if p.TCP.FIN && p.TCP.RST {
			return model.ReasonInvalidInput, "illegal TCP flag combination FIN+RST"
		}
	}
	return "", ""
}

func checkEndpoint(ip string, port uint16, side string) string {
	if net.ParseIP(ip) == nil {
		return fmt.Sprintf("%s_ip %q is not a valid IP", side, ip)
	}
	if port == 0 {
		return fmt.Sprintf("%s_port must be non-zero", side)
	}
	return ""
}

// ---------------- expiry ----------------

func (e *Engine) sweepLocked(now time.Time) []model.MappingView {
	var out []model.MappingView
	for k, m := range e.flows {
		if now.Before(m.expiresAt) {
			continue
		}
		out = append(out, m.snapshot())
		e.removeLocked(m, now, "idle timeout at "+now.UTC().Format(time.RFC3339))
		delete(e.flows, k)
		delete(e.ports, portKey{proto: m.proto, port: m.extPort})
		e.stats.MappingsExpired++
	}
	return out
}

// removeLocked releases the port and writes a tombstone. The caller deleted
// (or will delete) the maps.
func (e *Engine) removeLocked(m *mapping, now time.Time, why string) {
	pool := e.tcpPool
	if m.proto == model.UDP {
		pool = e.udpPool
	}
	pool.Release(m.extPort, now)
	_ = e.store.DeleteMapping(context.Background(), e.runID, m.id, now)
	_ = e.store.AddTombstone(context.Background(), storage.StoredTombstone{
		RunID: e.runID, Proto: m.proto, ExternalPort: m.extPort,
		RemoteIP: m.remIP, RemotePort: m.remPort,
		ClosedAt: now, RetainUntil: now.Add(tombstoneRetain),
	})
	_ = e.store.AppendDecision(context.Background(), model.Decision{
		RunID: e.runID, At: now, Watermark: e.watermark,
		Verdict: "lifecycle_expired", MappingID: m.id,
		StateBefore: m.state, StateAfter: StateClosed,
		Rationale: "mapping " + m.id + " removed: " + why,
	})
	_ = e.store.PruneTombstones(context.Background(), e.runID, now)
}

// ---------------- outbound ----------------

func (e *Engine) processOutboundLocked(ctx context.Context, p model.Packet,
	now time.Time, d model.Decision) (Result, error) {
	k := keyOf(p)

	// Unallocated destination of an outbound packet aimed at the NAT's own
	// external address is meaningless for an internal sender; not a separate
	// class here (dst is the remote peer by definition).
	m, ok := e.flows[k]
	if !ok {
		// Only TCP SYN or UDP may create a mapping.
		if p.Tuple.Proto == model.TCP && !p.TCP.SYN {
			return reject(d, model.ReasonNoMapping,
				"outbound TCP segment with no SYN and no active flow: nothing to translate"), nil
		}
		pool := e.tcpPool
		if p.Tuple.Proto == model.UDP {
			pool = e.udpPool
		}
		port, got := pool.Alloc(now)
		if !got {
			e.stats.RejectedExhaustion++
			d.Verdict = model.Reject
			d.Reason = model.ReasonPortExhausted
			d.Class = model.ClassExhaustion
			active, cooling := pool.Stats(now)
			d.Rationale = fmt.Sprintf(
				"port pool exhausted: %d/%d ports active, %d in cooldown; cannot open new %s mapping",
				active, pool.Size(), cooling, p.Tuple.Proto)
			return Result{Decision: d}, nil
		}
		m = &mapping{
			id: mappingID(p.Tuple.Proto, port), proto: p.Tuple.Proto,
			intSrcIP: p.Tuple.SrcIP, intSrcPort: p.Tuple.SrcPort,
			remIP: p.Tuple.DstIP, remPort: p.Tuple.DstPort,
			extPort:   port,
			state:     StateUDPOpen,
			createdAt: now, lastSeen: now,
		}
		if p.Tuple.Proto == model.TCP {
			m.state = StateSynSent
		}
		m.expiresAt = e.deadlineFor(m.state, now)
		e.flows[k] = m
		e.ports[portKey{proto: m.proto, port: port}] = m
		e.stats.MappingsCreated++
		e.stats.OutboundFirst++

		if err := e.persistMapping(ctx, m); err != nil {
			// Roll back in-memory allocation on a write failure so the model
			// state and store stay consistent.
			delete(e.flows, k)
			delete(e.ports, portKey{proto: m.proto, port: port})
			pool.Release(port, now)
			return Result{}, err
		}

		post := p.Tuple
		post.SrcIP = e.cfg.ExternalIP
		post.SrcPort = port
		d.Verdict = model.AcceptTranslate
		d.Post = &post
		d.MappingID = m.id
		d.AllocatedPort = port
		d.StateAfter = m.state
		d.Rationale = fmt.Sprintf(
			"first outbound %s packet: created mapping %s (%s:%d -> %s:%d), external port %d",
			p.Tuple.Proto, m.id, p.Tuple.SrcIP, p.Tuple.SrcPort,
			p.Tuple.DstIP, p.Tuple.DstPort, port)
		return Result{Decision: d}, nil
	}

	// Existing flow.
	d.MappingID = m.id
	d.StateBefore = m.state

	if p.Tuple.Proto == model.TCP {
		closed, why, cerr := e.applyTCPStep(ctx, m, p, now)
		if cerr != nil {
			return Result{}, cerr
		}
		if why == "" {
			e.stats.RejectedState++
			d.Verdict, d.Reason, d.Class = model.Reject, model.ReasonStateConflict, model.ClassState
			d.StateAfter = m.state
			d.Rationale = "outbound: " + stepTCPRationale(m.state, p)
			return Result{Decision: d}, nil
		}
		post := p.Tuple
		post.SrcIP = e.cfg.ExternalIP
		post.SrcPort = m.extPort
		d.Post = &post
		if closed {
			d.StateAfter = StateClosed
			d.Rationale = "outbound: " + why + "; packet delivered, mapping torn down"
		} else {
			d.StateAfter = m.state
			d.Rationale = "outbound: " + why
		}
		d.Verdict = model.AcceptForward
		e.stats.OutboundForward++
		return Result{Decision: d}, nil
	}

	// UDP existing flow: forward + refresh.
	m.lastSeen = now
	m.expiresAt = e.deadlineFor(StateUDPOpen, now)
	if err := e.persistMapping(ctx, m); err != nil {
		return Result{}, err
	}
	post := p.Tuple
	post.SrcIP = e.cfg.ExternalIP
	post.SrcPort = m.extPort
	d.Verdict = model.AcceptForward
	d.Post = &post
	d.StateAfter = StateUDPOpen
	d.Rationale = "outbound UDP matches active mapping: refreshing idle timer and translating"
	e.stats.OutboundForward++
	return Result{Decision: d}, nil
}

// ---------------- inbound ----------------

func (e *Engine) processInboundLocked(ctx context.Context, p model.Packet,
	now time.Time, d model.Decision) (Result, error) {
	if p.Tuple.DstIP != e.cfg.ExternalIP {
		e.stats.RejectedInput++
		return reject(d, model.ReasonExternalMismatch, fmt.Sprintf(
			"inbound packet dst %s is not the NAT external address %s",
			p.Tuple.DstIP, e.cfg.ExternalIP)), nil
	}

	pk := inboundPortKey(p)
	m, held := e.ports[pk]
	if !held {
		// Distinguish a just-dead mapping from one that never existed.
		had, err := e.store.FindTombstone(ctx, e.runID, p.Tuple.Proto, p.Tuple.DstPort,
			p.Tuple.SrcIP, p.Tuple.SrcPort, now)
		if err != nil {
			return Result{}, err
		}
		e.stats.RejectedState++
		if had {
			d.Verdict, d.Reason, d.Class = model.Reject, model.ReasonMappingExpired, model.ClassState
			d.Rationale = fmt.Sprintf(
				"late return on port %d from %s:%d: mapping already expired/closed",
				p.Tuple.DstPort, p.Tuple.SrcIP, p.Tuple.SrcPort)
		} else {
			d.Verdict, d.Reason, d.Class = model.Reject, model.ReasonNoMapping, model.ClassState
			d.Rationale = fmt.Sprintf(
				"unsolicited inbound %s to external port %d: no active mapping",
				p.Tuple.Proto, p.Tuple.DstPort)
		}
		return Result{Decision: d}, nil
	}

	// Port is held: enforce the exact remote peer (symmetric NAPT).
	if p.Tuple.SrcIP != m.remIP || p.Tuple.SrcPort != m.remPort {
		e.stats.RejectedState++
		d.Verdict, d.Reason, d.Class = model.Reject, model.ReasonRemoteMismatch, model.ClassState
		d.MappingID = m.id
		d.StateBefore = m.state
		d.StateAfter = m.state
		d.Rationale = fmt.Sprintf(
			"return peer %s:%d does not match mapping %s remote %s:%d",
			p.Tuple.SrcIP, p.Tuple.SrcPort, m.id, m.remIP, m.remPort)
		return Result{Decision: d}, nil
	}

	d.MappingID = m.id
	d.StateBefore = m.state

	if p.Tuple.Proto == model.TCP {
		closed, why, cerr := e.applyTCPStep(ctx, m, p, now)
		if cerr != nil {
			return Result{}, cerr
		}
		if why == "" {
			e.stats.RejectedState++
			d.Verdict, d.Reason, d.Class = model.Reject, model.ReasonStateConflict, model.ClassState
			d.StateAfter = m.state
			d.Rationale = "inbound: " + stepTCPRationale(m.state, p)
			return Result{Decision: d}, nil
		}
		post := p.Tuple
		post.DstIP = m.intSrcIP
		post.DstPort = m.intSrcPort
		d.Post = &post
		if closed {
			d.StateAfter = StateClosed
			d.Rationale = "inbound: " + why + "; packet delivered, mapping torn down"
		} else {
			d.StateAfter = m.state
			d.Rationale = "inbound: " + why
		}
		d.Verdict = model.AcceptForward
		e.stats.InboundForward++
		return Result{Decision: d}, nil
	}

	// UDP return: deliver and refresh.
	m.lastSeen = now
	m.expiresAt = e.deadlineFor(StateUDPOpen, now)
	if err := e.persistMapping(ctx, m); err != nil {
		return Result{}, err
	}
	post := p.Tuple
	post.DstIP = m.intSrcIP
	post.DstPort = m.intSrcPort
	d.Verdict = model.AcceptForward
	d.Post = &post
	d.StateAfter = StateUDPOpen
	d.Rationale = "inbound UDP matches mapping remote endpoint: delivering and refreshing idle timer"
	e.stats.InboundForward++
	return Result{Decision: d}, nil
}

// ---------------- helpers ----------------

func (e *Engine) closeMapping(ctx context.Context, m *mapping, now time.Time, why string) {
	delete(e.flows, flowKey{proto: m.proto, intIP: m.intSrcIP, intPort: m.intSrcPort,
		remIP: m.remIP, remPort: m.remPort})
	delete(e.ports, portKey{proto: m.proto, port: m.extPort})
	e.removeLocked(m, now, why)
}

func (e *Engine) persistMapping(ctx context.Context, m *mapping) error {
	return e.store.PutMapping(ctx, storage.StoredMapping{
		RunID: e.runID, ID: m.id, Proto: m.proto,
		IntSrcIP: m.intSrcIP, IntSrcPort: m.intSrcPort,
		ExtDstIP: m.remIP, ExtDstPort: m.remPort,
		ExternalIP: e.cfg.ExternalIP, ExternalPort: m.extPort,
		State: m.state, CreatedAt: m.createdAt, LastSeen: m.lastSeen,
		ExpiresAt: m.expiresAt,
	})
}

func (e *Engine) deadlineFor(state string, from time.Time) time.Time {
	switch state {
	case StateSynSent:
		return from.Add(e.cfg.TCPSynTimeout.Duration)
	case StateEstablished:
		return from.Add(e.cfg.TCPEstabTimeout.Duration)
	case StateFinWait1, StateFinWait2:
		return from.Add(e.cfg.TCPFinTimeout.Duration)
	case StateTimeWait:
		return from.Add(e.cfg.TCPTimeWait.Duration)
	default: // UDP_OPEN
		return from.Add(e.cfg.UDPTimeout.Duration)
	}
}

// applyTCPStep runs the state machine against an existing mapping and applies
// the side effects (timer slide, state transition, persistence or teardown).
// It returns:
//
//	closed: the mapping was torn down by this packet;
//	why:    rationale of the accepted transition ("" when the segment is
//	        illegal and the packet must be rejected);
//	err:    persistence/compute failure only.
func (e *Engine) applyTCPStep(ctx context.Context, m *mapping, p model.Packet,
	now time.Time) (closed bool, why string, err error) {
	prev := m.state
	step := stepTCP(prev, p.Direction, p.TCP)
	if step.reject {
		return false, "", nil
	}

	entered := step.stateAfter
	if step.finInternalSet {
		m.finInternal = true
	}
	if step.finExternalSet {
		m.finExternal = true
	}

	switch {
	case step.close:
		m.state = StateClosed
		e.closeMapping(ctx, m, now, step.rationale)
		return true, step.rationale, nil
	case entered != prev:
		// A handshake completion or FIN boundary restarts the clock for the
		// new state from this instant.
		m.state = entered
		m.lastSeen = now
	default:
		// Same-state dataplane packet: refresh per state and direction.
		m.state = entered
		if refreshesSameState(prev, p.Direction) {
			m.lastSeen = now
		}
	}
	m.expiresAt = e.deadlineFor(m.state, m.lastSeen)
	if err := e.persistMapping(ctx, m); err != nil {
		return false, "", err
	}
	return false, step.rationale, nil
}

// refreshesSameState decides whether an accepted dataplane packet that does
// NOT change the state slides its idle timer.
func refreshesSameState(state string, dir model.Direction) bool {
	switch state {
	case StateSynSent, StateEstablished:
		return true
	case StateFinWait1:
		return true // waiting peer FIN; ACK/data prove the path is alive
	case StateFinWait2:
		return dir == model.Inbound // internal side already half-closed
	case StateTimeWait:
		return false // fixed quiet window, deliberately not slid
	}
	return true
}

// stepTCPRationale re-evaluates the rejected segment to produce a stable
// rationale for the decision log. The mapping is not mutated.
func stepTCPRationale(state string, p model.Packet) string {
	return stepTCP(state, p.Direction, p.TCP).rationale
}

func reject(d model.Decision, r model.RejectReason, why string) Result {
	d.Verdict, d.Reason, d.Class = model.Reject, r, model.ClassOf(r)
	d.Rationale = why
	return Result{Decision: d}
}
