package storage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	_ "github.com/lib/pq"

	"example.com/cgcoord/protocol"
)

// PostgresStore implements Store on PostgreSQL.
//
// Schema:
//
//	groups(name PK, state JSONB, seq BIGINT)  -- seq is the last assigned
//	    event sequence (-1 before the first event);
//	events(group, seq, at, request_id, type, detail JSONB, PK(group,seq)).
//
// Coordinator transactions run at SERIALIZABLE: concurrent joins cannot
// produce two plans from the same read set, and the state blob plus journal
// append are committed together.
type PostgresStore struct {
	db *sql.DB
}

// NewPostgresStore connects, verifies connectivity and ensures the schema
// exists. The caller owns Close().
func NewPostgresStore(ctx context.Context, dsn string) (*PostgresStore, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	st := &PostgresStore{db: db}
	if err := st.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return st, nil
}

func (s *PostgresStore) migrate(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, `
CREATE TABLE IF NOT EXISTS cgcoord_groups (
    name  TEXT PRIMARY KEY,
    state JSONB NOT NULL,
    seq   BIGINT NOT NULL DEFAULT -1
);
CREATE TABLE IF NOT EXISTS cgcoord_events (
    group_name TEXT      NOT NULL REFERENCES cgcoord_groups(name),
    seq        BIGINT    NOT NULL,
    at         TIMESTAMPTZ NOT NULL,
    request_id TEXT      NOT NULL DEFAULT '',
    type       TEXT      NOT NULL,
    detail     JSONB     NOT NULL,
    PRIMARY KEY (group_name, seq)
);`)
	return err
}

// Close implements Store.
func (s *PostgresStore) Close() error { return s.db.Close() }

// ListGroups implements Store.
func (s *PostgresStore) ListGroups(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT name FROM cgcoord_groups ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var names []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		names = append(names, n)
	}
	return names, rows.Err()
}

// Begin implements Store with SERIALIZABLE isolation.
func (s *PostgresStore) Begin(ctx context.Context) (Tx, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return nil, err
	}
	return &pgTx{tx: tx}, nil
}

type pgTx struct {
	tx *sql.Tx
}

func (t *pgTx) CreateGroup(name string) error {
	_, err := t.tx.Exec(`INSERT INTO cgcoord_groups (name, state) VALUES ($1, '{}'::jsonb)`, name)
	if pgCode(err) == "23505" {
		return ErrGroupExists
	}
	return err
}

func (t *pgTx) SaveGroupState(state *protocol.GroupState) error {
	raw, err := json.Marshal(state)
	if err != nil {
		return err
	}
	res, err := t.tx.Exec(`UPDATE cgcoord_groups SET state = $2 WHERE name = $1`, state.Name, raw)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrNotFound
	}
	return nil
}

func (t *pgTx) AppendEvent(e protocol.Event) (protocol.Seq, error) {
	payload, err := protocol.MarshalEvent(e)
	if err != nil {
		return 0, err
	}
	// Keep the detail/type/at/request_id columns in sync with the wire JSON.
	var wire struct {
		At        json.RawMessage `json:"at"`
		Type      string          `json:"type"`
		RequestID string          `json:"request_id"`
		Detail    json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(payload, &wire); err != nil {
		return 0, err
	}
	var seq int64
	if err := t.tx.QueryRow(
		`UPDATE cgcoord_groups SET seq = seq + 1 WHERE name = $1 RETURNING seq`,
		e.Group,
	).Scan(&seq); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return 0, ErrNotFound
		}
		return 0, err
	}
	if _, err := t.tx.Exec(
		`INSERT INTO cgcoord_events (group_name, seq, at, request_id, type, detail)
		 VALUES ($1,$2,$3::timestamptz,$4,$5,$6::jsonb)`,
		e.Group, seq, string(wire.At), wire.RequestID, wire.Type, string(wire.Detail),
	); err != nil {
		return 0, err
	}
	return protocol.Seq(seq), nil
}

func (t *pgTx) LoadGroup(name string) (*protocol.GroupState, error) {
	var raw []byte
	err := t.tx.QueryRow(`SELECT state FROM cgcoord_groups WHERE name = $1`, name).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	var s protocol.GroupState
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, err
	}
	return &s, nil
}

func (t *pgTx) ReadEvents(group string, from protocol.Seq, limit int) ([]protocol.Event, error) {
	rows, err := t.tx.Query(
		`SELECT seq, at, request_id, type, detail
		 FROM cgcoord_events WHERE group_name = $1 AND seq >= $2
		 ORDER BY seq ASC LIMIT $3`,
		group, from, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var events []protocol.Event
	for rows.Next() {
		var seq int64
		var at time.Time
		var typ, reqID string
		var detail []byte
		if err := rows.Scan(&seq, &at, &reqID, &typ, &detail); err != nil {
			return nil, err
		}
		atJSON, err := json.Marshal(at)
		if err != nil {
			return nil, err
		}
		wire := fmt.Sprintf(
			`{"seq":%d,"group":%s,"type":%s,"at":%s,"request_id":%s,"detail":%s}`,
			seq, jsonString(group), jsonString(typ), string(atJSON), jsonString(reqID), string(detail),
		)
		e, err := protocol.UnmarshalEvent([]byte(wire))
		if err != nil {
			return nil, err
		}
		events = append(events, e)
	}
	return events, rows.Err()
}

func (t *pgTx) Commit() error   { return t.tx.Commit() }
func (t *pgTx) Rollback()       { _ = t.tx.Rollback() }

func pgCode(err error) string {
	type coder interface{ SQLState() string }
	var c coder
	if errors.As(err, &c) {
		return c.SQLState()
	}
	return ""
}

func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}
