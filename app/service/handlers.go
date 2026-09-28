package service

import (
	"context"
	"time"

	"dsnet/app/compute"
	"dsnet/kernel"
	"dsnet/proto"
	"dsnet/store"

	"github.com/jackc/pgx/v5"
)

// Submit seeds a new run: create run row + kernel start + the root transfer
// from the root node to the root task's partition node.
func (s *Service) Submit(ctx context.Context, req proto.SubmitRequest) (*proto.RunStatus, error) {
	if req.RunID == "" {
		return nil, &ServiceError{proto.FailInvalidRequest, 400, "run_id required"}
	}
	if err := req.Root.Validate(); err != nil {
		return nil, &ServiceError{proto.FailInvalidRequest, 400, err.Error()}
	}
	now := time.Now().UTC()
	var deadline *time.Time
	if req.BudgetMs > 0 {
		d := now.Add(time.Duration(req.BudgetMs) * time.Millisecond)
		deadline = &d
	}
	row := store.RunRow{ID: req.RunID, ClientRef: req.ClientRef,
		Phase: proto.PhaseRunning, BudgetMs: int64(req.BudgetMs),
		SubmittedAt: now, Deadline: deadline}
	if err := s.st.CreateRun(ctx, row); err != nil {
		return nil, &ServiceError{proto.FailInvalidRequest, 500, err.Error()}
	}

	rootTransfer := s.newID("trf")
	to := nodeFor(req.Root.Partition)
	events, err := s.commit(ctx, req.RunID,
		func(tx pgx.Tx) error {
			// Mark run last seq + insert the root materialized edge.
			if err := store.InsertTransfer(ctx, tx, store.TransferRow{
				ID: rootTransfer, RunID: req.RunID,
				FromNode: kernel.RootNodeID, ToNode: to,
				TaskID: req.Root.ID, Partition: req.Root.Partition,
				State: proto.EdgeOpen, OpenedSeq: seqOf(events, proto.EvTransferOpen),
				Op: req.Root.Op, Spec: req.Root,
			}); err != nil {
				return err
			}
			return s.st.MarkRun(ctx, tx, req.RunID, proto.PhaseRunning, "", "",
				events[len(events)-1].Seq)
		},
		kernel.Command{Start: &kernel.StartRun{RunID: req.RunID}},
		kernel.Command{Open: &kernel.OpenTransfer{
			RunID: req.RunID, TransferID: rootTransfer,
			Sender: kernel.RootNodeID, ReceiverNode: to,
			TaskID: req.Root.ID, Partition: req.Root.Partition}},
	)
	if err != nil {
		return nil, err
	}
	s.log.Info("run submitted", "run_id", req.RunID,
		"root_transfer", rootTransfer, "request", req.ClientRef,
		"budget_ms", req.BudgetMs, "events", len(events),
		"handler", "POST /runs")
	return s.Status(ctx, req.RunID)
}

// Claim picks one queued open edge for (worker, partition) and records the
// claim atomically. Empty transfer id in the response means "queue empty" —
// explicitly NOT global termination (DS counters live in status).
func (s *Service) Claim(ctx context.Context, req proto.ClaimRequest) (*proto.ClaimResponse, error) {
	if req.WorkerID == "" || req.Partition == "" {
		return nil, &ServiceError{proto.FailInvalidRequest, 400,
			"worker_id and partition required"}
	}
	// Liveness: heartbeat the subscription so the partition node is known.
	if err := s.st.Heartbeat(ctx, req.WorkerID, req.Partition, ""); err != nil {
		return nil, &ServiceError{proto.FailInvalidRequest, 500, err.Error()}
	}

	// Decide candidate from the committed projection.
	candID, err := s.st.ClaimCandidate(ctx, "", req.Partition)
	if err != nil {
		return nil, &ServiceError{proto.FailInvalidRequest, 500, err.Error()}
	}
	if candID == "" {
		return &proto.ClaimResponse{}, nil
	}
	tr, err := s.st.GetTransfer(ctx, candID)
	if err != nil {
		return nil, &ServiceError{proto.FailUnknownTransfer, 404, candID}
	}

	events, err := s.commit(ctx, tr.RunID,
		func(tx pgx.Tx) error {
			ok, err := store.TakeClaim(ctx, tx, candID, req.WorkerID)
			if err != nil {
				return err
			}
			if !ok {
				// Another worker won the race; fail the tx so the claim
				// event is not persisted. Caller retries and finds emptiness.
				return errRaceLost
			}
			if err := s.st.MarkRun(ctx, tx, tr.RunID, proto.PhaseRunning, "", "",
				events[len(events)-1].Seq); err != nil {
				return err
			}
			return nil
		},
		kernel.Command{Claim: &kernel.ClaimTask{
			RunID: tr.RunID, TransferID: candID, NodeID: nodeFor(tr.Partition)}},
	)
	if err != nil {
		if se, ok := err.(*ServiceError); ok && se.Msg == "persist failed: "+errRaceLost.Error() {
			// Race lost: report empty, worker polls again.
			return &proto.ClaimResponse{}, nil
		}
		return nil, err
	}
	_ = events
	spec, _ := s.st.GetTaskSpec(ctx, tr.RunID, tr.TaskID)
	s.log.Info("task claimed", "run_id", tr.RunID,
		"transfer_id", candID, "task_id", tr.TaskID,
		"partition", tr.Partition, "worker", req.WorkerID,
		"handler", "POST /claim")
	return &proto.ClaimResponse{
		RunID: tr.RunID, TaskID: tr.TaskID, TransferID: candID,
		Op: spec.Op, SleepMs: spec.SleepMs, Count: spec.Count,
		ChildOp: spec.ChildOp, ChildPartition: spec.ChildPartition,
		Reason: spec.Reason, ParentTask: spec.ID,
	}, nil
}

