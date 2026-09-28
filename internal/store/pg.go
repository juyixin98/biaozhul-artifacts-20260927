package store

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"fmt"
	"time"

	"clsnap/internal/errs"
	"clsnap/internal/protocol"

	_ "github.com/lib/pq"
)

//go:embed pg_schema.sql
var pgSchema string

// PgConfig configures one node's PostgreSQL store. Each node gets its own
// schema in the same database.
type PgConfig struct {
	DSN      string // e.g. postgres://clsnap:clsnap@127.0.0.1:5432/clsnap?sslmode=disable
	Schema   string // e.g. n1
	NodeID   string
	RunID    string
	OutboxCap int
}

// PgStore implements Store against PostgreSQL. All state changes that must
// survive a crash (ledger, clock, sessions, channels, outbox, journal) are
// committed before the corresponding side effect is exposed.
type PgStore struct {
	db        *sql.DB
	nodeID    string
	runID     string
	schema    string
	outboxCap int
}

func OpenPg(ctx context.Context, cfg PgConfig) (*PgStore, error) {
	if cfg.OutboxCap <= 0 {
		cfg.OutboxCap = 4096
	}
	db, err := sql.Open("postgres", cfg.DSN)
	if err != nil {
		return nil, errs.New(errs.ClassUnavailable, "db_open", "cannot open postgres", err)
	}
	db.SetMaxOpenConns(1) // search_path is connection-scoped; serialize access
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, errs.New(errs.ClassUnavailable, "db_unreachable", "cannot reach postgres", err)
	}
	s := &PgStore{db: db, nodeID: cfg.NodeID, runID: cfg.RunID, schema: cfg.Schema, outboxCap: cfg.OutboxCap}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *PgStore) migrate(ctx context.Context) error {
	if _, err := s.db.ExecContext(ctx, "CREATE SCHEMA IF NOT EXISTS "+quoteIdent(s.schema)); err != nil {
		return errs.New(errs.ClassUnavailable, "db_schema", "cannot create schema", err)
	}
	if _, err := s.db.ExecContext(ctx, "SET search_path TO "+quoteIdent(s.schema)); err != nil {
		return err
	}
	if _, err := s.db.ExecContext(ctx, pgSchema); err != nil {
		return errs.New(errs.ClassUnavailable, "db_migrate", "cannot apply schema migration", err)
	}
	return nil
}

func (s *PgStore) Close() error { return s.db.Close() }

func (s *PgStore) NodeID() string { return s.nodeID }
func (s *PgStore) RunID() string  { return s.runID }

// Bootstrap initializes a fresh node. It is idempotent.
func (s *PgStore) Bootstrap(initial map[string]int64) (bool, error) {
	ctx := context.Background()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, dbErr(err)
	}
	defer tx.Rollback()

	var fresh bool
	err = tx.QueryRowContext(ctx,
		`INSERT INTO meta(key,value) VALUES('node_id',$1)
		 ON CONFLICT (key) DO NOTHING RETURNING true`, s.nodeID).Scan(&fresh)
	if err == sql.ErrNoRows {
		fresh = false
	} else if err != nil {
		return false, dbErr(err)
	}
	if fresh {
		for acct, v := range initial {
			if v < 0 {
				return false, errs.New(errs.ClassInputInvalid, errs.CodeBadAmount,
					fmt.Sprintf("negative initial balance %q", acct), nil)
			}
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO ledger(account,balance) VALUES($1,$2)`, acct, v); err != nil {
				return false, dbErr(err)
			}
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO counters(name,value) VALUES('lamport',0)
			 ON CONFLICT DO NOTHING`); err != nil {
			return false, dbErr(err)
		}
		// Event journal of the new boot.
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO events(run_id,kind,session_id,lamport,detail)
			 VALUES($1,$2,'',0,$3)`, s.runID, EvNodeBoot,
			jsonVal(map[string]interface{}{"node_id": s.nodeID, "initial": initial})); err != nil {
			return false, dbErr(err)
		}
	}
	if err := tx.Commit(); err != nil {
		return false, dbErr(err)
	}
	return fresh, nil
}

// RecoverAborts implements the restart rule: every session that was recording
// when the process previously stopped is marked aborted. Partial data remains
// in the journal; it can never be completed.
func (s *PgStore) RecoverAborts(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT session_id FROM sessions WHERE status='recording' ORDER BY session_id`)
	if err != nil {
		return nil, dbErr(err)
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, dbErr(err)
		}
		ids = append(ids, id)
	}
	rows.Close()
	for _, id := range ids {
		if _, err := s.db.ExecContext(ctx,
			`UPDATE sessions SET status='aborted', aborted_at=now(),
			        abort_reason='process restarted while session was recording'
			  WHERE session_id=$1`, id); err != nil {
			return nil, dbErr(err)
		}
		if _, err := s.db.ExecContext(ctx,
			`INSERT INTO events(run_id,kind,session_id,lamport,detail)
			 VALUES($1,$2,$3,0,$4)`, s.runID, EvSessionAborted, id,
			jsonVal(map[string]interface{}{"reason": "recovered_as_aborted_on_boot"})); err != nil {
			return nil, dbErr(err)
		}
	}
	return ids, nil
}

