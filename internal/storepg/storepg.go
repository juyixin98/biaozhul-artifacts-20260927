// Package storepg is the PostgreSQL Store implementation.
//
// All state transitions run inside a single database transaction that locks
// the affected row(s); the actual decision logic is the shared pure kernel, so
// storemem and storepg cannot drift in semantics.
package storepg

import (
	"context"
	_ "embed"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/lib/pq"

	"localbroker/internal/kernel"
	"localbroker/internal/protocol"
	"localbroker/internal/store"
)

//go:embed schema.sql
var schemaSQL string

// Store is a PostgreSQL-backed Store.
type Store struct {
	db *sql.DB
}

// Open connects to the database, ensures the schema exists and returns a Store.
func Open(ctx context.Context, connString string) (*Store, error) {
	db, err := sql.Open("postgres", connString)
	if err != nil {
		return nil, fmt.Errorf("storepg open: %w", err)
	}
	db.SetMaxOpenConns(16)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("storepg ping: %w", err)
	}
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("storepg schema: %w", err)
	}
	return &Store{db: db}, nil
}

// DB exposes the underlying handle for tests/admin tooling.
func (s *Store) DB() *sql.DB { return s.db }

// Close closes the pool.
func (s *Store) Close() error { return s.db.Close() }

// CreateQueue inserts a queue config.
func (s *Store) CreateQueue(ctx context.Context, cfg protocol.QueueConfig) error {
	if cfg.Name == "" {
		return &kernel.InvalidArgument{Msg: "queue name must not be empty"}
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO broker_queues (name, visibility_timeout, max_attempts, created_at, schema_version)
		VALUES ($1, $2, $3, $4, $5)`,
		cfg.Name, int64(cfg.VisibilityTimeout), cfg.MaxAttempts, time.Now().UTC(), protocol.Version)
	if err != nil {
		var pqe *pq.Error
		if errors.As(err, &pqe) && pqe.Code == "23505" { // unique_violation
			return kernel.ErrQueueExists
		}
		return fmt.Errorf("create queue: %w", err)
	}
	return nil
}

// QueueConfig returns a queue's config.
func (s *Store) QueueConfig(ctx context.Context, name string) (protocol.QueueConfig, error) {
	var (
		cfg      protocol.QueueConfig
		visNanos int64
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT name, visibility_timeout, max_attempts FROM broker_queues WHERE name=$1`, name).
		Scan(&cfg.Name, &visNanos, &cfg.MaxAttempts)
	if errors.Is(err, sql.ErrNoRows) {
		return protocol.QueueConfig{}, kernel.ErrQueueNotFound
	}
	if err != nil {
		return protocol.QueueConfig{}, fmt.Errorf("load queue: %w", err)
	}
	cfg.VisibilityTimeout = time.Duration(visNanos)
	return cfg, nil
}

// Queues lists all configured queues ordered by name.
func (s *Store) Queues(ctx context.Context) ([]protocol.QueueConfig, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT name, visibility_timeout, max_attempts FROM broker_queues ORDER BY name`)
	if err != nil {
		return nil, fmt.Errorf("list queues: %w", err)
	}
	defer rows.Close()
	var out []protocol.QueueConfig
	for rows.Next() {
		var (
			cfg      protocol.QueueConfig
			visNanos int64
		)
		if err := rows.Scan(&cfg.Name, &visNanos, &cfg.MaxAttempts); err != nil {
			return nil, fmt.Errorf("scan queue: %w", err)
		}
		cfg.VisibilityTimeout = time.Duration(visNanos)
		out = append(out, cfg)
	}
	return out, rows.Err()
}

// Publish inserts the message and appends the enqueued event in one tx.
func (s *Store) Publish(ctx context.Context, queue, id string, body []byte, now time.Time, runID string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("publish begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO broker_messages
		    (id, queue, body, state, attempts, receipt, receipt_gen, last_receipt,
		     deadline, enqueued_at, updated_at, failures)
		VALUES ($1,$2,$3,'available',0,'',0,'',to_timestamp(0),$4,$4,'[]'::jsonb)`,
		id, queue, body, now); err != nil {
		var pqe *pq.Error
		if errors.As(err, &pqe) && pqe.Code == "23503" { // foreign_key_violation
			return kernel.ErrQueueNotFound
		}
		if errors.As(err, &pqe) && pqe.Code == "23505" {
			return &kernel.InvalidArgument{Msg: "duplicate message id " + id}
		}
		return fmt.Errorf("publish message: %w", err)
	}
	if err := insertEvent(ctx, tx, queue, kernel.NewEnqueued(id, now), runID); err != nil {
		return err
	}
	return tx.Commit()
}

