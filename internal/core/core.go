// Package core is the causal-broadcast computational kernel.
//
// Inputs: local events (Broadcast) and network messages (Ingest).
// Outputs: per-ingest verdicts (deliver / buffered / duplicate / conflict /
// rejected / overflow) and local delivery orders.
//
// The kernel decides "deliverable" with vector clocks: receive != deliver.
// It holds no goroutine state of its own; persistence lives behind store.Store
// and every delivery advancement is one store transaction, so recovery after
// restart simply re-reads pending rows and the durable delivered clock.
package core

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"sort"
	"sync"
	"time"

	"cbcast/internal/clock"
	"cbcast/internal/protocol"
	"cbcast/internal/store"
)

// ErrBufferFull is the sentinel returned on capacity backpressure.
var ErrBufferFull = errors.New("causal buffer full")

// DecisionLogger receives structured evidence for every kernel decision. The
// HTTP layer may be a no-op; tests wire a capturing sink so logs can be
// correlated to inputs (attempt_id, run_id) and inspected.
type DecisionLogger interface {
	LogDecision(rec DecisionRecord)
}

// DecisionRecord is one JSON-serializable line of decision evidence.
type DecisionRecord struct {
	Schema      string          `json:"schema"`
	Version     string          `json:"version"`
	RunID       string          `json:"run_id"`
	NodeID      string          `json:"node_id"`
	AttemptID   string          `json:"attempt_id"`
	AttemptSeq  uint64          `json:"attempt_seq"`
	Step        string          `json:"step"`
	Verdict     string          `json:"verdict"`
	MessageID   string          `json:"message_id"`
	Sender      string          `json:"sender,omitempty"`
	Source      string          `json:"source,omitempty"` // "local" | "network"
	IncomingVC  protocol.VC     `json:"incoming_vc,omitempty"`
	DeliveredVC protocol.VC     `json:"delivered_vc,omitempty"`
	WaitingFor  []protocol.Gap  `json:"waiting_for,omitempty"`
	Reason      string          `json:"reason,omitempty"`
	BufferUsed  int             `json:"buffer_used,omitempty"`
	BufferCap   int             `json:"buffer_cap,omitempty"`
	Cascade     []cascadeStep   `json:"cascade,omitempty"`
	DeliverSeq  uint64          `json:"deliver_seq,omitempty"`
	Timestamp   time.Time       `json:"ts"`
}

type cascadeStep struct {
	Order     int         `json:"order"`
	MessageID string      `json:"message_id"`
	Sender    string      `json:"sender"`
	VC        protocol.VC `json:"vc"`
	Basis     string      `json:"basis"`
}

// Options configures a Core.
type Options struct {
	NodeID          string
	Members         []string // canonical sorted membership
	BufferCapacity  int
	MaxPayloadBytes int
	Store           store.Store
	Logger          DecisionLogger
	RunID           string
	Clock           func() time.Time
}

// Core is the kernel. One Core serves one node's traffic and is safe for
// concurrent use (a mutex serializes decisions; the store provides durable
// transactional commit).
type Core struct {
	nodeID  string
	members []string
	memberS map[string]bool
	bufCap  int
	maxPay  int
	st      store.Store
	log     DecisionLogger
	now     func() time.Time

	mu        sync.Mutex
	runID     string
	attemptN  uint64
}

// New builds a Core and reconciles any pending rows left from a previous run
// (they remain pending; nothing speculative is delivered until its gaps close).
func New(opts Options) (*Core, error) {
	if opts.Store == nil {
		return nil, errors.New("core: store is required")
	}
	if opts.Logger == nil {
		opts.Logger = nopLogger{}
	}
	if opts.Clock == nil {
		opts.Clock = time.Now
	}
	runID := opts.RunID
	if runID == "" {
		runID = newRunID()
	}
	c := &Core{
		nodeID:  opts.NodeID,
		members: append([]string(nil), opts.Members...),
		memberS: memberSet(opts.Members),
		bufCap:  opts.BufferCapacity,
		maxPay:  opts.MaxPayloadBytes,
		st:      opts.Store,
		log:     opts.Logger,
		now:     opts.Clock,
		runID:   runID,
	}
	return c, nil
}

// RunID returns the identity stamped into decision records.
func (c *Core) RunID() string { return c.runID }

// SetRunID overrides the run identity (used by tests to tie logs to a run).
func (c *Core) SetRunID(id string) {
	c.mu.Lock()
	c.runID = id
	c.mu.Unlock()
}

