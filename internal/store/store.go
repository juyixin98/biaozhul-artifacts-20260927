// Package store is the SQLite-backed persistence adapter for the cluster
// inventory (nodes, instances, policy) and the durable run/event log.
//
// The scheduler package is stateless: the reconcile loop builds a
// model.PlanRequest from a Snapshot, calls scheduler.Plan and commits the
// returned decisions transactionally.
package store

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"placer/internal/model"

	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schemaSQL string

// Store wraps one SQLite database. A single write mutex serializes
// transactions so the reconcile loop and HTTP handlers cannot interleave
// partial updates; reads use the same connection and are consistent within
// a snapshot read.
type Store struct {
	db *sql.DB
	mu sync.Mutex
}

// Open opens (creating if needed) the database at dsn and applies the
// schema. Use ":memory:" for tests.
func Open(ctx context.Context, dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", dsn, err)
	}
	// modernc.org/sqlite accepts these pragmas via the DSN too, but setting
	// them on the connection keeps tests on :memory: working identically.
	if _, err := db.ExecContext(ctx,
		"PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;"); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("sqlite pragmas: %w", err)
	}
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return &Store{db: db}, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// Snapshot is one consistent read of the scheduling world.
type Snapshot struct {
	Nodes     []model.Node
	Pending   []model.Instance
	Bound     []model.Binding
	Instances []model.Instance // all instances, including failed/evicted
	Policy    model.Policy
}

// LoadSnapshot reads the whole world under one serialized read.
func (s *Store) LoadSnapshot(ctx context.Context) (Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Snapshot{}, err
	}
	defer func() { _ = tx.Rollback() }()

	snap := Snapshot{Policy: model.Policy{Groups: []model.GroupRule{}}}

	rows, err := tx.QueryContext(ctx,
		`SELECT id, region, zone, status, capacity, labels, taints FROM nodes ORDER BY id`)
	if err != nil {
		return Snapshot{}, err
	}
	for rows.Next() {
		var n model.Node
		var capJSON, labelsJSON, taintsJSON string
		if err := rows.Scan(&n.ID, &n.Region, &n.Zone, &n.Status, &capJSON, &labelsJSON, &taintsJSON); err != nil {
			_ = rows.Close()
			return Snapshot{}, err
		}
		if err := json.Unmarshal([]byte(capJSON), &n.Capacity); err != nil {
			return Snapshot{}, fmt.Errorf("node %s capacity: %w", n.ID, err)
		}
		if err := json.Unmarshal([]byte(labelsJSON), &n.Labels); err != nil {
			return Snapshot{}, fmt.Errorf("node %s labels: %w", n.ID, err)
		}
		if err := json.Unmarshal([]byte(taintsJSON), &n.Taints); err != nil {
			return Snapshot{}, fmt.Errorf("node %s taints: %w", n.ID, err)
		}
		snap.Nodes = append(snap.Nodes, n)
	}
	if err := rows.Err(); err != nil {
		return Snapshot{}, err
	}
	_ = rows.Close()

	irows, err := tx.QueryContext(ctx, `
		SELECT id, state, node_id, request, zone, selector, tolerations, groups_json,
		       attempts, last_code
		FROM instances ORDER BY id`)
	if err != nil {
		return Snapshot{}, err
	}
	for irows.Next() {
		in, err := scanInstance(irows)
		if err != nil {
			_ = irows.Close()
			return Snapshot{}, err
		}
		snap.Instances = append(snap.Instances, in)
		switch in.State {
		case model.StatePending:
			snap.Pending = append(snap.Pending, in)
		case model.StateBound:
			snap.Bound = append(snap.Bound, model.Binding{
				InstanceID: in.ID, NodeID: in.NodeID,
				Request: in.Request, Groups: in.Groups,
			})
		}
	}
	if err := irows.Err(); err != nil {
		return Snapshot{}, err
	}
	_ = irows.Close()

	var doc string
	switch err := tx.QueryRowContext(ctx, `SELECT doc FROM policy_doc WHERE id = 1`).Scan(&doc); {
	case errors.Is(err, sql.ErrNoRows):
		// empty policy stands
	case err != nil:
		return Snapshot{}, err
	default:
		if err := json.Unmarshal([]byte(doc), &snap.Policy); err != nil {
			return Snapshot{}, fmt.Errorf("policy doc: %w", err)
		}
	}
	return snap, nil
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanInstance(r rowScanner) (model.Instance, error) {
	var in model.Instance
	var reqJSON, zone, selJSON, tolJSON, groupsJSON, nodeID, state, lastCode string
	var attempts int
	if err := r.Scan(&in.ID, &state, &nodeID, &reqJSON, &zone, &selJSON, &tolJSON,
		&groupsJSON, &attempts, &lastCode); err != nil {
		return model.Instance{}, err
	}
	in.State = model.InstanceState(state)
	in.NodeID = nodeID
	in.Attempts = attempts
	in.LastCode = lastCode
	in.Zone = zone
	if err := json.Unmarshal([]byte(reqJSON), &in.Request); err != nil {
		return model.Instance{}, fmt.Errorf("instance %s request: %w", in.ID, err)
	}
	if err := unmarshalNonEmpty(selJSON, &in.NodeSelector); err != nil {
		return model.Instance{}, fmt.Errorf("instance %s selector: %w", in.ID, err)
	}
	if err := json.Unmarshal([]byte(tolJSON), &in.Tolerations); err != nil {
		return model.Instance{}, fmt.Errorf("instance %s tolerations: %w", in.ID, err)
	}
	if err := unmarshalNonEmpty(groupsJSON, &in.Groups); err != nil {
		return model.Instance{}, fmt.Errorf("instance %s groups: %w", in.ID, err)
	}
	return in, nil
}

