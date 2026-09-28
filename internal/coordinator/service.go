// Package coordinator is the application service layer. It orchestrates the
// pure kernel and the PostgreSQL store: load folded state -> run a kernel
// command -> persist the event batch under an optimistic group-version check
// -> return the post-fold state. Every step emits a correlated trace.
package coordinator

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"opp291/coordinator/internal/kernel"
	"opp291/coordinator/internal/protocol"
	"opp291/coordinator/internal/store"
)

// maxApplyRetries bounds optimistic-concurrency retries on VERSION_CONFLICT.
const maxApplyRetries = 5

// Service is the coordinator API surface used by the HTTP handlers.
type Service struct {
	st        *store.Store
	now       func() time.Time
	idCounter func() string
}

// New constructs a Service.
func New(st *store.Store) *Service {
	return &Service{st: st, now: time.Now}
}

// SetClock overrides time generation (tests/deterministic demos).
func (s *Service) SetClock(f func() time.Time) {
	if f != nil {
		s.now = f
	}
}

func (s *Service) clock() time.Time { return s.now().UTC() }

// traceBuilder accumulates steps for one request.
type traceBuilder struct {
	reqID, method, path, member string
	gen                         int64
	rows                        []store.Trace
}

func (tb *traceBuilder) add(step, location string, ok bool, version int64, code, detail string, uncertain bool, extra map[string]any) {
	tb.rows = append(tb.rows, store.Trace{
		RequestID: tb.reqID, Method: tb.method, Path: tb.path, MemberID: tb.member,
		Generation: tb.gen, Step: step, Version: version, Location: location,
		OK: ok, FailCode: code, FailDetail: detail, Uncertain: uncertain, Extra: extra,
	})
}

// ---- Create ----

func (s *Service) CreateGroup(ctx context.Context, req protocol.CreateGroupRequest, reqID string) error {
	tb := &traceBuilder{reqID: reqID, method: "POST", path: "/groups"}
	at := s.clock()
	timeout, perr := time.ParseDuration(orDefault(req.SessionTimeout, "10s"))
	if perr != nil || req.PartitionCount <= 0 {
		tb.member = ""
		tb.add("validate", "coordinator.CreateGroup", false, 0, string(protocol.FailureInvalidRequest), perrString(perr), false, nil)
		_ = s.st.AppendTraces(ctx, tb.rows)
		return apiErr(protocol.FailureInvalidRequest, "invalid create group request", reqID)
	}
	tb.add("kernel.create", "kernel.CreateGroup", true, 0, "", "", false, map[string]any{"partition_count": req.PartitionCount})
	evs, err := kernel.CreateGroup(req.PartitionCount, timeout, at, reqID)
	if err != nil {
		tb.add("kernel.create", "kernel.CreateGroup", false, 0, codeOf(err), err.Error(), false, nil)
		_ = s.st.AppendTraces(ctx, tb.rows)
		return mapErr(err, reqID)
	}
	if err := s.st.CreateGroup(ctx, req.GroupID, req.PartitionCount, timeout, evs, withCreateTraces(tb, evs)); err != nil {
		if errors.Is(err, store.ErrVersionConflict) {
			return apiErr(protocol.FailureAlreadyMember, "group already exists", reqID)
		}
		return err
	}
	return nil
}

func withCreateTraces(tb *traceBuilder, evs []kernel.Event) []store.Trace {
	tb.add("store.append", "store.CreateGroup", true, int64(len(evs)), "", "", false, nil)
	tb.add("fold", "kernel.Fold", true, int64(len(evs)), "", "", false, nil)
	return tb.rows
}

// ---- Join ----

func (s *Service) Join(ctx context.Context, groupID string, memberID string, reqID string) (*kernel.State, error) {
	tb := &traceBuilder{reqID: reqID, method: "POST", path: "/groups/" + groupID + "/join", member: memberID}
	return s.mutate(ctx, groupID, reqID, tb, func(st *kernel.State) ([]kernel.Event, error) {
		tb.add("kernel.join", "kernel.Join", true, st.Version, "", "", false, nil)
		return kernel.Join(st, memberID, s.clock(), reqID)
	})
}