// Start marks a claimed task executing (queued -> in-flight).
func (s *Service) Start(ctx context.Context, req proto.StartRequest) error {
	tr, err := s.st.GetTransfer(ctx, req.TransferID)
	if err != nil {
		return &ServiceError{proto.FailUnknownTransfer, 404, req.TransferID}
	}
	events, err := s.commit(ctx, tr.RunID,
		func(tx pgx.Tx) error {
			if err := s.st.MarkStarted(ctx, tx, tr.ID); err != nil {
				return err
			}
			return s.st.MarkRun(ctx, tx, tr.RunID, proto.PhaseRunning, "", "",
				events[len(events)-1].Seq)
		},
		kernel.Command{StartExec: &kernel.StartExec{
			RunID: tr.RunID, TransferID: tr.ID}})
	if err != nil {
		return err
	}
	_ = events
	return nil
}

// Complete reports success; the compute interpreter deterministically derives
// children, which are opened as causal edges BEFORE this task stops being
// active.
func (s *Service) Complete(ctx context.Context, req proto.CompleteRequest) error {
	tr, err := s.st.GetTransfer(ctx, req.TransferID)
	if err != nil {
		return &ServiceError{proto.FailUnknownTransfer, 404, req.TransferID}
	}
	spec, err := s.st.GetTaskSpec(ctx, tr.RunID, tr.TaskID)
	if err != nil {
		return &ServiceError{proto.FailInvalidRequest, 500, err.Error()}
	}
	out := compute.Execute(spec)
	if out.Failure != "" {
		return &ServiceError{proto.FailTaskFailed, 422,
			"task op " + string(spec.Op) + " cannot succeed: " + out.Failure}
	}

	// Build child transfer commands with unique identities BEFORE commit.
	type childInfo struct {
		transferID string
		tr         store.TransferRow
	}
	infos := make([]childInfo, 0, len(out.Children))
	childCmds := make([]kernel.Command, 0, len(out.Children)+1)
	for _, ch := range out.Children {
		if ch.ID == "" {
			return &ServiceError{proto.FailInvalidRequest, 400, "child task missing id"}
		}
		tid := s.newID("trf")
		infos = append(infos, childInfo{tid, store.TransferRow{
			ID: tid, RunID: tr.RunID, FromNode: nodeFor(tr.Partition),
			ToNode: nodeFor(ch.Partition), TaskID: ch.ID,
			Partition: ch.Partition, State: proto.EdgeOpen,
			ParentTask: tr.TaskID, Op: ch.Op, Spec: ch,
		}})
		childCmds = append(childCmds, kernel.Command{Open: &kernel.OpenTransfer{
			RunID: tr.RunID, TransferID: tid,
			Sender: nodeFor(tr.Partition), ReceiverNode: nodeFor(ch.Partition),
			TaskID: ch.ID, Partition: ch.Partition}})
	}
	childCmds = append(childCmds, kernel.Command{Succeed: &kernel.SucceedTask{
		RunID: tr.RunID, TransferID: tr.ID}})

	events, err := s.commit(ctx, tr.RunID,
		func(tx pgx.Tx) error {
			for _, ci := range infos {
				ci.tr.OpenedSeq = openSeqFor(events, ci.transferID)
				if err := store.InsertTransfer(ctx, tx, ci.tr); err != nil {
					return err
				}
			}
			// Settle this task edge according to its type.
			phase := s.phaseAfter(events)
			if err := s.st.MarkOutcome(ctx, tx, tr.ID, "succeeded", "", 0); err != nil {
				return err
			}
			if err := s.materializeSettles(ctx, tx, events); err != nil {
				return err
			}
			fc, msg := failureFrom(events)
			return s.st.MarkRun(ctx, tx, tr.RunID, phase, fc, msg,
				events[len(events)-1].Seq)
		}, childCmds...)
	if err != nil {
		return err
	}
	s.log.Info("task completed; children opened",
		"run_id", tr.RunID, "transfer_id", tr.ID, "task_id", tr.TaskID,
		"children", len(infos), "events", len(events),
		"phase", s.phaseAfter(events), "handler", "POST /complete")
	return nil
}