// Reset wipes a node schema (tests / deterministic restarts only).
func (s *PgStore) Reset(ctx context.Context) error {
	if _, err := s.db.ExecContext(ctx,
		`TRUNCATE meta,ledger,counters,outbox,incoming_seq,seen_messages,
		          events,sessions,channels RESTART IDENTITY CASCADE`); err != nil {
		return dbErr(err)
	}
	return nil
}

func (s *PgStore) Balances() map[string]int64 {
	rows, err := s.db.QueryContext(context.Background(), `SELECT account,balance FROM ledger`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := map[string]int64{}
	for rows.Next() {
		var a string
		var v int64
		if err := rows.Scan(&a, &v); err != nil {
			return nil
		}
		out[a] = v
	}
	return out
}

func (s *PgStore) AddBalance(account string, delta int64) error {
	tag, err := s.db.ExecContext(context.Background(),
		`UPDATE ledger SET balance=balance+$1 WHERE account=$2 AND balance+$1 >= 0`,
		delta, account)
	if err != nil {
		return dbErr(err)
	}
	n, _ := tag.RowsAffected()
	if n == 0 {
		var exists int
		_ = s.db.QueryRowContext(context.Background(),
			`SELECT 1 FROM ledger WHERE account=$1`, account).Scan(&exists)
		if exists == 0 {
			return errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer, "unknown account "+account, nil)
		}
		return errs.New(errs.ClassComputeFailure, errs.CodeInsufficientFunds,
			fmt.Sprintf("account %q would go negative (%+d)", account, delta), nil)
	}
	return nil
}

func (s *PgStore) Clock() (int64, func(int64)) {
	var t int64
	_ = s.db.QueryRowContext(context.Background(),
		`SELECT value FROM counters WHERE name='lamport'`).Scan(&t)
	return t, func(newT int64) {
		_, _ = s.db.ExecContext(context.Background(),
			`UPDATE counters SET value=GREATEST(value,$1) WHERE name='lamport'`, newT)
	}
}

func (s *PgStore) AppendEvent(ctx context.Context, ev Event) (int64, error) {
	var id int64
	err := s.db.QueryRowContext(ctx,
		`INSERT INTO events(run_id,at,kind,session_id,lamport,detail)
		 VALUES($1,COALESCE($2,now()),$3,$4,$5,$6) RETURNING id`,
		s.runID, nilTime(ev.At), ev.Kind, ev.SessionID, ev.Lamport, jsonVal(ev.Detail)).Scan(&id)
	if err != nil {
		return 0, dbErr(err)
	}
	return id, nil
}

func (s *PgStore) Journal(ctx context.Context, sessionID string, limit int) ([]Event, error) {
	q := `SELECT id,run_id,at,kind,session_id,lamport,detail FROM events`
	args := []interface{}{}
	if sessionID != "" {
		q += ` WHERE session_id=$1`
		args = append(args, sessionID)
	}
	q += ` ORDER BY id`
	if limit > 0 {
		q += fmt.Sprintf(` LIMIT %d`, limit)
	}
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, dbErr(err)
	}
	defer rows.Close()
	var out []Event
	for rows.Next() {
		var ev Event
		var detail []byte
		if err := rows.Scan(&ev.Seq, &ev.RunID, &ev.At, &ev.Kind, &ev.SessionID,
			&ev.Lamport, &detail); err != nil {
			return nil, dbErr(err)
		}
		ev.NodeID = s.nodeID
		_ = json.Unmarshal(detail, &ev.Detail)
		out = append(out, ev)
	}
	return out, nil
}