// Store exposes the persistence layer (replay/status handlers use it).
func (c *Core) Store() store.Store { return c.st }

// Members returns the canonical membership.
func (c *Core) Members() []string { return append([]string(nil), c.members...) }

// Broadcast creates a local event, delivers it locally (which by construction
// also releases any buffered successors it closes) and returns the envelope to
// be sent to peers by the transport. The local delivery verdict is in res.
func (c *Core) Broadcast(ctx context.Context, payload []byte) (*protocol.Envelope, *protocol.IngestResult, error) {
	attemptSeq, attemptID := c.nextAttempt()
	rec := c.baseRec(attemptSeq, attemptID, "local", "broadcast")

	if c.maxPay > 0 && len(payload) > c.maxPay {
		verr := &protocol.ValidationError{
			Code:    protocol.ValOversize,
			Message: fmt.Sprintf("payload %d bytes exceeds limit %d", len(payload), c.maxPay),
		}
		c.finishRec(rec, string(protocol.VerdictRejected), "", verr)
		return nil, c.rejectResult(attemptID, verr), verr
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	tx, err := c.st.BeginDelivery(ctx)
	if err != nil {
		return nil, nil, err
	}
	base := tx.CurrentClock()

	vc := clock.Clone(base)
	clock.Tick(vc, c.nodeID)
	env := &protocol.Envelope{
		MessageID:   protocol.ExpectedMessageID(c.nodeID, vc[c.nodeID]),
		Sender:      c.nodeID,
		Clock:       vc,
		Payload:     append([]byte(nil), payload...),
		PayloadHash: protocol.ComputePayloadHash(payload),
		CreatedAt:   c.now().UTC(),
	}
	rec.MessageID = env.MessageID
	rec.Sender = env.Sender
	rec.IncomingVC = clock.Clone(vc)

	outcome, err := c.st.Put(ctx, env)
	if err != nil {
		tx.Discard()
		return nil, nil, err
	}
	// Local identity cannot have existed unless clock state was rewound.
	if outcome != store.PutInserted {
		tx.Discard()
		return nil, nil, fmt.Errorf("core: local event %s already present (store corruption or clock rewind)", env.MessageID)
	}

	// Local event deliverable on base by construction; cascade may release
	// buffered successors waiting on this local tick.
	pending, err := c.st.ListPending(ctx)
	if err != nil {
		tx.Discard()
		return nil, nil, err
	}
	ordered, steps, newClock, err := c.buildCascade(ctx, tx, base, []*protocol.Envelope{env}, pending)
	if err != nil {
		tx.Discard()
		return nil, nil, err
	}
	if err := tx.CommitDelivery(ordered, newClock); err != nil {
		return nil, nil, err
	}

	res := &protocol.IngestResult{
		Verdict:    protocol.VerdictDelivered,
		MessageID:  env.MessageID,
		AttemptID:  attemptID,
		Delivered:  ordered,
		BufferUsed: c.pendingCount(ctx),
		BufferCap:  c.bufCap,
	}
	c.finishRec(rec, string(protocol.VerdictDelivered), env.MessageID, nil, withCascade(steps), withVCs(base, newClock))
	return env, res, nil
}

// Ingest processes one network message. It never returns "success" for an
// unknown/error input: the Verdict field carries the exact category.
func (c *Core) Ingest(ctx context.Context, env *protocol.Envelope, source string) *protocol.IngestResult {
	attemptSeq, attemptID := c.nextAttempt()
	rec := c.baseRec(attemptSeq, attemptID, "network", "ingest")
	rec.Sender = safeSender(env)
	rec.Source = source
	rec.MessageID = safeID(env)
	if env != nil {
		rec.IncomingVC = clock.Clone(env.Clock)
	}

	if err := protocol.Validate(env, c.memberS, c.maxPay); err != nil {
		ve, _ := protocol.AsValidationError(err)
		c.finishRec(rec, string(protocol.VerdictRejected), rec.MessageID, err)
		return c.rejectResult(attemptID, ve)
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	tx, err := c.st.BeginDelivery(ctx)
	if err != nil {
		return c.errorResult(attemptID, env.MessageID, err)
	}
	delivered := tx.CurrentClock()
	rec.DeliveredVC = clock.Clone(delivered)

	// Known identity: duplicate (same content) or conflict (different content).
	if existing, status, err := c.st.Get(ctx, env.MessageID); err == nil && existing != nil {
		tx.Discard()
		switch {
		case existing.PayloadHash != env.PayloadHash:
			reason := fmt.Sprintf("message %s already known with payload_hash %s; got %s",
				env.MessageID, existing.PayloadHash, env.PayloadHash)
			c.finishRec(rec, string(protocol.VerdictConflict), env.MessageID,
				errors.New(reason))
			return &protocol.IngestResult{
				Verdict:    protocol.VerdictConflict,
				MessageID:  env.MessageID,
				AttemptID:  attemptID,
				Reason:     reason,
				BufferUsed: c.pendingCount(ctx),
				BufferCap:  c.bufCap,
			}
		default:
			reason := "identical envelope already received"
			if status == store.StatusDelivered {
				reason = "identical envelope already delivered"
			}
			c.finishRec(rec, string(protocol.VerdictDuplicate), env.MessageID,
				errors.New(reason))
			return &protocol.IngestResult{
				Verdict:    protocol.VerdictDuplicate,
				MessageID:  env.MessageID,
				AttemptID:  attemptID,
				Reason:     reason,
				BufferUsed: c.pendingCount(ctx),
				BufferCap:  c.bufCap,
			}
		}
	} else if err != nil {
		tx.Discard()
		return c.errorResult(attemptID, env.MessageID, err)
	}

	// Gather currently buffered messages (used for capacity and cascade).
	pending, err := c.st.ListPending(ctx)
	if err != nil {
		tx.Discard()
		return c.errorResult(attemptID, env.MessageID, err)
	}

	// Backpressure: never silently drop predecessors. Capacity is checked
	// BEFORE the new row is stored.
	if c.bufCap > 0 && len(pending) >= c.bufCap {
		tx.Discard()
		gaps := clock.MissingGaps(env.Clock, env.Sender, delivered)
		reason := fmt.Sprintf("buffer capacity %d exhausted; refusing (not dropping) message %s", c.bufCap, env.MessageID)
		res := &protocol.IngestResult{
			Verdict:    protocol.VerdictOverflow,
			MessageID:  env.MessageID,
			AttemptID:  attemptID,
			WaitingFor: gaps,
			Reason:     reason,
			BufferUsed: len(pending),
			BufferCap:  c.bufCap,
		}
		c.finishRec(rec, string(protocol.VerdictOverflow), env.MessageID,
			errors.New(reason), withGaps(gaps))
		return res
	}

	outcome, err := c.st.Put(ctx, env)
	if err != nil {
		tx.Discard()
		return c.errorResult(attemptID, env.MessageID, err)
	}
	if outcome == store.PutDuplicate {
		tx.Discard()
		c.finishRec(rec, string(protocol.VerdictDuplicate), env.MessageID,
			errors.New("race: identical envelope inserted concurrently"))
		return &protocol.IngestResult{Verdict: protocol.VerdictDuplicate, MessageID: env.MessageID, AttemptID: attemptID}
	}
	if outcome == store.PutConflict {
		tx.Discard()
		c.finishRec(rec, string(protocol.VerdictConflict), env.MessageID,
			errors.New("race: same id different content inserted concurrently"))
		return &protocol.IngestResult{Verdict: protocol.VerdictConflict, MessageID: env.MessageID, AttemptID: attemptID}
	}

	// Causally ready now? Build the cascade starting from this message.
	gaps := clock.MissingGaps(env.Clock, env.Sender, delivered)
	if len(gaps) > 0 {
		tx.Discard()
		used := c.pendingCount(ctx)
		res := &protocol.IngestResult{
			Verdict:    protocol.VerdictBuffered,
			MessageID:  env.MessageID,
			AttemptID:  attemptID,
			WaitingFor: gaps,
			Reason:     fmt.Sprintf("waiting on %d predecessor gap(s): %s", len(gaps), joinGaps(gaps)),
			BufferUsed: used,
			BufferCap:  c.bufCap,
		}
		c.finishRec(rec, string(protocol.VerdictBuffered), env.MessageID,
			errors.New(res.Reason), withGaps(gaps))
		return res
	}

	// Deliver: trigger + everything that becomes deliverable in the cascade.
	// `pending` was snapshotted before Put so it excludes the trigger itself.
	ordered, steps, newClock, err := c.buildCascade(ctx, tx, delivered, []*protocol.Envelope{env}, pending)
	if err != nil {
		tx.Discard()
		return c.errorResult(attemptID, env.MessageID, err)
	}
	if err := tx.CommitDelivery(ordered, newClock); err != nil {
		return c.errorResult(attemptID, env.MessageID, err)
	}
	res := &protocol.IngestResult{
		Verdict:    protocol.VerdictDelivered,
		MessageID:  env.MessageID,
		AttemptID:  attemptID,
		Delivered:  ordered,
		BufferUsed: c.pendingCount(ctx),
		BufferCap:  c.bufCap,
	}
	c.finishRec(rec, string(protocol.VerdictDelivered), env.MessageID, nil,
		withCascade(steps), withVCs(delivered, newClock))
	return res
}

// buildCascade returns the local delivery order triggered by seeds (already
// stored pending) on top of clock base, plus the clock after merging them.
// Caller must verify seeds are deliverable against base.
//
// pool is the set of other buffered messages that may be released by the
// cascade; seeds themselves must not be in pool (they are the frontier).
func (c *Core) buildCascade(ctx context.Context, tx store.DeliveryTxn, base protocol.VC, seeds []*protocol.Envelope, pool []*protocol.Envelope) ([]*protocol.Envelope, []cascadeStep, protocol.VC, error) {
	cur := clock.Clone(base)
	used := map[string]bool{}
	var ordered []*protocol.Envelope
	var steps []cascadeStep

	frontier := append([]*protocol.Envelope(nil), seeds...)
	for len(frontier) > 0 {
		// Deterministic exploration order at equal readiness: by MessageID
		// (== "<sender>:<sender-seq>").
		sort.Slice(frontier, func(i, j int) bool {
			return frontier[i].MessageID < frontier[j].MessageID
		})
		for _, env := range frontier {
			used[env.MessageID] = true
			ordered = append(ordered, env)
			steps = append(steps, cascadeStep{
				Order:     len(ordered),
				MessageID: env.MessageID,
				Sender:    env.Sender,
				VC:        clock.Clone(env.Clock),
				Basis:     cascadeBasis(env, cur),
			})
			clock.Merge(cur, env.Clock)
		}
		// Among the remaining pool, everything deliverable on the new clock
		// joins the next frontier.
		remaining := make([]*protocol.Envelope, 0, len(pool))
		var next []*protocol.Envelope
		for _, p := range pool {
			if used[p.MessageID] {
				continue
			}
			if clock.Deliverable(p.Clock, p.Sender, cur) {
				next = append(next, p)
			} else {
				remaining = append(remaining, p)
			}
		}
		pool = remaining
		frontier = next
	}
	return ordered, steps, cur, nil
}

func cascadeBasis(env *protocol.Envelope, before protocol.VC) string {
	gaps := clock.MissingGaps(env.Clock, env.Sender, before)
	if len(gaps) == 0 {
		return fmt.Sprintf("delivered clock %s satisfies event clock %s", fmtVC(before), fmtVC(env.Clock))
	}
	return fmt.Sprintf("released after predecessors closed; remaining gaps at enqueue: %s", joinGaps(gaps))
}

// PendingView exposes the current buffer with explicit waiting reasons, used by
// the status/debug endpoint.
func (c *Core) PendingView(ctx context.Context) ([]PendingInfo, error) {
	c.mu.Lock()
	st, err := c.st.State(ctx)
	c.mu.Unlock()
	if err != nil {
		return nil, err
	}
	pending, err := c.st.ListPending(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]PendingInfo, 0, len(pending))
	for _, p := range pending {
		out = append(out, PendingInfo{
			Envelope:   p,
			WaitingFor: clock.MissingGaps(p.Clock, p.Sender, st.DeliveredClock),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Envelope.MessageID < out[j].Envelope.MessageID })
	return out, nil
}

// PendingInfo pairs a buffered envelope with its missing predecessors.
type PendingInfo struct {
	Envelope   *protocol.Envelope
	WaitingFor []protocol.Gap
}

// Snapshot is the debug/status view.
type Snapshot struct {
	NodeID      string        `json:"node_id"`
	RunID       string        `json:"run_id"`
	Version     string        `json:"version"`
	DeliveredVC protocol.VC   `json:"delivered_vc"`
	DeliverSeq  uint64        `json:"deliver_seq"`
	Pending     []PendingInfo `json:"pending"`
	BufferCap   int           `json:"buffer_cap"`
}

// Snapshot returns state for status endpoints.
func (c *Core) Snapshot(ctx context.Context) (Snapshot, error) {
	pending, err := c.PendingView(ctx)
	if err != nil {
		return Snapshot{}, err
	}
	st, err := c.st.State(ctx)
	if err != nil {
		return Snapshot{}, err
	}
	return Snapshot{
		NodeID:      c.nodeID,
		RunID:       c.runID,
		Version:     protocol.Version,
		DeliveredVC: st.DeliveredClock,
		DeliverSeq:  st.DeliverSeq,
		Pending:     pending,
		BufferCap:   c.bufCap,
	}, nil
}

func (c *Core) pendingCount(ctx context.Context) int {
	p, err := c.st.ListPending(ctx)
	if err != nil {
		return -1
	}
	return len(p)
}

func (c *Core) nextAttempt() (uint64, string) {
	c.mu.Lock()
	c.attemptN++
	n := c.attemptN
	run := c.runID
	c.mu.Unlock()
	return n, fmt.Sprintf("%s#a%d", run, n)
}

func (c *Core) rejectResult(attemptID string, ve *protocol.ValidationError) *protocol.IngestResult {
	reason := ""
	if ve != nil {
		reason = ve.Code + ": " + ve.Message
	}
	return &protocol.IngestResult{
		Verdict:   protocol.VerdictRejected,
		AttemptID: attemptID,
		Reason:    reason,
		BufferCap: c.bufCap,
	}
}

func (c *Core) errorResult(attemptID, messageID string, err error) *protocol.IngestResult {
	// Internal errors are surfaced honestly as a rejected verdict with an
	// error-class reason; they are never reported as delivered.
	return &protocol.IngestResult{
		Verdict:   protocol.VerdictRejected,
		MessageID: messageID,
		AttemptID: attemptID,
		Reason:    "internal_error: " + err.Error(),
		BufferCap: c.bufCap,
	}
}

// --- decision records -------------------------------------------------------

type recOption func(*DecisionRecord)

func withCascade(steps []cascadeStep) recOption {
	return func(r *DecisionRecord) { r.Cascade = steps }
}
func withVCs(before, after protocol.VC) recOption {
	return func(r *DecisionRecord) {
		r.DeliveredVC = clock.Clone(after)
	}
}
func withGaps(gaps []protocol.Gap) recOption {
	return func(r *DecisionRecord) { r.WaitingFor = gaps }
}

func (c *Core) baseRec(seq uint64, attemptID, source, step string) DecisionRecord {
	return DecisionRecord{
		Schema:    "cbcast.decision/v1",
		Version:   protocol.Version,
		RunID:     c.runID,
		NodeID:    c.nodeID,
		AttemptID: attemptID,
		AttemptSeq: seq,
		Step:      step,
		Source:    source,
		Timestamp: c.now().UTC(),
	}
}

func (c *Core) finishRec(rec DecisionRecord, verdict, messageID string, cause error, opts ...recOption) {
	rec.Verdict = verdict
	if messageID != "" {
		rec.MessageID = messageID
	}
	if cause != nil {
		rec.Reason = cause.Error()
	}
	rec.BufferUsed = c.pendingCount(context.Background())
	rec.BufferCap = c.bufCap
	for _, o := range opts {
		o(&rec)
	}
	c.log.LogDecision(rec)
}

// --- small helpers ----------------------------------------------------------

func memberSet(members []string) map[string]bool {
	m := make(map[string]bool, len(members))
	for _, x := range members {
		m[x] = true
	}
	return m
}

func joinGaps(gaps []protocol.Gap) string {
	out := ""
	for i, g := range gaps {
		if i > 0 {
			out += ","
		}
		out += g.String()
	}
	return out
}

func fmtVC(vc protocol.VC) string {
	keys := make([]string, 0, len(vc))
	for k := range vc {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	s := "{"
	for i, k := range keys {
		if i > 0 {
			s += ","
		}
		s += fmt.Sprintf("%s:%d", k, vc[k])
	}
	return s + "}"
}

func safeSender(e *protocol.Envelope) string {
	if e == nil {
		return ""
	}
	return e.Sender
}
func safeID(e *protocol.Envelope) string {
	if e == nil {
		return ""
	}
	return e.MessageID
}

func newRunID() string {
	var b [6]byte
	if _, err := io.ReadFull(rand.Reader, b[:]); err != nil {
		return time.Now().UTC().Format("run-20060102T150405Z")
	}
	return "run-" + hex.EncodeToString(b[:])
}

type nopLogger struct{}

func (nopLogger) LogDecision(DecisionRecord) {}
