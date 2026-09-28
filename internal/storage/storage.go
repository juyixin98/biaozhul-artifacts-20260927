// Package storage is the durable boundary (SQLite). It persists observation
// digests, plans with their binding, per-step recovery journal (idempotency
// keys, tokens, attempts, error categories) and an append-only evidence log.
// Recovery after interruption reads only these tables plus a fresh Read of the
// provider — never the in-memory state of a crashed process.
package storage

import (
	"database/sql"
	"encoding/json"
	"fmt"

	_ "modernc.org/sqlite"
)

// Store wraps a SQLite database.
type Store struct {
	db *sql.DB
}

// Open opens (creating the schema in) the database at dsn.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // avoid SQLITE_BUSY in the local single process
	if err := db.Ping(); err != nil {
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) migrate() error {
	_, err := s.db.Exec(schema)
	return err
}

const schema = `
CREATE TABLE IF NOT EXISTS observations (
  run         INTEGER PRIMARY KEY AUTOINCREMENT,
  digest      TEXT NOT NULL,
  payload     TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS plans (
  plan_id        TEXT PRIMARY KEY,
  run            INTEGER NOT NULL,
  status         TEXT NOT NULL,
  bound_digest   TEXT NOT NULL,
  desired_digest TEXT NOT NULL,
  payload        TEXT NOT NULL,
  desired        TEXT NOT NULL,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS step_states (
  plan_id    TEXT NOT NULL,
  step_order INTEGER NOT NULL,
  ref        TEXT NOT NULL,
  action     TEXT NOT NULL,
  status     TEXT NOT NULL,           -- pending|running|succeeded|failed|ambiguous
  attempts   INTEGER NOT NULL DEFAULT 0,
  idem_key   TEXT NOT NULL DEFAULT '',
  token      TEXT NOT NULL DEFAULT '',
  err_cat    TEXT NOT NULL DEFAULT '',
  err_code   TEXT NOT NULL DEFAULT '',
  err_msg    TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (plan_id, step_order)
);

CREATE TABLE IF NOT EXISTS evidence (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run        INTEGER NOT NULL DEFAULT 0,
  plan_id    TEXT NOT NULL DEFAULT '',
  step_order INTEGER NOT NULL DEFAULT -1,
  at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  kind       TEXT NOT NULL,          -- observe|plan|decision|action|result|error|recovery
  category   TEXT NOT NULL DEFAULT '',
  code       TEXT NOT NULL DEFAULT '',
  message    TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT ''
);
`

// NextObservationRun allocates the next monotonic observation run number.
func (s *Store) NextObservationRun() (int64, error) {
	res, err := s.db.Exec(`INSERT INTO observations (digest, payload) VALUES ('pending','{}')`)
	if err != nil {
		return 0, err
	}
	run, err := res.LastInsertId()
	if err != nil {
		return 0, err
	}
	return run, nil
}

// SaveObservation fills the digest/payload of the allocated run row.
func (s *Store) SaveObservation(run int64, digest, payloadJSON string) error {
	_, err := s.db.Exec(`UPDATE observations SET digest=?, payload=? WHERE run=?`,
		digest, payloadJSON, run)
	return err
}

// PlanRecord is a stored plan plus the desired input it was made from.
type PlanRecord struct {
	ID            string
	Run           int64
	Status        string
	BoundDigest   string
	DesiredDigest string
	Payload       string
	Desired       string
}

func (s *Store) SavePlan(rec PlanRecord) error {
	_, err := s.db.Exec(`INSERT INTO plans
(plan_id, run, status, bound_digest, desired_digest, payload, desired)
VALUES (?,?,?,?,?,?,?)
ON CONFLICT(plan_id) DO UPDATE SET
 status=excluded.status, payload=excluded.payload,
 updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')`,
		rec.ID, rec.Run, rec.Status, rec.BoundDigest, rec.DesiredDigest, rec.Payload, rec.Desired)
	return err
}

func (s *Store) SetPlanStatus(id, status string) error {
	_, err := s.db.Exec(`UPDATE plans SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE plan_id=?`,
		status, id)
	return err
}

func (s *Store) GetPlan(id string) (*PlanRecord, error) {
	row := s.db.QueryRow(`SELECT plan_id, run, status, bound_digest, desired_digest, payload, desired
FROM plans WHERE plan_id=?`, id)
	var rec PlanRecord
	err := row.Scan(&rec.ID, &rec.Run, &rec.Status, &rec.BoundDigest, &rec.DesiredDigest, &rec.Payload, &rec.Desired)
	if err == sql.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &rec, nil
}