// EnqueueOutbox assigns the per-channel seq atomically and persists the
// envelope. Persist-before-send is what makes delivery reliable.
func (s *PgStore) EnqueueOutbox(ctx context.Context, toPeer string, env protocol.Envelope) (int64, error) {
	var n int
	if err := s.db.QueryRowContext(ctx,
		`SELECT count(*) FROM outbox WHERE to_peer=$1`, toPeer).Scan(&n); err != nil {
		return 0, dbErr(err)
	}
	if n >= s.outboxCap {
		return 0, errs.New(errs.ClassResourceExhausted, errs.CodeOutboxFull,
			fmt.Sprintf("outbox to %s full (%d unacked)", toPeer, n), nil)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, dbErr(err)
	}
	defer tx.Rollback()
	var seq int64
	if err := tx.QueryRowContext(ctx,
		`INSERT INTO counters(name,value) VALUES('out_'+$1,1)
		 ON CONFLICT (name) DO UPDATE SET value=counters.value+1 RETURNING value`,
		toPeer).Scan(&seq); err != nil {
		return 0, dbErr(err)
	}
	env.Seq = seq
	raw, _ := json.Marshal(env)
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO outbox(to_peer,seq,envelope) VALUES($1,$2,$3)`,
		toPeer, seq, string(raw)); err != nil {
		return 0, dbErr(err)
	}
	if err := tx.Commit(); err != nil {
		return 0, dbErr(err)
	}
	return seq, nil
}

func (s *PgStore) PendingOutbox(peer string) ([]OutboxItem, error) {
	rows, err := s.db.QueryContext(context.Background(),
		`SELECT id,envelope FROM outbox WHERE to_peer=$1 ORDER BY seq`, peer)
	if err != nil {
		return nil, dbErr(err)
	}
	defer rows.Close()
	var out []OutboxItem
	for rows.Next() {
		var id int64
		var raw []byte
		if err := rows.Scan(&id, &raw); err != nil {
			return nil, dbErr(err)
		}
		var env protocol.Envelope
		if err := json.Unmarshal(raw, &env); err != nil {
			return nil, dbErr(err)
		}
		out = append(out, OutboxItem{ID: id, ToPeer: peer, Envelope: env})
	}
	return out, nil
}

func (s *PgStore) AckOutbox(id int64) error {
	_, err := s.db.ExecContext(context.Background(), `DELETE FROM outbox WHERE id=$1`, id)
	return dbErr(err)
}

func (s *PgStore) OutboxLen(peer string) (int, error) {
	var n int
	if err := s.db.QueryRowContext(context.Background(),
		`SELECT count(*) FROM outbox WHERE to_peer=$1`, peer).Scan(&n); err != nil {
		return 0, dbErr(err)
	}
	return n, nil
}

func (s *PgStore) StartSession(ctx context.Context, rec SessionRecord) error {
	var existing string
	err := s.db.QueryRowContext(ctx,
		`SELECT status FROM sessions WHERE session_id=$1`, rec.SessionID).Scan(&existing)
	if err == nil {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionExists,
			"session "+rec.SessionID+" already exists ("+existing+")", nil)
	}
	if err != sql.ErrNoRows {
		return dbErr(err)
	}
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO sessions(session_id,initiator,status,started_at)
		 VALUES($1,$2,'recording',$3)`,
		rec.SessionID, rec.Initiator, nilTime(rec.StartedAt)); err != nil {
		return dbErr(err)
	}
	// Channel rows come from the topology registered at bootstrap.
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO channels(session_id,from_peer)
		 SELECT $1,peer FROM incoming_seq
		 ON CONFLICT DO NOTHING`, rec.SessionID); err != nil {
		return dbErr(err)
	}
	return nil
}

// BootstrapTopology registers known incoming channels (peers). Called by the
// node after New, before traffic. Idempotent.
func (s *PgStore) BootstrapTopology(peers []string) error {
	for _, p := range peers {
		if _, err := s.db.ExecContext(context.Background(),
			`INSERT INTO incoming_seq(peer,seq) VALUES($1,0) ON CONFLICT DO NOTHING`, p); err != nil {
			return dbErr(err)
		}
	}
	return nil
}

func (s *PgStore) GetSession(sessionID string) (*SessionRecord, error) {
	rec := SessionRecord{Channels: map[string]*ChannelState{}}
	var localBalances []byte
	var localLamport sql.NullInt64
	var localAt sql.NullTime
	var localTotal sql.NullInt64
	var completedAt, abortedAt sql.NullTime
	var abortReason string
	err := s.db.QueryRowContext(context.Background(),
		`SELECT session_id,initiator,status,started_at,completed_at,aborted_at,
		        abort_reason,local_lamport,local_recorded_at,local_balances,local_total
		   FROM sessions WHERE session_id=$1`, sessionID).Scan(
		&rec.SessionID, &rec.Initiator, &rec.Status, &rec.StartedAt,
		&completedAt, &abortedAt, &abortReason,
		&localLamport, &localAt, &localBalances, &localTotal)
	if err == sql.ErrNoRows {
		return nil, errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown,
			"unknown session "+sessionID, nil)
	}
	if err != nil {
		return nil, dbErr(err)
	}
	if completedAt.Valid {
		t := completedAt.Time
		rec.CompletedAt = &t
	}
	if abortedAt.Valid {
		t := abortedAt.Time
		rec.AbortedAt = &t
		rec.AbortReason = abortReason
	}
	if localLamport.Valid {
		rec.Local = &LocalState{
			NodeID: s.nodeID, Lamport: localLamport.Int64,
			RecordedAt: localAt.Time, TotalBalance: localTotal.Int64,
		}
		_ = json.Unmarshal(localBalances, &rec.Local.Balances)
	}
	rows, err := s.db.QueryContext(context.Background(),
		`SELECT from_peer,recorded,total_inflight,marker_seen_at,marker_lamport
		   FROM channels WHERE session_id=$1 ORDER BY from_peer`, sessionID)
	if err != nil {
		return nil, dbErr(err)
	}
	defer rows.Close()
	for rows.Next() {
		ch := ChannelState{To: s.nodeID}
		var recorded []byte
		var markerAt sql.NullTime
		var markerLamport sql.NullInt64
		if err := rows.Scan(&ch.From, &recorded, &ch.TotalInFlight,
			&markerAt, &markerLamport); err != nil {
			return nil, dbErr(err)
		}
		_ = json.Unmarshal(recorded, &ch.Recorded)
		if markerAt.Valid {
			ch.MarkerSeenAt = markerAt.Time
			ch.MarkerLamport = markerLamport.Int64
		}
		rec.Channels[ch.From] = &ch
	}
	rec.PeersPending = nil
	if rec.Status == StatusRecording {
		for from, ch := range rec.Channels {
			if ch.MarkerSeenAt == (time.Time{}) {
				rec.PeersPending = append(rec.PeersPending, from)
			}
		}
	}
	return &rec, nil
}

func (s *PgStore) ListSessions() ([]*SessionRecord, error) {
	rows, err := s.db.QueryContext(context.Background(),
		`SELECT session_id FROM sessions ORDER BY started_at,session_id`)
	if err != nil {
		return nil, dbErr(err)
	}
	defer rows.Close()
	var out []*SessionRecord
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, dbErr(err)
		}
		rec, err := s.GetSession(id)
		if err != nil {
			return nil, err
		}
		out = append(out, rec)
	}
	return out, nil
}

func (s *PgStore) SaveLocalState(ctx context.Context, sessionID string, st LocalState) error {
	raw, _ := json.Marshal(st.Balances)
	tag, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET local_lamport=$1,local_recorded_at=$2,
		        local_balances=$3,local_total=$4
		  WHERE session_id=$5 AND local_lamport IS NULL`,
		st.Lamport, nilTime(st.RecordedAt), string(raw), st.TotalBalance, sessionID)
	if err != nil {
		return dbErr(err)
	}
	if n, _ := tag.RowsAffected(); n == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeLocalStateExists,
			"local state already recorded for "+sessionID, nil)
	}
	return nil
}