// Fail reports a deterministic task failure.
func (s *Service) Fail(ctx context.Context, req proto.FailRequest) error {
	tr, err := s.st.GetTransfer(ctx, req.TransferID)
	if err != nil {
		return &ServiceError{proto.FailUnknownTransfer, 404, req.TransferID}
	}
	reason := req.Reason
	if reason == "" {
		reason = "worker reported failure"
	}
	events, err := s.commit(ctx, tr.RunID,
		func(tx pgx.Tx) error {
			if err := s.st.MarkOutcome(ctx, tx, tr.ID, "failed", "", 0); err != nil {
				return err
			}
			if err := s.materializeSettles(ctx, tx, events); err != nil {
				return err
			}
			return s.st.MarkRun(ctx, tx, tr.RunID, proto.PhaseFailed,
				proto.FailTaskFailed, reason, events[len(events)-1].Seq)
		},
		kernel.Command{FailTask: &kernel.FailTask{
			RunID: tr.RunID, TransferID: tr.ID, Reason: reason}})
	if err != nil {
		return err
	}
	s.log.Error("task failed; run marked failed",
		"run_id", tr.RunID, "transfer_id", tr.ID, "reason", reason,
		"handler", "POST /fail", "failure_class", proto.FailTaskFailed)
	return nil
}

// Idle tells the server the worker/node is passive on a partition.
func (s *Service) Idle(ctx context.Context, req proto.IdleRequest) error {
	tr, err := s.st.GetRun(ctx, req.RunID)
	if err != nil {
		return &ServiceError{proto.FailUnknownRun, 404, req.RunID}
	}
	_ = tr
	node := nodeFor(req.Partition)
	// A node that never engaged (no task ever arrived) has nothing to
	// disengage: report success as a no-op rather than fail.
	events, err := s.commit(ctx, req.RunID,
		func(tx pgx.Tx) error {
			if err := s.materializeSettles(ctx, tx, events); err != nil {
				return err
			}
			fc, msg := failureFrom(events)
			return s.st.MarkRun(ctx, tx, req.RunID, s.phaseAfter(events),
				fc, msg, events[len(events)-1].Seq)
		},
		kernel.Command{GoIdle: &kernel.GoIdle{RunID: req.RunID, NodeID: node}})
	if err != nil {
		return err
	}
	_ = events
	return nil
}

// Ack applies an explicitly delivered DS signal (test/control surface).
func (s *Service) Ack(ctx context.Context, req proto.AckRequest) error {
	tr, err := s.st.GetTransfer(ctx, req.TransferID)
	if err != nil {
		return &ServiceError{proto.FailUnknownTransfer, 404, req.TransferID}
	}
	events, err := s.commit(ctx, tr.RunID,
		func(tx pgx.Tx) error {
			if err := s.materializeSettles(ctx, tx, events); err != nil {
				return err
			}
			fc, msg := failureFrom(events)
			return s.st.MarkRun(ctx, tx, tr.RunID, s.phaseAfter(events),
				fc, msg, events[len(events)-1].Seq)
		},
		kernel.Command{Signal: &kernel.ApplySignal{
			RunID: tr.RunID, TransferID: tr.ID, Kind: req.Signal}})
	if err != nil {
		return err
	}
	s.log.Info("signal applied", "run_id", tr.RunID,
		"transfer_id", tr.ID, "signal", req.Signal, "handler", "POST /ack")
	_ = events
	return nil
}
