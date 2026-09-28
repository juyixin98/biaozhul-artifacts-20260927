package plugins

import (
	"context"
	"errors"

	"admission/internal/model"
)

// ErrQuotaExhausted is the adapter-level signal that a reserve request is over
// the configured limit. The quota validator translates it into a
// resource_exhausted verdict (a denial, not an invocation error). Adapters
// return it directly or wrap it with fmt.Errorf("...: %w", ErrQuotaExhausted).
var ErrQuotaExhausted = errors.New("quota exhausted")

// QuotaService is the adapter port for the only external participant in the
// system: a per-kind capacity ledger. Two local implementations exist:
// storage.SQLQuota (the real, transactional SQLite ledger) and ScriptedQuota
// (deterministic fixture for timeout / failure tests).
//
// Reserve is UID-idempotent: reserving twice with the same UID reserves once
// (this is what makes re-entrant admission safe after a crash). CREATE counts
// the requested replicas; UPDATE counts the positive new-vs-old delta (a
// scale-down reserves nothing); DELETE releases the old amount.
type QuotaService interface {
	Reserve(ctx context.Context, uid, kind string, amount int64, op model.Operation) error
	// Release revokes a previously recorded hold when admission does not
	// commit. Releasing an unknown uid is a no-op.
	Release(ctx context.Context, uid string) error
}

// SignedDelta converts a request amount + operation into the ledger delta.
// Only positive deltas are reserved; a scale-down or delete is reconciled at
// commit time, not at admission time.
func SignedDelta(amount int64, op model.Operation) int64 {
	switch op {
	case model.OpCreate, model.OpUpdate:
		if amount < 0 {
			return 0
		}
		return amount
	default:
		return 0
	}
}