func (s *PgStore) RecordChannelMessage(ctx context.Context, sessionID, fromPeer string,
	t protocol.Transfer, markerLamport int64) error {
	raw, _ := json.Marshal(t)
	tag, err := s.db.ExecContext(ctx,
		`UPDATE channels SET recorded = recorded || $1::jsonb,
		        total_inflight = total_inflight + $2
		  WHERE session_id=$3 AND from_peer=$4 AND marker_seen_at IS NULL`,
		"["+string(raw)+"]", t.Amount, sessionID, fromPeer)
	if err != nil {
		return dbErr(err)
	}
	if n, _ := tag.RowsAffected(); n == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			fmt.Sprintf("channel %s closed or unknown for %s", fromPeer, sessionID), nil)
	}
	return nil
}

func (s *PgStore) CloseChannel(ctx context.Context, sessionID, fromPeer string, markerLamport int64) error {
	tag, err := s.db.ExecContext(ctx,
		`UPDATE channels SET marker_seen_at=now(),marker_lamport=$1
		  WHERE session_id=$2 AND from_peer=$3 AND marker_seen_at IS NULL`,
		markerLamport, sessionID, fromPeer)
	if err != nil {
		return dbErr(err)
	}
	if n, _ := tag.RowsAffected(); n == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			"channel already closed or unknown", nil)
	}
	return nil
}

