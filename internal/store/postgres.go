package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

// Postgres is the durable Store backed by PostgreSQL. All state for one node
// lives in one DSN; the three local processes may share a database cluster
// using distinct database names or row namespaces by node id (we namespace by
// node id in every table, so one database is enough for the demo).
type Postgres struct {
	pool *pgxpool.Pool
}

// OpenPostgres connects (with retries for container startup) and verifies.
func OpenPostgres(ctx context.Context, dsn string) (*Postgres, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, apperr.Failure(apperr.CodeStoreIO, "store.OpenPostgres", "bad dsn", err)
	}
	cfg.MaxConns = 8
	var pool *pgxpool.Pool
	deadline := time.Now().Add(20 * time.Second)
	for {
		pool, err = pgxpool.NewWithConfig(ctx, cfg)
		if err == nil {
			if pingErr := pool.Ping(ctx); pingErr == nil {
				return &Postgres{pool: pool}, nil
			} else {
				err = pingErr
				pool.Close()
			}
		}
		if time.Now().After(deadline) {
			return nil, apperr.Failure(apperr.CodeStoreIO, "store.OpenPostgres",
				"postgres unreachable: "+dsn, err)
		}
		time.Sleep(300 * time.Millisecond)
	}
}

func (p *Postgres) Close() error {
	p.pool.Close()
	return nil
}

// Schema is idempotent; CREATE TABLE IF NOT EXISTS keeps boot simple.
const schema = `
CREATE TABLE IF NOT EXISTS accounts (
  node_id   TEXT NOT NULL,
  acc_id    TEXT NOT NULL,
  owner     TEXT NOT NULL,
  balance   NUMERIC NOT NULL,
  PRIMARY KEY (node_id, acc_id)
);
CREATE TABLE IF NOT EXISTS boot_epoch (
  node_id TEXT PRIMARY KEY,
  epoch   BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_refs (
  node_id TEXT NOT NULL,
  ref     TEXT NOT NULL,
  PRIMARY KEY (node_id, ref)
);
CREATE TABLE IF NOT EXISTS outbox (
  id      BIGSERIAL,
  src     TEXT NOT NULL,
  dst     TEXT NOT NULL,
  chan_seq BIGINT NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (id),
  UNIQUE (src, dst, chan_seq)
);
CREATE INDEX IF NOT EXISTS outbox_channel_idx ON outbox(src, dst, chan_seq);
CREATE TABLE IF NOT EXISTS snap_records (
  node_id   TEXT NOT NULL,
  snap_id   TEXT NOT NULL,
  phase     TEXT NOT NULL,
  reason    TEXT NOT NULL DEFAULT '',
  local     JSONB,
  channels  JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (node_id, snap_id)
);
CREATE TABLE IF NOT EXISTS events (
  id      BIGSERIAL PRIMARY KEY,
  run_id  TEXT NOT NULL,
  seq     BIGINT NOT NULL,
  payload JSONB NOT NULL,
  UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id, seq);
`

func (p *Postgres) Bootstrap(ctx context.Context, node protocol.NodeID, accounts []protocol.Account) error {
	_, err := p.pool.Exec(ctx, schema)
	if err != nil {
		return wrapPG(err, "create schema")
	}
	// Seed accounts only when none exist for this node (idempotent boot).
	var n int
	if err := p.pool.QueryRow(ctx,
		`SELECT count(*) FROM accounts WHERE node_id=$1`, node).Scan(&n); err != nil {
		return wrapPG(err, "count accounts")
	}
	if n == 0 {
		batch := new(pgx.Batch)
		for _, a := range accounts {
			batch.Queue(`INSERT INTO accounts(node_id, acc_id, owner, balance)
			             VALUES ($1,$2,$3,$4)`, node, a.ID, node, a.Balance)
		}
		br := p.pool.SendBatch(ctx, batch)
		for range accounts {
			if _, err := br.Exec(); err != nil {
				br.Close()
				return wrapPG(err, "seed accounts")
			}
		}
		br.Close()
	}
	return nil
}

