// Package store is the SQLite-backed persistence layer. It owns the schema and
// provides the transactional primitives the controller needs. The store does
// not encode rollout policy; it only stores and retrieves resource state.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	_ "modernc.org/sqlite"

	"rollctl/internal/model"
)

// ErrNotFound is returned for missing rows.
var ErrNotFound = errors.New("store: not found")

// Store wraps a SQLite database. All access is serialized with a single mutex:
// SQLite in WAL mode with one writer keeps the audit log and resource tables
// mutually consistent without an external orchestrator.
type Store struct {
	mu  sync.RWMutex
	db  *sql.DB
	dsn string
}

// Open opens (creating if needed) the database at path and applies migrations.
func Open(ctx context.Context, path string) (*Store, error) {
	// _pragma settings: WAL, busy timeout, foreign keys on.
	dsn := fmt.Sprintf("file:%s?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_time_format=sqlite", path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &Store{db: db, dsn: dsn}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close closes the underlying database.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS workloads (
			name TEXT PRIMARY KEY,
			replicas INTEGER NOT NULL,
			current_revision TEXT NOT NULL,
			current_release_id TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS releases (
			id TEXT PRIMARY KEY,
			workload TEXT NOT NULL,
			revision TEXT NOT NULL,
			previous_release_id TEXT NOT NULL DEFAULT '',
			state TEXT NOT NULL,
			kind TEXT NOT NULL,
			policy_json TEXT NOT NULL,
			failure_category TEXT NOT NULL DEFAULT '',
			fail_message TEXT NOT NULL DEFAULT '',
			rollback_of TEXT NOT NULL DEFAULT '',
			request_id TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL,
			started_tick INTEGER NOT NULL DEFAULT 0,
			finished_tick INTEGER NOT NULL DEFAULT 0,
			last_progress_tick INTEGER NOT NULL DEFAULT 0,
			saw_flap INTEGER NOT NULL DEFAULT 0,
			saw_capacity_block INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS instances (
			id TEXT PRIMARY KEY,
			workload TEXT NOT NULL,
			revision TEXT NOT NULL,
			release_id TEXT NOT NULL,
			proc_id TEXT NOT NULL DEFAULT '',
			state TEXT NOT NULL,
			ready_streak INTEGER NOT NULL DEFAULT 0,
			created_tick INTEGER NOT NULL,
			ready_tick INTEGER NOT NULL DEFAULT 0,
			failed_tick INTEGER NOT NULL DEFAULT 0,
			terminated_tick INTEGER NOT NULL DEFAULT 0,
			fail_category TEXT NOT NULL DEFAULT '',
			fail_message TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS events (
			seq INTEGER PRIMARY KEY AUTOINCREMENT,
			tick INTEGER NOT NULL,
			ts TEXT NOT NULL,
			request_id TEXT NOT NULL DEFAULT '',
			workload TEXT NOT NULL,
			release_id TEXT NOT NULL DEFAULT '',
			instance_id TEXT NOT NULL DEFAULT '',
			revision TEXT NOT NULL DEFAULT '',
			level TEXT NOT NULL,
			type TEXT NOT NULL,
			category TEXT NOT NULL DEFAULT '',
			certain INTEGER NOT NULL DEFAULT 1,
			message TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE INDEX IF NOT EXISTS idx_instances_workload ON instances(workload)`,
		`CREATE INDEX IF NOT EXISTS idx_releases_workload ON releases(workload)`,
		`CREATE INDEX IF NOT EXISTS idx_events_workload ON events(workload, seq)`,
		`CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS sim_processes (
			proc_id TEXT PRIMARY KEY,
			workload TEXT NOT NULL,
			revision TEXT NOT NULL,
			release_id TEXT NOT NULL,
			started_tick INTEGER NOT NULL,
			last_tick INTEGER NOT NULL,
			exited INTEGER NOT NULL DEFAULT 0,
			exit_tick INTEGER NOT NULL DEFAULT 0,
			fail_kind TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE TABLE IF NOT EXISTS sim_behaviors (
			workload TEXT NOT NULL,
			revision TEXT NOT NULL,
			payload TEXT NOT NULL,
			PRIMARY KEY(workload, revision)
		)`,
	}
	for _, ddl := range stmts {
		if _, err := s.db.ExecContext(ctx, ddl); err != nil {
			return fmt.Errorf("migrate: %w: %s", err, ddl)
		}
	}
	return nil
}

// tx runs fn inside a serialized write transaction.
func (s *Store) tx(ctx context.Context, fn func(*sql.Tx) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
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

// ---------------------------------------------------------------- workloads

// CreateWorkload inserts a workload plus its bootstrap release and an event,
// atomically.
func (s *Store) CreateWorkload(ctx context.Context, w *model.Workload, bootstrap *model.Release, requestID string) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		var exists int
		if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM workloads WHERE name=?`, w.Name).Scan(&exists); err != nil {
			return err
		}
		if exists != 0 {
			return fmt.Errorf("workload %q already exists", w.Name)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO workloads(name,replicas,current_revision,current_release_id,created_at) VALUES(?,?,?,?,?)`,
			w.Name, w.Replicas, w.CurrentRevision, w.CurrentReleaseID, w.CreatedAt.UTC().Format(time.RFC3339Nano)); err != nil {
			return err
		}
		return insertRelease(tx, bootstrap)
	})
}

// GetWorkload loads one workload.
func (s *Store) GetWorkload(ctx context.Context, name string) (*model.Workload, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	row := s.db.QueryRowContext(ctx,
		`SELECT name,replicas,current_revision,current_release_id,created_at FROM workloads WHERE name=?`, name)
	w := &model.Workload{}
	var created string
	if err := row.Scan(&w.Name, &w.Replicas, &w.CurrentRevision, &w.CurrentReleaseID, &created); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	w.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	return w, nil
}

// ListWorkloads lists all workload names.
func (s *Store) ListWorkloads(ctx context.Context) ([]string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, `SELECT name FROM workloads ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, rows.Err()
}

// SetWorkloadCurrent points a workload at a finished release's revision.
func (s *Store) SetWorkloadCurrent(ctx context.Context, name, revision, releaseID string) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx,
			`UPDATE workloads SET current_revision=?, current_release_id=? WHERE name=?`,
			revision, releaseID, name)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			return ErrNotFound
		}
		return nil
	})
}

// ---------------------------------------------------------------- releases

func insertRelease(tx *sql.Tx, r *model.Release) error {
	pol, _ := json.Marshal(r.Policy)
	_, err := tx.Exec(
		`INSERT INTO releases(id,workload,revision,previous_release_id,state,kind,policy_json,failure_category,fail_message,rollback_of,request_id,created_at,started_tick,finished_tick,last_progress_tick,saw_flap,saw_capacity_block)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		r.ID, r.Workload, r.Revision, r.PreviousReleaseID, string(r.State), string(r.Kind),
		string(pol), string(r.FailureCategory), r.FailMessage, r.RollbackOf, r.RequestID,
		r.CreatedAt.UTC().Format(time.RFC3339Nano), r.StartedTick, r.FinishedTick, r.LastProgressTick,
		boolInt(r.SawFlap), boolInt(r.SawCapacityBlock))
	return err
}

// InsertRelease persists a new release row.
func (s *Store) InsertRelease(ctx context.Context, r *model.Release) error {
	return s.tx(ctx, func(tx *sql.Tx) error { return insertRelease(tx, r) })
}

func scanRelease(row interface {
	Scan(dest ...any) error
}) (*model.Release, error) {
	r := &model.Release{}
	var state, kind, pol, cat, msg, rollbackOf, reqID, created string
	var flap, block int
	if err := row.Scan(&r.ID, &r.Workload, &r.Revision, &r.PreviousReleaseID, &state, &kind, &pol,
		&cat, &msg, &rollbackOf, &reqID, &created, &r.StartedTick, &r.FinishedTick,
		&r.LastProgressTick, &flap, &block); err != nil {
		return nil, err
	}
	r.State = model.ReleaseState(state)
	r.Kind = model.ReleaseKind(kind)
	r.FailureCategory = model.FailureCategory(cat)
	r.FailMessage = msg
	r.RollbackOf = rollbackOf
	r.RequestID = reqID
	r.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	r.SawFlap = flap != 0
	r.SawCapacityBlock = block != 0
	if err := json.Unmarshal([]byte(pol), &r.Policy); err != nil {
		return nil, fmt.Errorf("release %s policy: %w", r.ID, err)
	}
	return r, nil
}

const releaseCols = `id,workload,revision,previous_release_id,state,kind,policy_json,failure_category,fail_message,rollback_of,request_id,created_at,started_tick,finished_tick,last_progress_tick,saw_flap,saw_capacity_block`

// GetRelease loads one release.
func (s *Store) GetRelease(ctx context.Context, id string) (*model.Release, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	row := s.db.QueryRowContext(ctx, `SELECT `+releaseCols+` FROM releases WHERE id=?`, id)
	r, err := scanRelease(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return r, err
}

// ActiveRelease returns the in-flight release of a workload, if any.
func (s *Store) ActiveRelease(ctx context.Context, workload string) (*model.Release, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	row := s.db.QueryRowContext(ctx, `SELECT `+releaseCols+` FROM releases WHERE workload=? AND state=? ORDER BY created_at DESC LIMIT 1`,
		workload, string(model.RelActive))
	r, err := scanRelease(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

// PendingOrActiveRelease returns a pending release if one exists, else the
// active one.
func (s *Store) PendingOrActiveRelease(ctx context.Context, workload string) (*model.Release, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	row := s.db.QueryRowContext(ctx, `SELECT `+releaseCols+` FROM releases WHERE workload=? AND state IN (?,?) ORDER BY created_at DESC LIMIT 1`,
		workload, string(model.RelPending), string(model.RelActive))
	r, err := scanRelease(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

// LatestFinishedRelease returns the most recent succeeded/failed release.
func (s *Store) LatestFinishedRelease(ctx context.Context, workload string) (*model.Release, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	row := s.db.QueryRowContext(ctx, `SELECT `+releaseCols+` FROM releases WHERE workload=? AND state IN (?,?) ORDER BY created_at DESC LIMIT 1`,
		workload, string(model.RelSucceeded), string(model.RelFailed))
	r, err := scanRelease(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

// ListReleases returns the full release history of a workload, newest first.
func (s *Store) ListReleases(ctx context.Context, workload string) ([]*model.Release, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, `SELECT `+releaseCols+` FROM releases WHERE workload=? ORDER BY created_at DESC`, workload)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Release
	for rows.Next() {
		r, err := scanRelease(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// UpdateRelease persists mutable release fields.
func (s *Store) UpdateRelease(ctx context.Context, r *model.Release) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx,
			`UPDATE releases SET state=?,failure_category=?,fail_message=?,started_tick=?,finished_tick=?,last_progress_tick=?,saw_flap=?,saw_capacity_block=? WHERE id=?`,
			string(r.State), string(r.FailureCategory), r.FailMessage,
			r.StartedTick, r.FinishedTick, r.LastProgressTick, boolInt(r.SawFlap), boolInt(r.SawCapacityBlock), r.ID)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			return ErrNotFound
		}
		return nil
	})
}

// ---------------------------------------------------------------- instances

// InsertInstance inserts an instance row.
func (s *Store) InsertInstance(ctx context.Context, ins *model.Instance) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			`INSERT INTO instances(id,workload,revision,release_id,proc_id,state,ready_streak,created_tick,ready_tick,failed_tick,terminated_tick,fail_category,fail_message,created_at,updated_at)
			 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			ins.ID, ins.Workload, ins.Revision, ins.ReleaseID, ins.ProcID, string(ins.State),
			ins.ReadyStreak, ins.CreatedTick, ins.ReadyTick, ins.FailedTick, ins.TerminatedTick,
			string(ins.FailCategory), ins.FailMessage,
			ins.CreatedAt.UTC().Format(time.RFC3339Nano), ins.UpdatedAt.UTC().Format(time.RFC3339Nano))
		return err
	})
}

