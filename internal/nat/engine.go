// Package nat implements the stateful, locally replayable NAT model.
//
// The engine is endpoint-dependent (symmetric): a mapping is keyed by the full
// private 5-tuple, and inbound packets are accepted only from the exact remote
// endpoint the flow was opened to. All time comes from packet timestamps,
// funnelled through a per-run monotonic high-water clock, so clock rewinds
// never extend a mapping's life.
package nat

import (
	"context"
	"encoding/json"
	"net/netip"
	"sync"
	"time"

	"natlab/internal/config"
	"natlab/internal/model"
)

// Engine evaluates packets against per-run NAT state.
type Engine struct {
	cfg   *config.Config
	store StateStore

	mu   sync.Mutex
	runs map[string]*runState
}

type runState struct {
	clock *MonotonicClock
	pool  *PortPool
}

// NewEngine builds an engine over the given store.
func NewEngine(cfg *config.Config, store StateStore) *Engine {
	return &Engine{cfg: cfg, store: store, runs: make(map[string]*runState)}
}

// RunResult is the typed outcome of one packet plus the event row to persist.
type RunResult struct {
	Decision  model.Decision
	Event     *model.Event
	Persisted *model.Mapping
}

// Evaluate validates one packet and applies it to runID's state. Input errors
// and state conflicts come back as RunResults with Accepted=false (model
// verdicts, logged per packet); compute failures come back as a non-nil error
// so the HTTP layer can return 500.
func (e *Engine) Evaluate(ctx context.Context, runID string, pkt model.Packet) (*RunResult, error) {
	// Phase 1: structural validation (no state, no clock needed).
	if code, reason := validatePacket(pkt); code != "" {
		d := reject(model.CatInvalidInput, code, reason, pkt.ObservedAt, time.Time{})
		return e.record(ctx, runID, pkt, d)
	}
	srcAddr, err := netip.ParseAddr(pkt.FiveTuple.SrcIP)
	if err != nil {
		d := reject(model.CatInvalidInput, model.CodeBadSrcIP, "src_ip not parseable: "+pkt.FiveTuple.SrcIP, pkt.ObservedAt, time.Time{})
		return e.record(ctx, runID, pkt, d)
	}
	dstAddr, err := netip.ParseAddr(pkt.FiveTuple.DstIP)
	if err != nil {
		d := reject(model.CatInvalidInput, model.CodeBadDstIP, "dst_ip not parseable: "+pkt.FiveTuple.DstIP, pkt.ObservedAt, time.Time{})
		return e.record(ctx, runID, pkt, d)
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	rs, err := e.ensureRun(ctx, runID)
	if err != nil {
		return nil, err
	}
	now, rewind := rs.clock.Advance(pkt.ObservedAt)
	if err := e.store.SetClock(ctx, runID, now); err != nil {
		return nil, computeErr(err)
	}

	// Phase 2: expire due mappings before any lookup (expires_at <= now).
	closed, err := e.store.SweepExpired(ctx, runID, now)
	if err != nil {
		return nil, computeErr(err)
	}
	for _, m := range closed {
		rs.pool.Release(m.MappedPort)
	}

	var d model.Decision
	switch pkt.Direction {
	case model.Outbound:
		if !e.cfg.IsPrivate(srcAddr) {
			d = reject(model.CatInvalidInput, model.CodeSrcNotPrivate,
				"outbound source "+pkt.FiveTuple.SrcIP+" is not inside private_cidrs", pkt.ObservedAt, now)
			break
		}
		d, err = e.evaluateOutbound(ctx, rs, runID, pkt, now)
	case model.Inbound:
		if dstAddr != e.cfg.PublicAddr() {
			d = reject(model.CatInvalidInput, model.CodeInboundDstMismatch,
				"inbound destination "+pkt.FiveTuple.DstIP+" is not the NAT public address "+e.cfg.PublicIPString(), pkt.ObservedAt, now)
			break
		}
		d, err = e.evaluateInbound(ctx, rs, runID, pkt, now)
	default:
		d = reject(model.CatInvalidInput, model.CodeBadDirection, "direction must be outbound or inbound", pkt.ObservedAt, now)
	}
	if err != nil {
		return nil, err
	}

	d.ObservedAt = pkt.ObservedAt
	d.EffectiveAt = now
	d.ClockRewind = rewind
	d.Swept = int64(len(closed))
	active, cerr := e.store.CountActive(ctx, runID, now)
	if cerr != nil {
		return nil, computeErr(cerr)
	}
	d.ActiveCount = active
	return e.record(ctx, runID, pkt, d)
}

func (e *Engine) ensureRun(ctx context.Context, runID string) (*runState, error) {
	if rs, ok := e.runs[runID]; ok {
		return rs, nil
	}
	hw, err := e.store.EnsureRun(ctx, runID)
	if err != nil {
		return nil, computeErr(err)
	}
	rs := &runState{
		clock: NewClock(hw),
		pool:  NewPortPool(e.cfg.PortLow, e.cfg.PortHigh),
	}
	// Reconstruct the reserved port set from persisted active mappings so a
	// restarted engine never hands out a port already owned.
	active, err := e.store.ListMappings(ctx, runID, true)
	if err != nil {
		return nil, computeErr(err)
	}
	for _, m := range active {
		rs.pool.Burn(m.MappedPort)
	}
	e.runs[runID] = rs
	return rs, nil
}

// reject builds a rejection decision.
func reject(cat model.Category, code, reason string, observed, effective time.Time) model.Decision {
	return model.Decision{
		Accepted: false, Category: cat, Code: code, Reason: reason,
		ObservedAt: observed, EffectiveAt: effective,
	}
}

// record assembles and persists the event row for a finished decision. Decisions
// rejected before state exists (structural validation) carry zeroed bookkeeping.
func (e *Engine) record(ctx context.Context, runID string, p model.Packet, d model.Decision) (*RunResult, error) {
	var mappingID int64
	var mappedPort uint16
	var state string
	if d.Mapping != nil {
		mappingID = d.Mapping.ID
		mappedPort = d.Mapping.MappedPort
		state = d.Mapping.State
	}
	detail := map[string]any{
		"active_count": d.ActiveCount,
		"swept":        d.Swept,
		"clock_rewind": d.ClockRewind,
	}
	if d.Translated != nil {
		detail["translated"] = d.Translated
	}
	if d.Mapping != nil {
		detail["expires_at"] = d.Mapping.ExpiresAt
		detail["mapping_state"] = d.Mapping.State
	}
	detailJSON, _ := json.Marshal(detail)
	ev := &model.Event{
		RunID: runID, Seq: p.Seq,
		ObservedAt: d.ObservedAt, EffectiveAt: d.EffectiveAt, ClockRewind: d.ClockRewind,
		Packet: p, Accepted: d.Accepted, Category: d.Category, Code: d.Code,
		Reason: d.Reason, MappingID: mappingID, MappedPort: mappedPort,
		State: state, Detail: string(detailJSON),
	}
	if err := e.store.AppendEvent(ctx, ev); err != nil {
		return nil, computeErr(err)
	}
	return &RunResult{Decision: d, Event: ev, Persisted: d.Mapping}, nil
}

// validatePacket returns ("", "") on success or a stable code + reason.
func validatePacket(p model.Packet) (code, reason string) {
	if p.ObservedAt.IsZero() {
		return model.CodeInvalidTimestamp, "ts is zero or missing (RFC3339 required)"
	}
	if p.Direction != model.Outbound && p.Direction != model.Inbound {
		return model.CodeBadDirection, "direction must be 'outbound' or 'inbound'"
	}
	if p.Fragment.IsFragment() {
		return model.CodeFragment, "non-reassembled IP fragment: only reassembled input is supported"
	}
	switch p.FiveTuple.Protocol {
	case model.TCP:
		if p.Flags == "" {
			return model.CodeBadFlag, "TCP packet requires flags (e.g. SYN, ACK, FIN+ACK, RST)"
		}
		if _, errMsg := parseTCPFlags(p.Flags); errMsg != "" {
			return model.CodeBadFlag, errMsg
		}
	case model.UDP:
		if p.Flags != "" {
			return model.CodeUDPFlags, "UDP packets must not carry TCP-style flags"
		}
	default:
		return model.CodeUnsupportedProto, "only TCP and UDP are modeled"
	}
	if _, err := netip.ParseAddr(p.FiveTuple.SrcIP); err != nil {
		return model.CodeBadSrcIP, "src_ip not parseable: " + p.FiveTuple.SrcIP
	}
	if _, err := netip.ParseAddr(p.FiveTuple.DstIP); err != nil {
		return model.CodeBadDstIP, "dst_ip not parseable: " + p.FiveTuple.DstIP
	}
	if p.FiveTuple.SrcPort == 0 {
		return model.CodeBadSrcPort, "src_port must be non-zero"
	}
	if p.FiveTuple.DstPort == 0 {
		return model.CodeBadDstPort, "dst_port must be non-zero"
	}
	return "", ""
}