func (p *Postgres) LoadAccounts(ctx context.Context, node protocol.NodeID) ([]protocol.Account, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT acc_id, owner, balance FROM accounts WHERE node_id=$1 ORDER BY acc_id`, node)
	if err != nil {
		return nil, wrapPG(err, "load accounts")
	}
	defer rows.Close()
	var out []protocol.Account
	for rows.Next() {
		var a protocol.Account
		if err := rows.Scan(&a.ID, &a.Owner, &a.Balance); err != nil {
			return nil, wrapPG(err, "scan account")
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func (p *Postgres) SaveAccounts(ctx context.Context, node protocol.NodeID, accounts []protocol.Account) error {
	tx, err := p.pool.Begin(ctx)
	if err != nil {
		return wrapPG(err, "begin save accounts")
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `DELETE FROM accounts WHERE node_id=$1`, node); err != nil {
		return wrapPG(err, "reset accounts")
	}
	for _, a := range accounts {
		if _, err := tx.Exec(ctx,
			`INSERT INTO accounts(node_id,acc_id,owner,balance) VALUES ($1,$2,$3,$4)`,
			node, a.ID, node, a.Balance); err != nil {
			return wrapPG(err, "insert account")
		}
	}
	return wrapPG(tx.Commit(ctx), "commit accounts")
}

func (p *Postgres) BumpEpoch(ctx context.Context, node protocol.NodeID) (uint64, error) {
	var epoch uint64
	err := p.pool.QueryRow(ctx,
		`INSERT INTO boot_epoch(node_id, epoch) VALUES ($1, 1)
		 ON CONFLICT (node_id) DO UPDATE SET epoch = boot_epoch.epoch + 1
		 RETURNING epoch`, node).Scan(&epoch)
	if err != nil {
		return 0, wrapPG(err, "bump epoch")
	}
	return epoch, nil
}

func (p *Postgres) CurrentEpoch(ctx context.Context, node protocol.NodeID) (uint64, error) {
	var epoch uint64
	err := p.pool.QueryRow(ctx,
		`SELECT COALESCE((SELECT epoch FROM boot_epoch WHERE node_id=$1), 0)`, node).Scan(&epoch)
	if err != nil {
		return 0, wrapPG(err, "current epoch")
	}
	return epoch, nil
}

func (p *Postgres) OpenRounds(ctx context.Context, node protocol.NodeID) ([]protocol.SnapshotID, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT snap_id FROM snap_records WHERE node_id=$1 AND phase='recording' ORDER BY snap_id`, node)
	if err != nil {
		return nil, wrapPG(err, "open rounds")
	}
	defer rows.Close()
	var out []protocol.SnapshotID
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			return nil, wrapPG(err, "scan round")
		}
		out = append(out, protocol.SnapshotID(s))
	}
	return out, rows.Err()
}

func (p *Postgres) SaveRecord(ctx context.Context, rec protocol.NodeRecord) error {
	localJSON := []byte("null")
	if rec.Local != nil {
		b, err := json.Marshal(rec.Local)
		if err != nil {
			return apperr.Failure(apperr.CodeStoreIO, "SaveRecord", "encode local", err)
		}
		localJSON = b
	}
	chJSON, err := json.Marshal(rec.Channels)
	if err != nil {
		return apperr.Failure(apperr.CodeStoreIO, "SaveRecord", "encode channels", err)
	}
	_, err = p.pool.Exec(ctx, `
		INSERT INTO snap_records(node_id, snap_id, phase, reason, local, channels, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6, now())
		ON CONFLICT (node_id, snap_id) DO UPDATE SET
		  phase=EXCLUDED.phase, reason=EXCLUDED.reason, local=EXCLUDED.local,
		  channels=EXCLUDED.channels, updated_at=now()`,
		rec.Node, rec.Snapshot, string(rec.Phase), rec.Reason, localJSON, chJSON)
	return wrapPG(err, "save record")
}

func (p *Postgres) GetRecord(ctx context.Context, node protocol.NodeID, snap protocol.SnapshotID) (protocol.NodeRecord, error) {
	row := p.pool.QueryRow(ctx,
		`SELECT node_id, snap_id, phase, reason, local, channels
		 FROM snap_records WHERE node_id=$1 AND snap_id=$2`, node, snap)
	return scanRecord(row)
}