// Claim atomically grants one delivery.
func (s *Store) Claim(ctx context.Context, queue string, now time.Time, receipt, runID string) (store.ClaimResult, error) {
	if receipt == "" {
		return store.ClaimResult{}, &kernel.InvalidArgument{Msg: "receipt must not be empty"}
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return store.ClaimResult{}, fmt.Errorf("claim begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	cfg, err := loadConfigTx(ctx, tx, queue)
	if err != nil {
		return store.ClaimResult{}, err
	}

	var (
		id       string
		body     []byte
		attempts int
	)
	// SKIP LOCKED: concurrent claimers take different rows and never block.
	row := tx.QueryRowContext(ctx, `
		SELECT id, body, attempts
		FROM broker_messages
		WHERE queue = $1 AND state = 'available'
		ORDER BY id
		FOR UPDATE SKIP LOCKED
		LIMIT 1`, queue)
	switch err := row.Scan(&id, &body, &attempts); {
	case errors.Is(err, sql.ErrNoRows):
		return store.ClaimResult{}, kernel.ErrNoMessage
	case err != nil:
		return store.ClaimResult{}, fmt.Errorf("claim select: %w", err)
	}

	if !kernel.DecideClaim(attempts, cfg.MaxAttempts) {
		// Defensive (expiry normally dead-letters exhausted attempts).
		cur := protocol.LeaseView{ID: id, State: protocol.StateAvailable, Attempts: attempts}
		o := kernel.ExpireOutcome{Dead: true, ToState: protocol.StateDead,
			Failure: &protocol.Failure{Attempt: attempts, Kind: protocol.FailLeaseExpired,
				Reason: "claim blocked: delivery budget already exhausted", At: now}}
		ev := kernel.NewExpireEvent(id, cur, o, now)
		if err := applyLeaseEndTx(ctx, tx, queue, id, ev, now); err != nil {
			return store.ClaimResult{}, err
		}
		if err := tx.Commit(); err != nil {
			return store.ClaimResult{}, fmt.Errorf("claim commit: %w", err)
		}
		return store.ClaimResult{}, kernel.ErrNoMessage
	}

	prev := protocol.LeaseView{ID: id, State: protocol.StateAvailable, Attempts: attempts}
	ev, cl := kernel.NewClaimed(id, prev, cfg.VisibilityTimeout, now, receipt)
	if _, err := tx.ExecContext(ctx, `
		UPDATE broker_messages
		SET state='invisible', attempts=$2, receipt=$3, receipt_gen=$4,
		    last_receipt=receipt, deadline=$5, updated_at=$6
		WHERE id=$1`,
		id, cl.Attempts, cl.Receipt, cl.ReceiptGen, cl.Deadline, now); err != nil {
		return store.ClaimResult{}, fmt.Errorf("claim update: %w", err)
	}
	if err := insertEvent(ctx, tx, queue, ev, runID); err != nil {
		return store.ClaimResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return store.ClaimResult{}, fmt.Errorf("claim commit: %w", err)
	}
	return store.ClaimResult{
		MessageID: id,
		Body:      body,
		Attempts:  cl.Attempts,
		Claimed:   cl,
	}, nil
}

// rowLocked is a row locked for a receipt operation.
type rowLocked struct {
	id          string
	state       protocol.State
	attempts    int
	receipt     string
	receiptGen  int64
	lastReceipt string
	deadline    time.Time
}

func (r rowLocked) view() protocol.LeaseView {
	return protocol.LeaseView{
		ID: r.id, State: r.state, Attempts: r.attempts,
		Receipt: r.receipt, ReceiptGen: r.receiptGen,
		LastReceipt: r.lastReceipt, Deadline: r.deadline,
	}
}

// lockByReceipt locks the row a receipt currently or previously addressed.
// A live receipt wins; otherwise fall back to the last issuer so the kernel
// can distinguish stale from expired precisely.
func lockByReceipt(ctx context.Context, tx *sql.Tx, queue, receipt string) (rowLocked, error) {
	var r rowLocked
	var state string
	err := tx.QueryRowContext(ctx, `
		SELECT id, state, attempts, receipt, receipt_gen, last_receipt, deadline
		FROM broker_messages
		WHERE queue=$1 AND (receipt=$2 OR last_receipt=$2)
		ORDER BY CASE WHEN receipt=$2 THEN 0 ELSE 1 END
		FOR UPDATE
		LIMIT 1`, queue, receipt).Scan(
		&r.id, &state, &r.attempts, &r.receipt, &r.receiptGen, &r.lastReceipt, &r.deadline)
	if errors.Is(err, sql.ErrNoRows) {
		return rowLocked{}, kernel.ErrInvalidReceipt
	}
	if err != nil {
		return rowLocked{}, fmt.Errorf("lock by receipt: %w", err)
	}
	switch protocol.State(state) {
	case protocol.StateAvailable, protocol.StateInvisible, protocol.StateDead, protocol.StateAcked:
		r.state = protocol.State(state)
	default:
		return rowLocked{}, fmt.Errorf("%w: %q", kernel.ErrUnknownState, state)
	}
	return r, nil
}

// Extend atomically validates and extends a lease.
func (s *Store) Extend(ctx context.Context, queue, receipt string, extra time.Duration, now time.Time, runID string) (time.Time, error) {
	if extra <= 0 {
		return time.Time{}, &kernel.InvalidArgument{Msg: "extend duration must be > 0"}
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return time.Time{}, fmt.Errorf("extend begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := loadConfigTx(ctx, tx, queue); err != nil {
		return time.Time{}, err
	}
	r, err := lockByReceipt(ctx, tx, queue, receipt)
	if err != nil {
		return time.Time{}, err
	}
	// The receipt check happens while the row is locked and together with the
	// deadline write: extend-vs-timeout is atomic.
	if err := kernel.CheckReceipt(r.view(), receipt, now); err != nil {
		return time.Time{}, err
	}
	newDeadline := now.Add(extra)
	ev := kernel.NewExtended(r.id, receipt, r.receiptGen, newDeadline, extra, now)
	if _, err := tx.ExecContext(ctx,
		`UPDATE broker_messages SET deadline=$2, updated_at=$3 WHERE id=$1`,
		r.id, newDeadline, now); err != nil {
		return time.Time{}, fmt.Errorf("extend update: %w", err)
	}
	if err := insertEvent(ctx, tx, queue, ev, runID); err != nil {
		return time.Time{}, err
	}
	if err := tx.Commit(); err != nil {
		return time.Time{}, fmt.Errorf("extend commit: %w", err)
	}
	return newDeadline, nil
}

// Ack confirms a live receipt.
func (s *Store) Ack(ctx context.Context, queue, receipt string, now time.Time, runID string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("ack begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := loadConfigTx(ctx, tx, queue); err != nil {
		return err
	}
	r, err := lockByReceipt(ctx, tx, queue, receipt)
	if err != nil {
		return err
	}
	if err := kernel.CheckReceipt(r.view(), receipt, now); err != nil {
		return err
	}
	ev := kernel.NewAcked(r.id, receipt, r.receiptGen, now)
	if _, err := tx.ExecContext(ctx, `
		UPDATE broker_messages
		SET state='acked', receipt='', deadline=to_timestamp(0), updated_at=$2
		WHERE id=$1`, r.id, now); err != nil {
		return fmt.Errorf("ack update: %w", err)
	}
	if err := insertEvent(ctx, tx, queue, ev, runID); err != nil {
		return err
	}
	return tx.Commit()
}

// Nack reports explicit failure; redelivers or dead-letters.
func (s *Store) Nack(ctx context.Context, queue, receipt, reason string, now time.Time, runID string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("nack begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	cfg, err := loadConfigTx(ctx, tx, queue)
	if err != nil {
		return err
	}
	r, err := lockByReceipt(ctx, tx, queue, receipt)
	if err != nil {
		return err
	}
	if err := kernel.CheckReceipt(r.view(), receipt, now); err != nil {
		return err
	}
	o := kernel.DecideNack(r.view(), cfg.MaxAttempts, reason, now)
	ev := kernel.NewNackEvent(r.id, r.view(), o, now)
	if err := applyLeaseEndTx(ctx, tx, queue, r.id, ev, now); err != nil {
		return err
	}
	return tx.Commit()
}

// ExpireDue reclaims all due leases in one transaction.
func (s *Store) ExpireDue(ctx context.Context, queue string, now time.Time, runID string) (int, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("expire begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	cfg, err := loadConfigTx(ctx, tx, queue)
	if err != nil {
		return 0, err
	}

	rows, err := tx.QueryContext(ctx, `
		SELECT id, attempts, receipt, receipt_gen, deadline
		FROM broker_messages
		WHERE queue=$1 AND state='invisible' AND deadline <= $2
		ORDER BY id
		FOR UPDATE SKIP LOCKED`, queue, now)
	if err != nil {
		return 0, fmt.Errorf("expire select: %w", err)
	}
	type due struct {
		id       string
		attempts int
		cur      protocol.LeaseView
	}
	var dues []due
	for rows.Next() {
		var d due
		if err := rows.Scan(&d.id, &d.attempts, &d.cur.Receipt, &d.cur.ReceiptGen, &d.cur.Deadline); err != nil {
			_ = rows.Close()
			return 0, fmt.Errorf("expire scan: %w", err)
		}
		d.cur.ID = d.id
		d.cur.State = protocol.StateInvisible
		d.cur.Attempts = d.attempts
		dues = append(dues, d)
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return 0, fmt.Errorf("expire rows: %w", err)
	}
	_ = rows.Close()

	for _, d := range dues {
		o := kernel.DecideExpire(d.cur, cfg.MaxAttempts, now)
		ev := kernel.NewExpireEvent(d.id, d.cur, o, now)
		if err := applyLeaseEndTx(ctx, tx, queue, d.id, ev, now); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("expire commit: %w", err)
	}
	return len(dues), nil
}

// loadConfigTx reads a queue config within a transaction.
func loadConfigTx(ctx context.Context, tx *sql.Tx, name string) (protocol.QueueConfig, error) {
	var (
		cfg      protocol.QueueConfig
		visNanos int64
	)
	err := tx.QueryRowContext(ctx,
		`SELECT name, visibility_timeout, max_attempts FROM broker_queues WHERE name=$1 FOR SHARE`,
		name).Scan(&cfg.Name, &visNanos, &cfg.MaxAttempts)
	if errors.Is(err, sql.ErrNoRows) {
		return protocol.QueueConfig{}, kernel.ErrQueueNotFound
	}
	if err != nil {
		return protocol.QueueConfig{}, fmt.Errorf("load queue tx: %w", err)
	}
	cfg.VisibilityTimeout = time.Duration(visNanos)
	return cfg, nil
}

// applyLeaseEndTx updates the row for an expire/nack/dead-letter event and
// appends the event. Failures are accumulated; original body is untouched.
func applyLeaseEndTx(ctx context.Context, tx *sql.Tx, queue, id string, ev kernel.Event, now time.Time) error {
	failJSON, err := json.Marshal(ev.Failure)
	if err != nil {
		return fmt.Errorf("encode failure: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE broker_messages
		SET state=$2,
		    receipt='',
		    deadline=to_timestamp(0),
		    updated_at=$3,
		    failures = failures || $4::jsonb
		WHERE id=$1`, id, string(ev.ToState), now, string(failJSON)); err != nil {
		return fmt.Errorf("lease-end update: %w", err)
	}
	return insertEvent(ctx, tx, queue, ev, ev.RunID)
}

// insertEvent appends one event using the queue sequence (BIGSERIAL).
func insertEvent(ctx context.Context, tx *sql.Tx, queue string, ev kernel.Event, runID string) error {
	var failJSON any
	if ev.Failure != nil {
		b, err := json.Marshal(ev.Failure)
		if err != nil {
			return fmt.Errorf("encode event failure: %w", err)
		}
		failJSON = string(b)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO broker_events
		    (queue, message_id, type, version, run_id, occurred_at, to_state,
		     attempts, receipt, receipt_gen, deadline, visibility_to, extra, failure)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`,
		queue, ev.MessageID, string(ev.Type), ev.Version, runID, ev.At,
		string(ev.ToState), ev.Attempts, ev.Receipt, ev.ReceiptGen,
		ev.Deadline, int64(ev.VisibilityTO), int64(ev.Extra), failJSON,
	); err != nil {
		return fmt.Errorf("insert event: %w", err)
	}
	return nil
}

func scanMessage(row interface {
	Scan(dest ...any) error
}) (protocol.Message, error) {
	var (
		m     protocol.Message
		state string
		fails []byte
	)
	err := row.Scan(&m.ID, &m.Queue, &m.Body, &state, &m.Attempts,
		&m.Receipt, &m.ReceiptGen, &m.LastReceipt, &m.Deadline,
		&m.EnqueuedAt, &m.UpdatedAt, &fails)
	if err != nil {
		return protocol.Message{}, err
	}
	m.State = protocol.State(state)
	if len(fails) > 0 {
		if err := json.Unmarshal(fails, &m.Failures); err != nil {
			return protocol.Message{}, fmt.Errorf("decode failures: %w", err)
		}
	}
	return m, nil
}

const selectMessageCols = `
	SELECT id, queue, body, state, attempts, receipt, receipt_gen, last_receipt,
	       deadline, enqueued_at, updated_at, failures
	FROM broker_messages`

// Message returns live state by id.
func (s *Store) Message(ctx context.Context, queue, id string) (protocol.Message, error) {
	m, err := scanMessage(s.db.QueryRowContext(ctx,
		selectMessageCols+` WHERE queue=$1 AND id=$2`, queue, id))
	if errors.Is(err, sql.ErrNoRows) {
		return protocol.Message{}, kernel.ErrNoMessage
	}
	return m, err
}

func (s *Store) listWhere(ctx context.Context, where, orderTail string, queue string) ([]protocol.Message, error) {
	rows, err := s.db.QueryContext(ctx,
		selectMessageCols+" WHERE queue=$1 AND "+where+" ORDER BY id "+orderTail, queue)
	if err != nil {
		return nil, fmt.Errorf("list messages: %w", err)
	}
	defer rows.Close()
	var out []protocol.Message
	for rows.Next() {
		m, err := scanMessage(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// ListDead returns dead-lettered messages.
func (s *Store) ListDead(ctx context.Context, queue string) ([]protocol.Message, error) {
	return s.listWhere(ctx, "state='dead'", "ASC", queue)
}

// ListMessages returns all rows.
func (s *Store) ListMessages(ctx context.Context, queue string) ([]protocol.Message, error) {
	return s.listWhere(ctx, "1=1", "ASC", queue)
}

// Events reads the ordered event log and decodes embedded failures.
func (s *Store) Events(ctx context.Context, queue string) ([]kernel.Event, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT seq, message_id, type, version, COALESCE(run_id,''), occurred_at,
		       to_state, attempts, receipt, receipt_gen, deadline, visibility_to, extra,
		       COALESCE(failure::text, '')
		FROM broker_events WHERE queue=$1 ORDER BY seq`, queue)
	if err != nil {
		return nil, fmt.Errorf("select events: %w", err)
	}
	defer rows.Close()

	var out []kernel.Event
	for rows.Next() {
		var (
			ev        kernel.Event
			typ       string
			toState   string
			failJSON  string
			vis, extra int64
		)
		if err := rows.Scan(&ev.Seq, &ev.MessageID, &typ, &ev.Version, &ev.RunID, &ev.At,
			&toState, &ev.Attempts, &ev.Receipt, &ev.ReceiptGen, &ev.Deadline,
			&vis, &extra, &failJSON); err != nil {
			return nil, fmt.Errorf("scan event: %w", err)
		}
		ev.Type = kernel.EventType(typ)
		ev.ToState = protocol.State(toState)
		ev.VisibilityTO = time.Duration(vis)
		ev.Extra = time.Duration(extra)
		if failJSON != "" {
			var f protocol.Failure
			if err := json.Unmarshal([]byte(failJSON), &f); err != nil {
				return nil, fmt.Errorf("decode event failure: %w", err)
			}
			ev.Failure = &f
		}
		out = append(out, ev)
	}
	return out, rows.Err()
}