// ---- Leave ----

func (s *Service) Leave(ctx context.Context, groupID, memberID, reqID string) (*kernel.State, error) {
	tb := &traceBuilder{reqID: reqID, method: "POST", path: "/groups/" + groupID + "/leave", member: memberID}
	return s.mutate(ctx, groupID, reqID, tb, func(st *kernel.State) ([]kernel.Event, error) {
		return kernel.Leave(st, memberID, s.clock(), reqID)
	})
}

// ---- Expire (sweeper) ----

func (s *Service) Expire(ctx context.Context, groupID, memberID, reqID string) (*kernel.State, error) {
	tb := &traceBuilder{reqID: reqID, method: "SWEEP", path: "/groups/" + groupID + "/sweep", member: memberID}
	st, err := s.mutate(ctx, groupID, reqID, tb, func(cur *kernel.State) ([]kernel.Event, error) {
		return kernel.Expire(cur, memberID, s.clock(), reqID)
	})
	if err == nil {
		// Flag uncertainty explicitly for the operator.
		tb.add("sweep.uncertain", "coordinator.Expire", true, st.Version, "", "", true, nil)
		_ = s.st.AppendTraces(ctx, []store.Trace{tb.rows[len(tb.rows)-1]})
	}
	return st, err
}

// ---- Heartbeat ----

func (s *Service) Heartbeat(ctx context.Context, groupID, memberID string, gen int64, reqID string) (*kernel.State, bool, error) {
	tb := &traceBuilder{reqID: reqID, method: "POST", path: "/groups/" + groupID + "/heartbeat", member: memberID, gen: gen}
	var rebalancing bool
	st, err := s.mutate(ctx, groupID, reqID, tb, func(cur *kernel.State) ([]kernel.Event, error) {
		evs, rb, kerr := kernel.Heartbeat(cur, memberID, gen, s.clock(), reqID)
		rebalancing = rb
		if kerr != nil {
			return nil, kerr
		}
		return evs, nil
	})
	return st, rebalancing, err
}

// ---- Revoke confirm ----

// ConfirmResult carries the post-confirmation state, settled flag and the
// per-partition outcomes.
type ConfirmResult struct {
	State   *kernel.State
	Settled bool
	Results []protocol.RevokeResult
}

func (s *Service) Confirm(ctx context.Context, groupID, memberID string, gen int64, parts []int, reqID string) (*ConfirmResult, error) {
	tb := &traceBuilder{reqID: reqID, method: "POST", path: "/groups/" + groupID + "/revoke", member: memberID, gen: gen}
	var settled bool
	var failures []kernel.ItemFailure
	requested := append([]int(nil), parts...)
	st, err := s.mutate(ctx, groupID, reqID, tb, func(cur *kernel.State) ([]kernel.Event, error) {
		evs, stl, kerr := kernel.ConfirmRevocation(cur, memberID, gen, parts, s.clock(), reqID)
		settled = stl
		if kerr != nil {
			failures = kerr.Items
			// A partial confirmation still produces events for the valid items;
			// persist them, then surface the per-item failures.
			if len(evs) > 0 {
				return evs, nil
			}
			return nil, kerr
		}
		return evs, nil
	})
	res := &ConfirmResult{State: st, Settled: settled}
	okSet := map[int]bool{}
	for _, pid := range requested {
		if !containsInt(failedPids(failures), pid) {
			okSet[pid] = true
		}
	}
	for _, pid := range requested {
		r := protocol.RevokeResult{Partition: pid, OK: okSet[pid]}
		if okSet[pid] && st != nil {
			r.NewOwner = st.Partitions[pid].Owner
		}
		res.Results = append(res.Results, r)
	}
	if err != nil {
		return res, err
	}
	if len(failures) > 0 {
		return res, itemError(failures, reqID)
	}
	return res, nil
}

// ---- Commit ----

type CommitResult struct {
	State    *kernel.State
	Accepted []protocol.CommitResult
	Failures []protocol.ItemError
}

