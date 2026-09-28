package store

import (
	"context"
	"database/sql"
	"errors"

	"github.com/local/evictioncoordinator/internal/domain"
)

// TX is the transaction-scoped query surface handed to an
// WithImmediateTx callback. It is an alias of the internal query interface so
// callers outside the package never issue their own SQL; they only pass it
// straight back into the *On methods.
type TX = txIsh

// WithImmediateTx is the exported entry point for an atomic read/write
// transaction (see withImmediateTx for the locking rationale).
func (s *Store) WithImmediateTx(ctx context.Context, fn func(q TX) error) error {
	return s.withImmediateTx(ctx, fn)
}

// The *On methods are transaction-scoped variants of the standalone reads.

func (s *Store) GetGroupOn(ctx context.Context, q TX, namespace, name string) (domain.Group, error) {
	return s.getGroupOn(ctx, q, namespace, name)
}

func (s *Store) InstanceOn(ctx context.Context, q TX, id string) (domain.Instance, error) {
	var inst domain.Instance
	var labels string
	row := q.QueryRowContext(ctx,
		`SELECT id, namespace, group_name, labels FROM instances WHERE id=?`, id)
	err := row.Scan(&inst.ID, &inst.Namespace, &inst.Group, &labels)
	if errors.Is(err, sql.ErrNoRows) {
		return inst, ErrNotFound
	}
	if err != nil {
		return inst, err
	}
	inst.Labels = labelsFromJSON(labels)
	return inst, nil
}

func (s *Store) MemberInstancesOn(ctx context.Context, q TX, namespace, groupName string) ([]domain.Instance, error) {
	return s.memberInstancesOn(ctx, q, namespace, groupName)
}

func (s *Store) LatestObservationOn(ctx context.Context, q TX, instanceID string) (domain.Observation, bool, error) {
	return s.latestObservationOn(ctx, q, instanceID)
}

// FailureAtEpochOn reports an involuntary failure recorded at/after epoch.
func (s *Store) FailureAtEpochOn(ctx context.Context, q TX, instanceID string, epoch int64) (bool, string, error) {
	return s.HasFailureAtEpoch(ctx, q, instanceID, epoch)
}

// PendingApprovalsOn lists pending approvals for a group inside a tx.
func (s *Store) PendingApprovalsOn(ctx context.Context, q TX, namespace, groupName string) ([]domain.Approval, error) {
	return s.PendingApprovalsForGroup(ctx, q, namespace, groupName)
}

// ChargedApprovalsOn lists every reservation still consuming budget (pending +
// unreclaimed expired/stale) inside a tx.
func (s *Store) ChargedApprovalsOn(ctx context.Context, q TX, namespace, groupName string) ([]domain.Approval, error) {
	return s.ChargedApprovalsForGroup(ctx, q, namespace, groupName)
}