// UpdateInstance persists instance state changes.
func (s *Store) UpdateInstance(ctx context.Context, ins *model.Instance) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx,
			`UPDATE instances SET proc_id=?,state=?,ready_streak=?,ready_tick=?,failed_tick=?,terminated_tick=?,fail_category=?,fail_message=?,updated_at=? WHERE id=?`,
			ins.ProcID, string(ins.State), ins.ReadyStreak, ins.ReadyTick, ins.FailedTick,
			ins.TerminatedTick, string(ins.FailCategory), ins.FailMessage,
			ins.UpdatedAt.UTC().Format(time.RFC3339Nano), ins.ID)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			return ErrNotFound
		}
		return nil
	})
}

// ListInstances returns all instance rows for a workload including tombstones.
func (s *Store) ListInstances(ctx context.Context, workload string) ([]*model.Instance, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx,
		`SELECT id,workload,revision,release_id,proc_id,state,ready_streak,created_tick,ready_tick,failed_tick,terminated_tick,fail_category,fail_message,created_at,updated_at
		 FROM instances WHERE workload=? ORDER BY created_at, id`, workload)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Instance
	for rows.Next() {
		ins, err := scanInstance(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, ins)
	}
	return out, rows.Err()
}

// GetInstance loads a single instance.
func (s *Store) GetInstance(ctx context.Context, id string) (*model.Instance, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	row := s.db.QueryRowContext(ctx,
		`SELECT id,workload,revision,release_id,proc_id,state,ready_streak,created_tick,ready_tick,failed_tick,terminated_tick,fail_category,fail_message,created_at,updated_at
		 FROM instances WHERE id=?`, id)
	ins, err := scanInstance(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return ins, err
}

func scanInstance(row interface {
	Scan(dest ...any) error
}) (*model.Instance, error) {
	i := &model.Instance{}
	var state, cat, msg, created, updated string
	if err := row.Scan(&i.ID, &i.Workload, &i.Revision, &i.ReleaseID, &i.ProcID, &state,
		&i.ReadyStreak, &i.CreatedTick, &i.ReadyTick, &i.FailedTick, &i.TerminatedTick,
		&cat, &msg, &created, &updated); err != nil {
		return nil, err
	}
	i.State = model.InstanceState(state)
	i.FailCategory = model.FailureCategory(cat)
	i.FailMessage = msg
	i.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	i.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	return i, nil
}

// ---------------------------------------------------------------- events

// AppendEvent inserts one event and returns its sequence number.
func (s *Store) AppendEvent(ctx context.Context, e *model.Event) (int64, error) {
	return e.Seq, s.tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx,
			`INSERT INTO events(tick,ts,request_id,workload,release_id,instance_id,revision,level,type,category,certain,message)
			 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,
			e.Tick, e.TS.UTC().Format(time.RFC3339Nano), e.RequestID, e.Workload, e.ReleaseID,
			e.InstanceID, e.Revision, string(e.Level), e.Type, e.Category, boolInt(e.Certain), e.Message)
		if err != nil {
			return err
		}
		e.Seq, _ = res.LastInsertId()
		return nil
	})
}

// EventsAfter returns events for a workload with sequence greater than afterSeq.
func (s *Store) EventsAfter(ctx context.Context, workload string, afterSeq int64) ([]*model.Event, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx,
		`SELECT seq,tick,ts,request_id,workload,release_id,instance_id,revision,level,type,category,certain,message
		 FROM events WHERE workload=? AND seq>? ORDER BY seq`, workload, afterSeq)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*model.Event
	for rows.Next() {
		e := &model.Event{}
		var ts, reqID, relID, instID, rev, level, typ, cat, msg string
		var certain int
		if err := rows.Scan(&e.Seq, &e.Tick, &ts, &reqID, &e.Workload, &relID, &instID, &rev,
			&level, &typ, &cat, &certain, &msg); err != nil {
			return nil, err
		}
		e.TS, _ = time.Parse(time.RFC3339Nano, ts)
		e.RequestID, e.ReleaseID, e.InstanceID, e.Revision = reqID, relID, instID, rev
		e.Level, e.Type, e.Category, e.Message = model.EventLevel(level), typ, cat, msg
		e.Certain = certain != 0
		out = append(out, e)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------- tick

// Tick returns the persisted logical controller tick.
func (s *Store) Tick(ctx context.Context) (int64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var v string
	err := s.db.QueryRowContext(ctx, `SELECT v FROM meta WHERE k='tick'`).Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	var t int64
	_, err = fmt.Sscanf(v, "%d", &t)
	return t, err
}

// SetTick persists the logical controller tick.
func (s *Store) SetTick(ctx context.Context, t int64) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `INSERT INTO meta(k,v) VALUES('tick',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v`, fmt.Sprintf("%d", t))
		return err
	})
}

// ---------------------------------------------------------------- sim tables

// SimProcRow is one persisted simulated process.
type SimProcRow struct {
	ProcID      string
	Workload    string
	Revision    string
	ReleaseID   string
	StartedTick int64
	LastTick    int64
	Exited      bool
	ExitTick    int64
	FailKind    string
}

// ListSimProcesses returns persisted simulated processes (used when the
// simulator is reattached after a controller restart).
func (s *Store) ListSimProcesses(ctx context.Context) ([]SimProcRow, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx,
		`SELECT proc_id,workload,revision,release_id,started_tick,last_tick,exited,exit_tick,fail_kind FROM sim_processes ORDER BY started_tick,proc_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []SimProcRow
	for rows.Next() {
		var r SimProcRow
		var exited int
		if err := rows.Scan(&r.ProcID, &r.Workload, &r.Revision, &r.ReleaseID, &r.StartedTick, &r.LastTick, &exited, &r.ExitTick, &r.FailKind); err != nil {
			return nil, err
		}
		r.Exited = exited != 0
		out = append(out, r)
	}
	return out, rows.Err()
}

// PutSimProcess upserts a simulated process row.
func (s *Store) PutSimProcess(ctx context.Context, r SimProcRow) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			`INSERT INTO sim_processes(proc_id,workload,revision,release_id,started_tick,last_tick,exited,exit_tick,fail_kind)
			 VALUES(?,?,?,?,?,?,?,?,?)
			 ON CONFLICT(proc_id) DO UPDATE SET last_tick=excluded.last_tick,exited=excluded.exited,exit_tick=excluded.exit_tick,fail_kind=excluded.fail_kind`,
			r.ProcID, r.Workload, r.Revision, r.ReleaseID, r.StartedTick, r.LastTick, boolInt(r.Exited), r.ExitTick, r.FailKind)
		return err
	})
}

// DeleteSimProcess removes a simulated process row.
func (s *Store) DeleteSimProcess(ctx context.Context, procID string) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `DELETE FROM sim_processes WHERE proc_id=?`, procID)
		return err
	})
}

// PutSimBehavior upserts the behavior JSON for a workload/revision.
func (s *Store) PutSimBehavior(ctx context.Context, workload, revision, payload string) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			`INSERT INTO sim_behaviors(workload,revision,payload) VALUES(?,?,?)
			 ON CONFLICT(workload,revision) DO UPDATE SET payload=excluded.payload`, workload, revision, payload)
		return err
	})
}

// GetSimBehavior loads the behavior JSON for a workload/revision.
func (s *Store) GetSimBehavior(ctx context.Context, workload, revision string) (string, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var payload string
	err := s.db.QueryRowContext(ctx, `SELECT payload FROM sim_behaviors WHERE workload=? AND revision=?`, workload, revision).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return payload, true, nil
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