func (s *PgStore) AbortSession(ctx context.Context, sessionID, reason string) error {
	tag, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET status='aborted',aborted_at=now(),abort_reason=$1
		  WHERE session_id=$2`, reason, sessionID)
	if err != nil {
		return dbErr(err)
	}
	if n, _ := tag.RowsAffected(); n == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	return nil
}

func (s *PgStore) CompleteSession(ctx context.Context, sessionID string) error {
	var open int
	if err := s.db.QueryRowContext(ctx,
		`SELECT count(*) FROM channels WHERE session_id=$1 AND marker_seen_at IS NULL`,
		sessionID).Scan(&open); err != nil {
		return dbErr(err)
	}
	if open > 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			fmt.Sprintf("%d channels still open", open), nil)
	}
	var hasLocal int
	if err := s.db.QueryRowContext(ctx,
		`SELECT count(*) FROM sessions WHERE session_id=$1 AND local_lamport IS NOT NULL`,
		sessionID).Scan(&hasLocal); err != nil {
		return dbErr(err)
	}
	if hasLocal == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			"cannot complete without local state", nil)
	}
	tag, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET status='complete',completed_at=now() WHERE session_id=$1`,
		sessionID)
	if err != nil {
		return dbErr(err)
	}
	if n, _ := tag.RowsAffected(); n == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	return nil
}

func (s *PgStore) HasSeenMessage(msgID string) (bool, error) {
	var n int
	if err := s.db.QueryRowContext(context.Background(),
		`SELECT 1 FROM seen_messages WHERE msg_id=$1`, msgID).Scan(&n); err == sql.ErrNoRows {
		return false, nil
	} else if err != nil {
		return false, dbErr(err)
	}
	return true, nil
}

func (s *PgStore) RememberMessage(msgID string) error {
	_, err := s.db.ExecContext(context.Background(),
		`INSERT INTO seen_messages(msg_id) VALUES($1) ON CONFLICT DO NOTHING`, msgID)
	return dbErr(err)
}

func (s *PgStore) LastInSeq(fromPeer string) (int64, error) {
	var seq int64
	err := s.db.QueryRowContext(context.Background(),
		`SELECT seq FROM incoming_seq WHERE peer=$1`, fromPeer).Scan(&seq)
	if err == sql.ErrNoRows {
		return 0, errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			"unknown incoming channel "+fromPeer, nil)
	}
	if err != nil {
		return 0, dbErr(err)
	}
	return seq, nil
}

func (s *PgStore) SetLastInSeq(fromPeer string, seq int64) error {
	tag, err := s.db.ExecContext(context.Background(),
		`UPDATE incoming_seq SET seq=$1 WHERE peer=$2 AND seq < $1`, seq, fromPeer)
	if err != nil {
		return dbErr(err)
	}
	if n, _ := tag.RowsAffected(); n == 0 {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			fmt.Sprintf("seq %d is not greater than last seen on %s", seq, fromPeer), nil)
	}
	return nil
}

func jsonVal(v interface{}) []byte {
	if v == nil {
		return []byte("{}")
	}
	b, _ := json.Marshal(v)
	return b
}

func nilTime(t time.Time) interface{} {
	if t.IsZero() {
		return nil
	}
	return t
}

func quoteIdent(s string) string {
	// schema names come from local config, not user input; still quote safely.
	return `"` + replaceAll(s, `"`, `""`) + `"`
}

func replaceAll(s, old, new string) string {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		if i+len(old) <= len(s) && s[i:i+len(old)] == old {
			out = append(out, new...)
			i += len(old) - 1
			continue
		}
		out = append(out, s[i])
	}
	return string(out)
}

func dbErr(err error) error {
	if err == nil {
		return nil
	}
	return errs.New(errs.ClassUnavailable, "db_error", err.Error(), err)
}