func (p *Postgres) ListRecords(ctx context.Context, snap protocol.SnapshotID) ([]protocol.NodeRecord, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT node_id, snap_id, phase, reason, local, channels
		 FROM snap_records WHERE snap_id=$1 ORDER BY node_id`, snap)
	if err != nil {
		return nil, wrapPG(err, "list records")
	}
	defer rows.Close()
	var out []protocol.NodeRecord
	for rows.Next() {
		rec, err := scanRecord(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, rec)
	}
	return out, rows.Err()
}

func (p *Postgres) ListSnapshots(ctx context.Context) ([]protocol.SnapshotID, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT DISTINCT snap_id FROM snap_records ORDER BY snap_id`)
	if err != nil {
		return nil, wrapPG(err, "list snapshots")
	}
	defer rows.Close()
	var out []protocol.SnapshotID
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			return nil, wrapPG(err, "scan snap")
		}
		out = append(out, protocol.SnapshotID(s))
	}
	return out, rows.Err()
}

func (p *Postgres) Abort(ctx context.Context, node protocol.NodeID, snap protocol.SnapshotID, reason string) error {
	tag, err := p.pool.Exec(ctx,
		`UPDATE snap_records SET phase='aborted', reason=$3, updated_at=now()
		 WHERE node_id=$1 AND snap_id=$2`, node, snap, reason)
	if err != nil {
		return wrapPG(err, "abort")
	}
	if tag.RowsAffected() == 0 {
		return apperr.Inputf(apperr.CodeUnknownSnapshot, "cannot abort unknown round %s@%s", snap, node)
	}
	return nil
}

// --- outbox ---

func (p *Postgres) AppendOutbox(env protocol.Envelope) (uint64, error) {
	payload, err := json.Marshal(env)
	if err != nil {
		return 0, apperr.Failure(apperr.CodeStoreIO, "AppendOutbox", "encode", err)
	}
	var seq uint64
	err = p.pool.QueryRow(context.Background(), `
		INSERT INTO outbox(src,dst,chan_seq,payload)
		SELECT $1,$2,COALESCE(MAX(chan_seq),0)+1,$3 FROM outbox WHERE src=$1 AND dst=$2
		RETURNING chan_seq`, env.Src, env.Dst, payload).Scan(&seq)
	if err != nil {
		return 0, wrapPG(err, "append outbox")
	}
	return seq, nil
}

func (p *Postgres) ListOutbox(src, dst protocol.NodeID) ([]protocol.Envelope, error) {
	rows, err := p.pool.Query(context.Background(),
		`SELECT chan_seq, payload FROM outbox WHERE src=$1 AND dst=$2 ORDER BY chan_seq`, src, dst)
	if err != nil {
		return nil, wrapPG(err, "list outbox")
	}
	defer rows.Close()
	var out []protocol.Envelope
	for rows.Next() {
		var seq uint64
		var raw []byte
		if err := rows.Scan(&seq, &raw); err != nil {
			return nil, wrapPG(err, "scan outbox")
		}
		var env protocol.Envelope
		if err := json.Unmarshal(raw, &env); err != nil {
			return nil, apperr.Failure(apperr.CodeStoreIO, "ListOutbox", "decode", err)
		}
		env.Seq = seq
		out = append(out, env)
	}
	return out, rows.Err()
}

func (p *Postgres) AckOutbox(src, dst protocol.NodeID, seq uint64) error {
	tag, err := p.pool.Exec(context.Background(),
		`DELETE FROM outbox WHERE src=$1 AND dst=$2 AND chan_seq=$3`, src, dst, seq)
	if err != nil {
		return wrapPG(err, "ack outbox")
	}
	if tag.RowsAffected() == 0 {
		return apperr.Inputf(apperr.CodeMalformed,
			"ack: seq %d not pending on %s->%s", seq, src, dst)
	}
	return nil
}

func (p *Postgres) PurgeMarkers(src, dst protocol.NodeID, snap protocol.SnapshotID) (int, error) {
	tag, err := p.pool.Exec(context.Background(),
		`DELETE FROM outbox
		 WHERE src=$1 AND dst=$2
		   AND payload->>'type' = 'marker'
		   AND payload->'marker'->>'snapshot' = $3`, src, dst, string(snap))
	if err != nil {
		return 0, wrapPG(err, "purge markers")
	}
	return int(tag.RowsAffected()), nil
}

// --- events ---

func (p *Postgres) AppendEvent(ctx context.Context, ev protocol.Event) error {
	payload, err := json.Marshal(ev)
	if err != nil {
		return apperr.Failure(apperr.CodeStoreIO, "AppendEvent", "encode", err)
	}
	_, err = p.pool.Exec(ctx, `
		INSERT INTO events(run_id, seq, payload)
		SELECT $1, COALESCE(MAX(seq),0)+1, $2 FROM events WHERE run_id=$1
		ON CONFLICT (run_id, seq) DO NOTHING`, ev.RunID, payload)
	if err != nil {
		return wrapPG(err, "append event")
	}
	return nil
}

