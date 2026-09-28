// Package journal persists runs, their plans and the per-operation journal in
// SQLite. It is the durable record that lets an interrupted apply resume
// strictly by the real outcome rather than by optimistic assumptions.
package journal

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "modernc.org/sqlite"

	"infraplanner/internal/model"
)

// Store is the SQLite-backed journal.
type Store struct {
	db *sql.DB
}

// Open opens (and migrates) a journal file. Use ":memory:" for tests.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	// SQLite + Go: a single connection avoids "database is locked" and makes
	// journal writes strictly serial.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return &Store{db: db}, nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

const schema = `
CREATE TABLE IF NOT EXISTS runs (
	id            TEXT PRIMARY KEY,
	state         TEXT NOT NULL,
	fingerprint   TEXT NOT NULL,
	spec_json     TEXT NOT NULL,
	releases_json TEXT NOT NULL DEFAULT '[]',
	plan_json     TEXT NOT NULL DEFAULT '{}',
	created_at    TEXT NOT NULL,
	updated_at    TEXT NOT NULL,
	error_cat     TEXT NOT NULL DEFAULT '',
	error_code    TEXT NOT NULL DEFAULT '',
	error_msg     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS op_journal (
	run_id       TEXT NOT NULL,
	seq          INTEGER NOT NULL,
	type         TEXT NOT NULL,
	key_kind     TEXT NOT NULL,
	key_name     TEXT NOT NULL,
	existing_id  TEXT NOT NULL DEFAULT '',
	physical_id  TEXT NOT NULL DEFAULT '',
	state        TEXT NOT NULL,
	attempts     INTEGER NOT NULL DEFAULT 0,
	detail       TEXT NOT NULL DEFAULT '',
	updated_at   TEXT NOT NULL,
	error_cat    TEXT NOT NULL DEFAULT '',
	error_code   TEXT NOT NULL DEFAULT '',
	error_msg    TEXT NOT NULL DEFAULT '',
	PRIMARY KEY(run_id, seq)
);
CREATE TABLE IF NOT EXISTS evidence (
	run_id  TEXT NOT NULL,
	seq     INTEGER NOT NULL,
	attempt INTEGER NOT NULL,
	at      TEXT NOT NULL,
	kind    TEXT NOT NULL,
	body    TEXT NOT NULL,
	PRIMARY KEY(run_id, seq, attempt, kind)
);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
`

// Run is the persisted run row plus decoded plan.
type Run struct {
	ID          string
	State       model.RunState
	Fingerprint string
	Spec        []model.Desired
	Releases    []model.Key
	PlanJSON    []byte
	CreatedAt   time.Time
	UpdatedAt   time.Time
	Err         *model.Error
}

// OpRow is one persisted operation journal entry.
type OpRow struct {
	Seq        int
	Type       string
	Key        model.Key
	ExistingID string
	PhysicalID string
	State      model.OpState
	Attempts   int
	Detail     string
	UpdatedAt  time.Time
	Err        *model.Error
}

// Evidence is one recorded piece of operation evidence.
type Evidence struct {
	RunID   string    `json:"run_id"`
	Seq     int       `json:"seq"`
	Attempt int       `json:"attempt"`
	At      time.Time `json:"at"`
	Kind    string    `json:"kind"` // request|response|error|observe|decision
	Body    string    `json:"body"`
}

// CreateRun inserts a run in state planning with its spec and releases.
func (s *Store) CreateRun(ctx context.Context, id string, spec []model.Desired, releases []model.Key) error {
	specB, _ := json.Marshal(spec)
	relB, _ := json.Marshal(releases)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO runs(id,state,fingerprint,spec_json,releases_json,created_at,updated_at)
		 VALUES(?,?,?,?,?,?,?)`,
		id, string(model.RunPlanning), "", string(specB), string(relB), now, now)
	return err
}

// SetPlan stores the planned state, fingerprint and plan document.
func (s *Store) SetPlan(ctx context.Context, id, fingerprint string, planJSON []byte) error {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.ExecContext(ctx,
		`UPDATE runs SET state=?, fingerprint=?, plan_json=?, updated_at=? WHERE id=?`,
		string(model.RunPlanned), fingerprint, string(planJSON), now, id)
	return err
}

// SetRunState updates state and optional terminal error.
func (s *Store) SetRunState(ctx context.Context, id string, st model.RunState, errOut *model.Error) error {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	if errOut == nil {
		_, err := s.db.ExecContext(ctx,
			`UPDATE runs SET state=?, updated_at=?, error_cat='', error_code='', error_msg='' WHERE id=?`,
			string(st), now, id)
		return err
	}
	_, err := s.db.ExecContext(ctx,
		`UPDATE runs SET state=?, updated_at=?, error_cat=?, error_code=?, error_msg=? WHERE id=?`,
		string(st), now, errOut.Category, errOut.Code, errOut.Message, id)
	return err
}

// GetRun loads a run with decoded spec.
func (s *Store) GetRun(ctx context.Context, id string) (*Run, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT state,fingerprint,spec_json,releases_json,plan_json,created_at,updated_at,
		        error_cat,error_code,error_msg FROM runs WHERE id=?`, id)
	var st, fp, specB, relB, planB, created, updated, ecat, ecode, emsg string
	err := row.Scan(&st, &fp, &specB, &relB, &planB, &created, &updated, &ecat, &ecode, &emsg)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var specRes []model.Desired
	if err := json.Unmarshal([]byte(specB), &specRes); err != nil {
		return nil, err
	}
	var releases []model.Key
	if err := json.Unmarshal([]byte(relB), &releases); err != nil {
		return nil, err
	}
	r := &Run{
		ID: id, State: model.RunState(st), Fingerprint: fp, Spec: specRes,
		Releases: releases, PlanJSON: []byte(planB),
	}
	r.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	r.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	if ecat != "" {
		r.Err = &model.Error{Category: ecat, Code: ecode, Message: emsg}
	}
	return r, nil
}

