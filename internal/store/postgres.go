package store

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/lib/pq"

	"workbroker/internal/clock"
	"workbroker/internal/protocol"
)

//go:embed postgres_schema.sql
var schemaSQL string

// Postgres is the SQL Store. All state transitions run as one
// READ COMMITTED transaction that locks the rows it touches with
// SELECT ... FOR UPDATE; the kernel functions decide the mutation while the
// rows are locked, which is what makes extend/ack vs visibility timeout
// atomic under concurrency.
type Postgres struct {
	db  *sql.DB
	clk clock.Clock
}

// OpenPostgres connects, applies migrations and returns the store.
func OpenPostgres(ctx context.Context, dsn string, clk clock.Clock) (*Postgres, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("open postgres: %w", err)
	}
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return &Postgres{db: db, clk: clk}, nil
}

// NewPostgres wraps an existing *sql.DB (tests) after migrations were applied.
func NewPostgres(db *sql.DB, clk clock.Clock) *Postgres {
	return &Postgres{db: db, clk: clk}
}

// DB exposes the handle for test setup/teardown.
func (p *Postgres) DB() *sql.DB { return p.db }

func (p *Postgres) Close() error { return p.db.Close() }

// mapErr converts driver-level serialization/lock conflicts so callers can
// classify them instead of seeing an opaque error.
func mapErr(op string, err error) error {
	if err == nil {
		return nil
	}
	var pe *pq.Error
	if errors.As(err, &pe) {
		switch pe.Code {
		case "40001", "40P01": // serialization failure / deadlock detected
			return protocol.NewFailure(op, protocol.FailConflict, "transaction conflict, retryable", err)
		}
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return protocol.NewFailure(op, protocol.FailTimeout, "operation canceled or timed out", err)
	}
	return protocol.NewFailure(op, protocol.FailInternal, err.Error(), err)
}

var _ = mapErr // retained for the transaction helpers below

// ---------------------------------------------------------------------------
// row mapping
// ---------------------------------------------------------------------------

func scanMessage(row interface {
	Scan(...any) error
}) (*protocol.Message, error) {
	var m protocol.Message
	var status string
	var inflightAt, deadline sql.NullTime
	var receiptID, workerID sql.NullString
	if err := row.Scan(
		&m.ID, &m.Partition, &m.Body, &status,
		&m.Attempts, &m.MaxAttempts,
		&m.AvailableAt, &inflightAt, &deadline, &receiptID, &workerID,
		&m.CreatedAt, &m.UpdatedAt,
	); err != nil {
		return nil, err
	}
	m.Status = protocol.Status(status)
	if inflightAt.Valid {
		m.InFlightAt = inflightAt.Time
	}
	if deadline.Valid {
		m.VisibilityDeadline = deadline.Time
	}
	if receiptID.Valid {
		m.ReceiptID = receiptID.String
	}
	if workerID.Valid {
		m.WorkerID = workerID.String
	}
	return &m, nil
}

const messageColumns = `id, partition_key, body, status, attempts, max_attempts,
	available_at, inflight_at, visibility_deadline, receipt_id, worker_id,
	created_at, updated_at`

func scanReceipt(row interface {
	Scan(...any) error
}) (*protocol.Receipt, error) {
	var r protocol.Receipt
	var extendedAt sql.NullTime
	if err := row.Scan(&r.ID, &r.MessageID, &r.WorkerID, &r.IssuedAt,
		&r.ExpiresAt, &extendedAt, &r.Consumed); err != nil {
		return nil, err
	}
	if extendedAt.Valid {
		r.ExtendedAt = extendedAt.Time
	}
	return &r, nil
}

const receiptColumns = `id, message_id, worker_id, issued_at, expires_at, extended_at, consumed`

// ---------------------------------------------------------------------------
// persistence helpers (run inside a transaction with locked rows)
// ---------------------------------------------------------------------------

func insertMessageTx(ctx context.Context, tx *sql.Tx, m *protocol.Message) error {
	var inflightAt, deadline, receiptID, workerID any
	if !m.InFlightAt.IsZero() {
		inflightAt = m.InFlightAt
	}
	if !m.VisibilityDeadline.IsZero() {
		deadline = m.VisibilityDeadline
	}
	if m.ReceiptID != "" {
		receiptID = m.ReceiptID
	}
	if m.WorkerID != "" {
		workerID = m.WorkerID
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO messages (`+messageColumns+`)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
		m.ID, m.Partition, m.Body, string(m.Status),
		m.Attempts, m.MaxAttempts,
		m.AvailableAt, inflightAt, deadline, receiptID, workerID,
		m.CreatedAt, m.UpdatedAt)
	return err
}

