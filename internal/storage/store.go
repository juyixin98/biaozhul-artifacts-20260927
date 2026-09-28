// Package storage implements the SQLite persistence adapter for the
// resource model. The package is deliberately SQL-idiomatic and has no
// dependency on the reconcile algorithm: the controller drives it
// entirely through the Store interface, and tests can use
// AttachOwnerRefOut-of-band helpers to fabricate boundary states.
package storage

import (
	"context"
	"database/sql"
	_ "embed"
	"fmt"

	"lifecycle.local/v1/internal/model"
	"lifecycle.local/v1/internal/version"

	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schemaSQL string

// Store is the persistence boundary used by the application and
// reconciler layers. All methods are safe to call from one goroutine
// (the reconciler tick is serialized); the HTTP layer additionally
// serializes mutations through the app-level coordinator.
type Store interface {
	// RunInTx runs fn inside a single serializable write transaction.
	// A non-nil error rolls back; model.Error values propagate
	// unchanged. The reconciler runs an entire tick in one transaction
	// so each tick is atomic.
	RunInTx(ctx context.Context, fn func(Queryer) error) error
	// ReadTx offers a read-only transaction for queries.
	ReadTx(ctx context.Context, fn func(Queryer) error) error
	Close() error

	// Metadata / migration.
	MetaGet(ctx context.Context, key string) (string, bool, error)
	MetaSet(ctx context.Context, key, value string) error

	// Resources.
	GetResource(ctx context.Context, q Queryer, namespace, name string) (*model.Resource, error)
	GetResourceByUID(ctx context.Context, q Queryer, uid string) (*model.Resource, error)
	ListResources(ctx context.Context, q Queryer) ([]*model.Resource, error)
	InsertResource(ctx context.Context, q Queryer, r *model.Resource) error
	UpdateResource(ctx context.Context, q Queryer, r *model.Resource) error
	DeleteResource(ctx context.Context, q Queryer, uid string) error

	// Owner refs.
	AddOwnerRef(ctx context.Context, q Queryer, resourceUID string, ref model.OwnerRef, pos int) error
	RemoveOwnerRef(ctx context.Context, q Queryer, resourceUID, ownerUID string) error
	ListOwnerRefs(ctx context.Context, q Queryer, resourceUID string) ([]model.OwnerRef, error)
	// ListAllOwnerRefs returns every edge (used by the planner and by
	// the independent oracle tests); rows are (resourceUID, ref).
	ListAllOwnerRefs(ctx context.Context, q Queryer) (map[string][]model.OwnerRef, error)

	// Finalizers.
	AddFinalizer(ctx context.Context, q Queryer, resourceUID, finalizer string, pos int) error
	RemoveFinalizer(ctx context.Context, q Queryer, resourceUID, finalizer string) error
	ListFinalizers(ctx context.Context, q Queryer, resourceUID string) ([]string, error)

	// Conditions.
	SetCondition(ctx context.Context, q Queryer, resourceUID string, c model.Condition) error
	DeleteCondition(ctx context.Context, q Queryer, resourceUID, condType string) error
	ListConditions(ctx context.Context, q Queryer, resourceUID string) ([]model.Condition, error)
	DeleteConditions(ctx context.Context, q Queryer, resourceUID string) error

	// GC events.
	AppendEvent(ctx context.Context, q Queryer, e *model.GCEvent) error
	ListEvents(ctx context.Context, runID string, limit int) ([]model.GCEvent, error)
	ListEventsForTick(ctx context.Context, runID string, tick int64) ([]model.GCEvent, error)
	LastEventID(ctx context.Context, runID string) (int64, error)

	// Deleted owner tombstones.
	InsertDeletedOwner(ctx context.Context, q Queryer, d model.DeletedOwner) error
	GetDeletedOwner(ctx context.Context, q Queryer, uid string) (*model.DeletedOwner, error)
	ListDeletedOwners(ctx context.Context, q Queryer) ([]model.DeletedOwner, error)

	// Cycle marks.
	InsertCycleMark(ctx context.Context, q Queryer, m *model.CycleMark) error
	ListCycleMarks(ctx context.Context, q Queryer) ([]model.CycleMark, error)
}

// Queryer is satisfied by both *sql.DB and *sql.Tx.
type Queryer interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// sqliteStore is the concrete Store.
type sqliteStore struct {
	db *sql.DB
}

// Open opens (creating/migrating) a SQLite database at dsn. The pool is
// pinned to a single connection: the workload is a serialized
// reconciler plus low-volume HTTP calls, and a single connection gives
// deterministic transaction/locking behavior under modernc.org/sqlite.
func Open(ctx context.Context, dsn string) (Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, model.Errorf(model.ErrKindStorage, "open %q: %v", dsn, err)
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	s := &sqliteStore{db: db}
	if err := s.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *sqliteStore) Close() error { return s.db.Close() }

func (s *sqliteStore) migrate(ctx context.Context) error {
	if _, err := s.db.ExecContext(ctx, schemaSQL); err != nil {
		return model.Errorf(model.ErrKindStorage, "apply schema: %v", err)
	}
	got, ok, err := s.MetaGet(ctx, "schema_version")
	if err != nil {
		return err
	}
	if !ok {
		if err := s.MetaSet(ctx, "schema_version", fmt.Sprint(version.SchemaVersion)); err != nil {
			return err
		}
	} else if got != fmt.Sprint(version.SchemaVersion) {
		return model.Errorf(model.ErrKindStorage,
			"schema version mismatch: db=%s binary=%d", got, version.SchemaVersion)
	}
	return nil
}