// ListRuns returns run ids in reverse creation order.
func (s *Store) ListRuns(ctx context.Context, limit int) ([]*Run, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id FROM runs ORDER BY created_at DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	out := make([]*Run, 0, len(ids))
	for _, id := range ids {
		r, err := s.GetRun(ctx, id)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, nil
}

// UpsertOp writes an operation row.
func (s *Store) UpsertOp(ctx context.Context, runID string, o OpRow) error {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	ecat, ecode, emsg := "", "", ""
	if o.Err != nil {
		ecat, ecode, emsg = o.Err.Category, o.Err.Code, o.Err.Message
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO op_journal(run_id,seq,type,key_kind,key_name,existing_id,physical_id,state,attempts,detail,updated_at,error_cat,error_code,error_msg)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		 ON CONFLICT(run_id,seq) DO UPDATE SET
		   type=excluded.type, key_kind=excluded.key_kind, key_name=excluded.key_name,
		   existing_id=excluded.existing_id, physical_id=excluded.physical_id,
		   state=excluded.state, attempts=excluded.attempts, detail=excluded.detail,
		   updated_at=excluded.updated_at, error_cat=excluded.error_cat,
		   error_code=excluded.error_code, error_msg=excluded.error_msg`,
		runID, o.Seq, o.Type, string(o.Key.Kind), o.Key.Name, o.ExistingID,
		o.PhysicalID, string(o.State), o.Attempts, o.Detail, now, ecat, ecode, emsg)
	return err
}

// GetOp loads one operation row.
func (s *Store) GetOp(ctx context.Context, runID string, seq int) (*OpRow, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT seq,type,key_kind,key_name,existing_id,physical_id,state,attempts,detail,updated_at,error_cat,error_code,error_msg
		 FROM op_journal WHERE run_id=? AND seq=?`, runID, seq)
	return scanOp(row)
}

// ListOps returns all operations of a run in sequence order.
func (s *Store) ListOps(ctx context.Context, runID string) ([]*OpRow, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT seq,type,key_kind,key_name,existing_id,physical_id,state,attempts,detail,updated_at,error_cat,error_code,error_msg
		 FROM op_journal WHERE run_id=? ORDER BY seq`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*OpRow
	for rows.Next() {
		o, err := scanOp(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, nil
}

// CountOpsInState returns how many ops are in the given states.
func (s *Store) CountOpsInState(ctx context.Context, runID string, states ...model.OpState) (map[model.OpState]int, error) {
	out := map[model.OpState]int{}
	if len(states) == 0 {
		return out, nil
	}
	q := `SELECT state, COUNT(*) FROM op_journal WHERE run_id=? AND state IN (`
	args := []any{runID}
	for i, st := range states {
		if i > 0 {
			q += ","
		}
		q += "?"
		args = append(args, string(st))
	}
	q += ") GROUP BY state"
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var st string
		var n int
		if err := rows.Scan(&st, &n); err != nil {
			return nil, err
		}
		out[model.OpState(st)] = n
	}
	return out, nil
}

// AddEvidence appends an evidence record.
func (s *Store) AddEvidence(ctx context.Context, e Evidence) error {
	if e.At.IsZero() {
		e.At = time.Now().UTC()
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT OR REPLACE INTO evidence(run_id,seq,attempt,at,kind,body)
		 VALUES(?,?,?,?,?,?)`,
		e.RunID, e.Seq, e.Attempt, e.At.Format(time.RFC3339Nano), e.Kind, e.Body)
	return err
}

// ListEvidence returns evidence for a run ordered by time.
func (s *Store) ListEvidence(ctx context.Context, runID string) ([]Evidence, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT seq,attempt,at,kind,body FROM evidence WHERE run_id=? ORDER BY at, seq, attempt`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Evidence
	for rows.Next() {
		var e Evidence
		var at string
		e.RunID = runID
		if err := rows.Scan(&e.Seq, &e.Attempt, &at, &e.Kind, &e.Body); err != nil {
			return nil, err
		}
		e.At, _ = time.Parse(time.RFC3339Nano, at)
		out = append(out, e)
	}
	return out, nil
}

// rowScanner abstracts *sql.Row / *sql.Rows.
type rowScanner interface {
	Scan(dest ...any) error
}

func scanOp(r rowScanner) (*OpRow, error) {
	var o OpRow
	var kk, kn, st, updated, ecat, ecode, emsg string
	err := r.Scan(&o.Seq, &o.Type, &kk, &kn, &o.ExistingID, &o.PhysicalID,
		&st, &o.Attempts, &o.Detail, &updated, &ecat, &ecode, &emsg)
	if err != nil {
		return nil, err
	}
	o.Key = model.Key{Kind: model.Kind(kk), Name: kn}
	o.State = model.OpState(st)
	o.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	if ecat != "" {
		o.Err = &model.Error{Category: ecat, Code: ecode, Message: emsg}
	}
	return &o, nil
}