func (s *Service) Commit(ctx context.Context, groupID, memberID string, gen int64, items []protocol.CommitItem, reqID string) (*CommitResult, error) {
	tb := &traceBuilder{reqID: reqID, method: "POST", path: "/groups/" + groupID + "/commit", member: memberID, gen: gen}
	kitems := make([]kernel.CommitItem, len(items))
	for i, it := range items {
		kitems[i] = kernel.CommitItem{Partition: it.Partition, Offset: it.Offset}
	}
	var kfails []kernel.ItemFailure
	st, err := s.mutate(ctx, groupID, reqID, tb, func(cur *kernel.State) ([]kernel.Event, error) {
		evs, fails := kernel.Commit(cur, memberID, gen, kitems, s.clock(), reqID)
		kfails = fails
		if len(evs) == 0 && len(fails) > 0 {
			// All rejected: represent as an error but still trace.
			return nil, &kernel.Error{Code: fails[0].Code, Message: "all commits rejected", Items: fails}
		}
		return evs, nil
	})
	cr := &CommitResult{State: st}
	accepted := map[int]int64{}
	for _, it := range kitems {
		accepted[it.Partition] = it.Offset
	}
	for _, f := range kfails {
		delete(accepted, f.Partition)
		cr.Failures = append(cr.Failures, protocol.ItemError{
			Partition: f.Partition, Code: protocol.FailureCode(f.Code), SubReason: f.SubReason, Message: f.Message,
		})
	}
	for pid, off := range accepted {
		cr.Accepted = append(cr.Accepted, protocol.CommitResult{Partition: pid, Offset: off, OK: true})
	}
	if err != nil {
		return cr, err
	}
	return cr, nil
}

// ---- Reads ----

func (s *Service) Load(ctx context.Context, groupID string) (*kernel.State, error) {
	st, _, err := s.st.LoadGroup(ctx, groupID)
	if errors.Is(err, store.ErrNotFound) {
		return nil, apiErr(protocol.FailureUnknownMember, "unknown group", "")
	}
	return st, err
}

func (s *Service) Events(ctx context.Context, groupID string) ([]kernel.Event, error) {
	_, evs, err := s.st.LoadGroup(ctx, groupID)
	return evs, err
}

func (s *Service) Traces(ctx context.Context, reqID string) ([]store.TraceRow, error) {
	return s.st.ListTraces(ctx, reqID, 500)
}

// ---- Sweeper ----

// SweepExpired force-expires active members whose last heartbeat is older than
// the group session timeout. It is invoked periodically by the server.
func (s *Service) SweepExpired(ctx context.Context, reqID string) []string {
	meta, err := s.listGroups(ctx)
	if err != nil {
		return nil
	}
	var expired []string
	now := s.clock()
	for _, gid := range meta {
		st, err := s.Load(ctx, gid)
		if err != nil {
			continue
		}
		for _, m := range st.Members {
			if !m.Active {
				continue
			}
			if now.Sub(m.LastSeen) > st.SessionTimeout {
				if _, err := s.Expire(ctx, gid, m.ID, reqID+"-"+m.ID); err == nil {
					expired = append(expired, gid+":"+m.ID)
				}
			}
		}
	}
	return expired
}

func (s *Service) listGroups(ctx context.Context) ([]string, error) {
	// Reuse a lightweight query through the store; add a small helper there.
	return s.st.ListGroups(ctx)
}

// ---- mutation helper with OCC retry ----