func (p *Postgres) ListEvents(ctx context.Context, runID string) ([]protocol.Event, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT payload FROM events WHERE run_id=$1 ORDER BY seq`, runID)
	if err != nil {
		return nil, wrapPG(err, "list events")
	}
	defer rows.Close()
	var out []protocol.Event
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, wrapPG(err, "scan event")
		}
		var ev protocol.Event
		if err := json.Unmarshal(raw, &ev); err != nil {
			return nil, apperr.Failure(apperr.CodeStoreIO, "ListEvents", "decode", err)
		}
		out = append(out, ev)
	}
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, wrapPG(err, "events rows")
	}
	if len(out) == 0 {
		return nil, apperr.Inputf(apperr.CodeUnknownRun, "run %q not found", runID)
	}
	return out, nil
}

func (p *Postgres) ListRuns(ctx context.Context) ([]string, error) {
	rows, err := p.pool.Query(ctx, `SELECT DISTINCT run_id FROM events ORDER BY run_id`)
	if err != nil {
		return nil, wrapPG(err, "list runs")
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			return nil, wrapPG(err, "scan run")
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// --- dedup ---

func (p *Postgres) SeenRef(ctx context.Context, node protocol.NodeID, ref string) (bool, error) {
	var ok bool
	err := p.pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM seen_refs WHERE node_id=$1 AND ref=$2)`, node, ref).Scan(&ok)
	if err != nil {
		return false, wrapPG(err, "seen ref")
	}
	return ok, nil
}

func (p *Postgres) RememberRef(ctx context.Context, node protocol.NodeID, ref string) error {
	_, err := p.pool.Exec(ctx,
		`INSERT INTO seen_refs(node_id,ref) VALUES ($1,$2)`, node, ref)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return apperr.Conflict(apperr.CodeSnapshotInProgress, "ref "+ref+" already known")
		}
		return wrapPG(err, "remember ref")
	}
	return nil
}

// row abstracts pgx Row and Rows for scanRecord.
type row interface {
	Scan(dest ...any) error
}

func scanRecord(r row) (protocol.NodeRecord, error) {
	var (
		rec                                                   protocol.NodeRecord
		node, snap, phase, reason                             string
		localRaw, chRaw                                       []byte
	)
	rec.Channels = map[protocol.NodeID]*protocol.ChannelState{}
	if err := r.Scan(&node, &snap, &phase, &reason, &localRaw, &chRaw); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return protocol.NodeRecord{}, apperr.Inputf(apperr.CodeUnknownSnapshot, "record not found")
		}
		return protocol.NodeRecord{}, wrapPG(err, "scan record")
	}
	rec.Node = protocol.NodeID(node)
	rec.Snapshot = protocol.SnapshotID(snap)
	rec.Phase = protocol.Phase(phase)
	rec.Reason = reason
	if string(localRaw) != "null" && len(localRaw) > 0 {
		var ls protocol.LocalState
		if err := json.Unmarshal(localRaw, &ls); err != nil {
			return protocol.NodeRecord{}, apperr.Failure(apperr.CodeStoreIO, "scanRecord", "decode local", err)
		}
		rec.Local = &ls
	}
	if err := json.Unmarshal(chRaw, &rec.Channels); err != nil {
		return protocol.NodeRecord{}, apperr.Failure(apperr.CodeStoreIO, "scanRecord", "decode channels", err)
	}
	return rec, nil
}

func wrapPG(err error, op string) error {
	if err == nil {
		return nil
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "23505":
			return apperr.Conflict(apperr.CodeDuplicateSnapshot,
				fmt.Sprintf("unique violation at %s: %s", op, pgErr.ConstraintName))
		case "23514", "23502", "22P02":
			return apperr.Inputf(apperr.CodeMalformed, "%s: %s", op, pgErr.Message)
		case "53000", "53100", "53200", "53300", "53400":
			return apperr.Exhausted(apperr.CodeStoreFull,
				"postgres resource exhausted at "+op+": "+pgErr.Message)
		}
	}
	return apperr.Failure(apperr.CodeStoreIO, "postgres."+op, err.Error(), err)
}
