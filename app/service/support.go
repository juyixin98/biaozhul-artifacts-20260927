package service

import (
	"context"

	"dsnet/proto"
	"dsnet/store"

	"github.com/jackc/pgx/v5"
)

// seqOf returns the sequence of the first decided event of the given kind.
func seqOf(events []proto.Event, kind proto.EventKind) int64 {
	for _, ev := range events {
		if ev.Kind == kind {
			return ev.Seq
		}
	}
	return 0
}

// openSeqFor returns the transfer.opened seq for a specific transfer.
func openSeqFor(events []proto.Event, transferID string) int64 {
	for _, ev := range events {
		if ev.Kind == proto.EvTransferOpen && ev.TransferID == transferID {
			return ev.Seq
		}
	}
	return 0
}

// phaseAfter derives the run phase from the last terminal-marker event in a
// decided batch (falling back to running).
func phaseAfter(events []proto.Event) proto.RunPhase {
	phase := proto.PhaseRunning
	for _, ev := range events {
		switch ev.Kind {
		case proto.EvDSAnnounce:
			phase = proto.PhaseAnnounced
		case proto.EvRunFailed:
			phase = proto.PhaseFailed
		case proto.EvBudgetExpired:
			phase = proto.PhaseUnacknowledged
		}
	}
	return phase
}

// failureFrom extracts the failure class/message implied by the batch.
func failureFrom(events []proto.Event) (proto.FailureClass, string) {
	for i := len(events) - 1; i >= 0; i-- {
		ev := events[i]
		switch ev.Kind {
		case proto.EvRunFailed:
			return proto.FailTaskFailed, ev.Reason
		case proto.EvBudgetExpired:
			return proto.FailBudgetExceeded, ev.Reason
		}
	}
	return "", ""
}

// materializeSettles applies every edge.settled event in the batch to the
// materialized transfers table, distinguishing normal settle from the budget
// unacknowledged path.
func (s *Service) materializeSettles(ctx context.Context, tx pgx.Tx, events []proto.Event) error {
	for _, ev := range events {
		if ev.Kind != proto.EvEdgeSettled {
			continue
		}
		if ev.Reason == "budget:unacknowledged" {
			if err := s.st.MarkUnacked(ctx, tx, ev.TransferID, ev.Seq); err != nil {
				return err
			}
			continue
		}
		if err := store.SettleEdge(ctx, tx, ev.TransferID, ev.Seq); err != nil {
			return err
		}
	}
	return nil
}