// StepState is the persisted recovery state of one step.
type StepState struct {
	PlanID    string `json:"plan_id"`
	Order     int    `json:"step_order"`
	Ref       string `json:"ref"`
	Action    string `json:"action"`
	Status    string `json:"status"`
	Attempts  int    `json:"attempts"`
	IdemKey   string `json:"idem_key"`
	Token     string `json:"token"`
	ErrCat    string `json:"err_cat"`
	ErrCode   string `json:"err_code"`
	ErrMsg    string `json:"err_msg"`
}

// EnsureStep inserts a pending row if absent.
func (s *Store) EnsureStep(st StepState) error {
	_, err := s.db.Exec(`INSERT INTO step_states
(plan_id, step_order, ref, action, status, attempts, idem_key, token)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(plan_id, step_order) DO NOTHING`,
		st.PlanID, st.Order, st.Ref, st.Action, st.Status, st.Attempts, st.IdemKey, st.Token)
	return err
}

func (s *Store) GetStep(planID string, order int) (*StepState, error) {
	row := s.db.QueryRow(`SELECT plan_id, step_order, ref, action, status, attempts, idem_key, token, err_cat, err_code, err_msg
FROM step_states WHERE plan_id=? AND step_order=?`, planID, order)
	var st StepState
	err := row.Scan(&st.PlanID, &st.Order, &st.Ref, &st.Action, &st.Status, &st.Attempts,
		&st.IdemKey, &st.Token, &st.ErrCat, &st.ErrCode, &st.ErrMsg)
	if err == sql.ErrNoRows {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return &st, nil
}

// ListSteps returns all step rows in order.
func (s *Store) ListSteps(planID string) ([]StepState, error) {
	rows, err := s.db.Query(`SELECT plan_id, step_order, ref, action, status, attempts, idem_key, token, err_cat, err_code, err_msg
FROM step_states WHERE plan_id=? ORDER BY step_order`, planID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StepState
	for rows.Next() {
		var st StepState
		if err := rows.Scan(&st.PlanID, &st.Order, &st.Ref, &st.Action, &st.Status, &st.Attempts,
			&st.IdemKey, &st.Token, &st.ErrCat, &st.ErrCode, &st.ErrMsg); err != nil {
			return nil, err
		}
		out = append(out, st)
	}
	return out, rows.Err()
}

// UpdateStep persists new status / journal fields.
func (s *Store) UpdateStep(st StepState) error {
	_, err := s.db.Exec(`UPDATE step_states SET
 status=?, attempts=?, idem_key=?, token=?, err_cat=?, err_code=?, err_msg=?,
 updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE plan_id=? AND step_order=?`,
		st.Status, st.Attempts, st.IdemKey, st.Token, st.ErrCat, st.ErrCode, st.ErrMsg,
		st.PlanID, st.Order)
	return err
}

// EvidenceRow is one append-only evidence entry.
type EvidenceRow struct {
	ID       int64  `json:"id"`
	Run      int64  `json:"run"`
	PlanID   string `json:"plan_id"`
	Step     int    `json:"step"`
	At       string `json:"at"`
	Kind     string `json:"kind"`
	Category string `json:"category"`
	Code     string `json:"code"`
	Message  string `json:"message"`
	Detail   string `json:"detail"`
}

func (s *Store) AddEvidence(r EvidenceRow) error {
	_, err := s.db.Exec(`INSERT INTO evidence
(run, plan_id, step_order, kind, category, code, message, detail)
VALUES (?,?,?,?,?,?,?,?)`,
		r.Run, r.PlanID, r.Step, r.Kind, r.Category, r.Code, r.Message, r.Detail)
	return err
}

func (s *Store) ListEvidence(planID string) ([]EvidenceRow, error) {
	rows, err := s.db.Query(`SELECT id, run, plan_id, step_order, at, kind, category, code, message, detail
FROM evidence WHERE plan_id=? ORDER BY id`, planID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []EvidenceRow
	for rows.Next() {
		var r EvidenceRow
		if err := rows.Scan(&r.ID, &r.Run, &r.PlanID, &r.Step, &r.At, &r.Kind,
			&r.Category, &r.Code, &r.Message, &r.Detail); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// MustJSON is a small helper for evidence details.
func MustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "{}"
	}
	return string(b)
}

// ErrNotFound marks a missing persisted row.
var ErrNotFound = fmt.Errorf("storage: not found")