func unmarshalNonEmpty(s string, v any) error {
	if s == "" || s == "{}" || s == "[]" {
		return nil
	}
	return json.Unmarshal([]byte(s), v)
}

// CommitBindings atomically binds instance->node pairs and appends run
// events. Decisions not matching a currently pending instance fail the
// whole transaction (optimistic concurrency against the snapshot).
func (s *Store) CommitBindings(ctx context.Context, runID string, decisions []model.Decision) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.withTx(ctx, func(tx *sql.Tx) error {
		now := time.Now().UTC().Format(time.RFC3339Nano)
		for _, d := range decisions {
			res, err := tx.ExecContext(ctx, `
				UPDATE instances
				   SET state = ?, node_id = ?, attempts = attempts + 1,
				       last_code = '', updated_at = ?
				 WHERE id = ? AND state = ?`,
				string(model.StateBound), d.NodeID, now, d.InstanceID, string(model.StatePending))
			if err != nil {
				return err
			}
			n, err := res.RowsAffected()
			if err != nil {
				return err
			}
			if n != 1 {
				return fmt.Errorf("commit conflict: instance %q is no longer pending", d.InstanceID)
			}
		}
		return s.appendEventLocked(ctx, tx, runID, "bindings_committed",
			map[string]any{"decisions": decisions})
	})
}

// MarkFailed records a scheduling failure on pending instances without
// binding them. attempts is incremented; the caller decides whether the
// state flips to failed (retry budget exhausted).
func (s *Store) MarkFailed(ctx context.Context, runID string, conflicts []model.Conflict, markFailedState bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.withTx(ctx, func(tx *sql.Tx) error {
		now := time.Now().UTC().Format(time.RFC3339Nano)
		newState := string(model.StatePending)
		if markFailedState {
			newState = string(model.StateFailed)
		}
		for _, c := range conflicts {
			res, err := tx.ExecContext(ctx, `
				UPDATE instances
				   SET state = CASE WHEN ? THEN ? ELSE state END,
				       attempts = attempts + 1, last_code = ?, updated_at = ?
				 WHERE id = ? AND state IN (?, ?)`,
				markFailedState, newState, string(c.Code), now, c.InstanceID,
				string(model.StatePending), string(model.StateFailed))
			if err != nil {
				return err
			}
			n, err := res.RowsAffected()
			if err != nil {
				return err
			}
			if n != 1 {
				return fmt.Errorf("mark failed: instance %q disappeared or is bound", c.InstanceID)
			}
		}
		return s.appendEventLocked(ctx, tx, runID, "conflicts_recorded",
			map[string]any{"conflicts": conflicts, "terminal": markFailedState})
	})
}

// Attempts returns the attempt counter for an instance.
func (s *Store) Attempts(ctx context.Context, id string) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var n int
	err := s.db.QueryRowContext(ctx, `SELECT attempts FROM instances WHERE id = ?`, id).Scan(&n)
	return n, err
}

// withTx runs fn inside a transaction, rolling back on error.
func (s *Store) withTx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

func (s *Store) appendEventLocked(ctx context.Context, tx *sql.Tx, runID, kind string, payload any) error {
	var seq int
	if err := tx.QueryRowContext(ctx,
		`SELECT COALESCE(MAX(seq)+1, 0) FROM events WHERE run_id = ?`, runID).Scan(&seq); err != nil {
		return err
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO events(run_id, seq, at, kind, payload) VALUES (?,?,?,?,?)`,
		runID, seq, time.Now().UTC().Format(time.RFC3339Nano), kind, string(body))
	return err
}

// SaveRun persists a plan/replace run and its result and returns with all
// events appended. Unknown/error outcomes are stored with status "error",
// never silently as success.
func (s *Store) SaveRun(ctx context.Context, runID, kind, status string, request, result any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	reqJSON, _ := json.Marshal(request)
	resJSON, _ := json.Marshal(result)
	return s.withTx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO runs(run_id, kind, status, request, result, created_at)
			VALUES (?,?,?,?,?,?)
			ON CONFLICT(run_id) DO UPDATE SET
			  status=excluded.status, request=excluded.request, result=excluded.result`,
			runID, kind, status, string(reqJSON), string(resJSON),
			time.Now().UTC().Format(time.RFC3339Nano))
		return err
	})
}

// RunStatus reads a stored run status.
func (s *Store) RunStatus(ctx context.Context, runID string) (kind, status string, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	err = s.db.QueryRowContext(ctx,
		`SELECT kind, status FROM runs WHERE run_id = ?`, runID).Scan(&kind, &status)
	return kind, status, err
}

// Events returns the durable event stream for one run in sequence order.
func (s *Store) Events(ctx context.Context, runID string) ([]map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.QueryContext(ctx,
		`SELECT seq, kind, payload FROM events WHERE run_id = ? ORDER BY seq`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var seq int
		var kind, payload string
		if err := rows.Scan(&seq, &kind, &payload); err != nil {
			return nil, err
		}
		var body any
		if err := json.Unmarshal([]byte(payload), &body); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{"seq": seq, "kind": kind, "payload": body})
	}
	return out, rows.Err()
}