func updateMessageTx(ctx context.Context, tx *sql.Tx, m *protocol.Message) error {
	var inflightAt, deadline, receiptID, workerID any
	if !m.InFlightAt.IsZero() {
		inflightAt = m.InFlightAt
	}
	if !m.VisibilityDeadline.IsZero() {
		deadline = m.VisibilityDeadline
	}
	if m.ReceiptID != "" {
		receiptID = m.ReceiptID
	}
	if m.WorkerID != "" {
		workerID = m.WorkerID
	}
	// dead_reason is written separately via setDeadReasonTx; this update
	// deliberately leaves that column untouched.
	_, err := tx.ExecContext(ctx, `
		UPDATE messages SET
			status=$2, attempts=$3, max_attempts=$4, available_at=$5,
			inflight_at=$6, visibility_deadline=$7, receipt_id=$8,
			worker_id=$9, updated_at=$10
		WHERE id=$1`,
		m.ID, string(m.Status), m.Attempts, m.MaxAttempts, m.AvailableAt,
		inflightAt, deadline, receiptID, workerID, m.UpdatedAt)
	return err
}

func setDeadReasonTx(ctx context.Context, tx *sql.Tx, m *protocol.Message, dead *protocol.DeadReason) error {
	if dead == nil {
		return nil
	}
	payload, err := json.Marshal(dead)
	if err != nil {
		return fmt.Errorf("marshal dead reason: %w", err)
	}
	_, err = tx.ExecContext(ctx,
		`UPDATE messages SET dead_reason=$2 WHERE id=$1`, m.ID, payload)
	return err
}

func insertReceiptTx(ctx context.Context, tx *sql.Tx, r *protocol.Receipt) error {
	var extendedAt any
	if !r.ExtendedAt.IsZero() {
		extendedAt = r.ExtendedAt
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO receipts (`+receiptColumns+`)
		VALUES ($1,$2,$3,$4,$5,$6,$7)`,
		r.ID, r.MessageID, r.WorkerID, r.IssuedAt, r.ExpiresAt, extendedAt, r.Consumed)
	return err
}

func updateReceiptTx(ctx context.Context, tx *sql.Tx, r *protocol.Receipt) error {
	var extendedAt any
	if !r.ExtendedAt.IsZero() {
		extendedAt = r.ExtendedAt
	}
	_, err := tx.ExecContext(ctx, `
		UPDATE receipts SET expires_at=$2, extended_at=$3, consumed=$4
		WHERE id=$1`,
		r.ID, r.ExpiresAt, extendedAt, r.Consumed)
	return err
}

func insertFailureTx(ctx context.Context, tx *sql.Tx, f *protocol.AttemptFailure) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO attempt_failures
			(id, message_id, attempt, receipt_id, worker_id, cause, reason, happened_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		f.ID, f.MessageID, f.Attempt, f.ReceiptID, f.WorkerID,
		string(f.Class), f.Reason, f.HappenedAt)
	return err
}

// insertEventTx appends an event and populates its database-assigned seq.
func insertEventTx(ctx context.Context, tx *sql.Tx, ev *protocol.Event) error {
	data := ev.Data
	if len(data) == 0 {
		data = []byte(`{}`)
	}
	return tx.QueryRowContext(ctx, `
		INSERT INTO events
			(event_type, message_id, partition_key, receipt_id, worker_id,
			 occurred_at, attempt, class, reason, version, data)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
		RETURNING seq`,
		string(ev.Type), ev.MessageID, ev.Partition, ev.ReceiptID, ev.WorkerID,
		ev.At, ev.Attempt, string(ev.Class), ev.Reason, ev.Version, data,
	).Scan(&ev.Seq)
}

// loadPriorFailuresTx returns the complete attempt history of a message.
func loadPriorFailuresTx(ctx context.Context, tx *sql.Tx, messageID string) ([]protocol.AttemptFailure, error) {
	rows, err := tx.QueryContext(ctx, `
		SELECT id, message_id, attempt, receipt_id, worker_id, cause, reason, happened_at
		FROM attempt_failures WHERE message_id=$1 ORDER BY happened_at, id`, messageID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []protocol.AttemptFailure
	for rows.Next() {
		var f protocol.AttemptFailure
		var cause string
		if err := rows.Scan(&f.ID, &f.MessageID, &f.Attempt, &f.ReceiptID, &f.WorkerID,
			&cause, &f.Reason, &f.HappenedAt); err != nil {
			return nil, err
		}
		f.Class = protocol.AttemptCause(cause)
		out = append(out, f)
	}
	return out, rows.Err()
}

// newerReceiptCountTx counts receipts issued strictly after rcp.IssuedAt.
func newerReceiptCountTx(ctx context.Context, tx *sql.Tx, rcp *protocol.Receipt) (int, error) {
	var n int
	err := tx.QueryRowContext(ctx,
		`SELECT count(*) FROM receipts WHERE message_id=$1 AND issued_at > $2`,
		rcp.MessageID, rcp.IssuedAt).Scan(&n)
	return n, err
}