func (s *Service) mutate(ctx context.Context, groupID, reqID string, tb *traceBuilder,
	cmd func(*kernel.State) ([]kernel.Event, error)) (*kernel.State, error) {

	for attempt := 0; attempt < maxApplyRetries; attempt++ {
		st, _, lerr := s.st.LoadGroup(ctx, groupID)
		if lerr != nil {
			if errors.Is(lerr, store.ErrNotFound) {
				tb.add("load", "store.LoadGroup", false, 0, string(protocol.FailureUnknownMember), "unknown group", false, nil)
				_ = s.st.AppendTraces(ctx, tb.rows)
				return nil, apiErr(protocol.FailureUnknownMember, "unknown group: "+groupID, reqID)
			}
			return nil, lerr
		}
		tb.add("load", "store.LoadGroup", true, st.Version, "", "", false, nil)

		evs, cerr := cmd(st)
		if cerr != nil {
			ke := asKernelErr(cerr)
			code := codeOf(cerr)
			tb.add("kernel.command", "kernel", false, st.Version, code, cerr.Error(), ke != nil && ke.Uncertain, nil)
			_ = s.st.AppendTraces(ctx, tb.rows)
			return nil, mapErr(cerr, reqID)
		}
		if len(evs) == 0 {
			tb.add("noop", "coordinator.mutate", true, st.Version, "", "", false, nil)
			_ = s.st.AppendTraces(ctx, tb.rows)
			return st, nil
		}
		tb.add("store.append", "store.Apply", true, st.Version, "", "", false, map[string]any{"events": len(evs)})
		tb.add("fold", "kernel.Fold", true, st.Version+int64(len(evs)), "", "", false, nil)
		newState, aerr := s.st.Apply(ctx, groupID, st.Version, evs, tb.rows)
		if aerr == nil {
			return newState, nil
		}
		if errors.Is(aerr, store.ErrVersionConflict) {
			tb.add("store.retry", "store.Apply", false, st.Version, string(protocol.FailureConflict), "version conflict; retrying", false, map[string]any{"attempt": attempt + 1})
			tb.rows = tb.rows[:0]
			continue
		}
		return nil, aerr
	}
	return nil, apiErr(protocol.FailureConflict, "too many version conflicts", reqID)
}

// ---- error mapping ----

func asKernelErr(err error) *kernel.Error {
	var ke *kernel.Error
	if errors.As(err, &ke) {
		return ke
	}
	return nil
}

func codeOf(err error) string {
	if ke := asKernelErr(err); ke != nil {
		return ke.Code
	}
	return "INTERNAL"
}

func mapErr(err error, reqID string) error {
	ke := asKernelErr(err)
	if ke == nil {
		return err
	}
	out := &protocol.APIError{
		Code:      protocol.FailureCode(ke.Code),
		Message:   ke.Message,
		RequestID: reqID,
		Uncertain: ke.Uncertain,
	}
	for _, it := range ke.Items {
		out.Details = append(out.Details, protocol.ItemError{
			Partition: it.Partition, Code: protocol.FailureCode(it.Code),
			SubReason: it.SubReason, Message: it.Message,
		})
	}
	if out.Code == "PARTIAL_FAILURE" {
		// Batch partially applied: 207-style payload is handled by the server;
		// keep the category for logging.
		out.Code = protocol.FailureUnavailablePartition
	}
	return out
}

func itemError(items []kernel.ItemFailure, reqID string) error {
	out := &protocol.APIError{
		Code:      "PARTIAL_FAILURE",
		Message:   "some items failed",
		RequestID: reqID,
	}
	for _, it := range items {
		out.Details = append(out.Details, protocol.ItemError{
			Partition: it.Partition, Code: protocol.FailureCode(it.Code),
			SubReason: it.SubReason, Message: it.Message,
		})
	}
	return out
}

func apiErr(code protocol.FailureCode, msg, reqID string) *protocol.APIError {
	return &protocol.APIError{Code: code, Message: msg, RequestID: reqID}
}

func orDefault(v, d string) string {
	if strings.TrimSpace(v) == "" {
		return d
	}
	return v
}

func perrString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func containsInt(xs []int, v int) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func failedPids(items []kernel.ItemFailure) []int {
	out := make([]int, 0, len(items))
	for _, it := range items {
		out = append(out, it.Partition)
	}
	return out
}

// IsPartial reports whether an error is a batch-partial result.
func IsPartial(err error) (*protocol.APIError, bool) {
	var ae *protocol.APIError
	if errors.As(err, &ae) && ae.Code == "PARTIAL_FAILURE" {
		return ae, true
	}
	return nil, false
}

// Ensure unused import is referenced if trimmed.
var _ = fmt.Sprintf
