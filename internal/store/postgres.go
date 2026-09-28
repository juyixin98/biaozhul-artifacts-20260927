package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"cbcast/internal/clock"
	"cbcast/internal/protocol"
)

// PostgresStore is a durable Store backed by PostgreSQL. All tables are
// prefixed so the implementation can share a database with unrelated services.
//
// Crash semantics: message delivery is committed in one transaction that flips
// row statuses, assigns delivery sequences and advances the delivered clock
// together. A committed clock can never reference predecessor rows that the
// same database does not also show as delivered.
type PostgresStore struct {
	pool    *pgxpool.Pool
	prefix  string
	members []string

	tblMessages string
	tblClock    string
	seqRecv     string
}

// NewPostgres opens a pool, runs idempotent migrations and returns a store.
func NewPostgres(ctx context.Context, dsn, prefix string, members []string) (*PostgresStore, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse postgres dsn: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect postgres: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	s := &PostgresStore{
		pool:        pool,
		prefix:      prefix,
		members:     append([]string(nil), members...),
		tblMessages: prefix + "_messages",
		tblClock:    prefix + "_clock",
		seqRecv:     prefix + "_recv_seq",
	}
	if err := s.migrate(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}

func (s *PostgresStore) migrate(ctx context.Context) error {
	stmts := []string{
		fmt.Sprintf(`CREATE SEQUENCE IF NOT EXISTS %s`, s.seqRecv),
		fmt.Sprintf(`CREATE TABLE IF NOT EXISTS %s (
			message_id   text PRIMARY KEY,
			sender       text NOT NULL,
			clock        jsonb NOT NULL,
			payload      bytea NOT NULL,
			payload_hash text NOT NULL,
			created_at   timestamptz NOT NULL,
			status       text NOT NULL,
			deliver_seq  bigint,
			received_seq bigint NOT NULL DEFAULT nextval('%s')
		)`, s.tblMessages, s.seqRecv),
		fmt.Sprintf(`CREATE UNIQUE INDEX IF NOT EXISTS %s_deliverseq_uniq
			ON %s(deliver_seq) WHERE deliver_seq IS NOT NULL`, s.prefix, s.tblMessages),
		fmt.Sprintf(`CREATE TABLE IF NOT EXISTS %s (
			id        int PRIMARY KEY CHECK (id = 1),
			del_clock jsonb NOT NULL,
			seq_hw    bigint NOT NULL
		)`, s.tblClock),
		fmt.Sprintf(`INSERT INTO %s (id, del_clock, seq_hw)
			VALUES (1, '{}'::jsonb, 0) ON CONFLICT DO NOTHING`, s.tblClock),
	}
	for _, q := range stmts {
		if _, err := s.pool.Exec(ctx, q); err != nil {
			return fmt.Errorf("migration failed (%s): %w", truncate(q, 70), err)
		}
	}
	return nil
}

func truncate(q string, n int) string {
	if len(q) <= n {
		return q
	}
	return q[:n] + "..."
}

// pgTxn implements DeliveryTxn on a pgx transaction with the clock row locked.
type pgTxn struct {
	s   *PostgresStore
	tx  pgx.Tx
	cur protocol.VC
	hw  uint64
}

func (s *PostgresStore) BeginDelivery(ctx context.Context) (DeliveryTxn, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin delivery txn: %w", err)
	}
	var rawClock []byte
	var hw uint64
	if err := tx.QueryRow(ctx,
		fmt.Sprintf(`SELECT del_clock, seq_hw FROM %s WHERE id = 1 FOR UPDATE`, s.tblClock),
	).Scan(&rawClock, &hw); err != nil {
		_ = tx.Rollback(ctx)
		return nil, fmt.Errorf("lock clock: %w", err)
	}
	cur := clock.New(s.members)
	if err := json.Unmarshal(rawClock, &cur); err != nil {
		_ = tx.Rollback(ctx)
		return nil, fmt.Errorf("decode delivered clock: %w", err)
	}
	return &pgTxn{s: s, tx: tx, cur: cur, hw: hw}, nil
}

func (t *pgTxn) CurrentClock() protocol.VC { return clock.Clone(t.cur) }

func (t *pgTxn) CommitDelivery(ordered []*protocol.Envelope, newClock protocol.VC) error {
	ctx := context.Background()
	for i, env := range ordered {
		ct, err := t.tx.Exec(ctx,
			fmt.Sprintf(`UPDATE %s SET status = $1, deliver_seq = $2
				WHERE message_id = $3 AND status = $4`, t.s.tblMessages),
			string(StatusDelivered), t.hw+uint64(i)+1, env.MessageID, string(StatusPending))
		if err != nil {
			_ = t.tx.Rollback(ctx)
			return fmt.Errorf("mark delivered %s: %w", env.MessageID, err)
		}
		if ct.RowsAffected() != 1 {
			_ = t.tx.Rollback(ctx)
			return errInvariant("commit delivery: %s not pending (rows=%d)", env.MessageID, ct.RowsAffected())
		}
	}
	rawClock, err := json.Marshal(newClock)
	if err != nil {
		_ = t.tx.Rollback(ctx)
		return err
	}
	if _, err := t.tx.Exec(ctx,
		fmt.Sprintf(`UPDATE %s SET del_clock = $1, seq_hw = $2 WHERE id = 1`, t.s.tblClock),
		rawClock, t.hw+uint64(len(ordered))); err != nil {
		_ = t.tx.Rollback(ctx)
		return fmt.Errorf("advance clock: %w", err)
	}
	if err := t.tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit delivery: %w", err)
	}
	return nil
}

func (t *pgTxn) Discard() { _ = t.tx.Rollback(context.Background()) }

func (s *PostgresStore) Get(ctx context.Context, messageID string) (*protocol.Envelope, Status, error) {
	row := s.pool.QueryRow(ctx,
		fmt.Sprintf(`SELECT sender, clock, payload, payload_hash, created_at, status
			FROM %s WHERE message_id = $1`, s.tblMessages), messageID)
	var (
		sender, hash, status string
		rawClock, payload    []byte
		createdAt            time.Time
	)
	err := row.Scan(&sender, &rawClock, &payload, &hash, &createdAt, &status)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, "", nil
	}
	if err != nil {
		return nil, "", err
	}
	vc := clock.New(s.members)
	if err := json.Unmarshal(rawClock, &vc); err != nil {
		return nil, "", fmt.Errorf("decode clock for %s: %w", messageID, err)
	}
	return &protocol.Envelope{
		MessageID:   messageID,
		Sender:      sender,
		Clock:       vc,
		Payload:     payload,
		PayloadHash: hash,
		CreatedAt:   createdAt,
	}, Status(status), nil
}

func (s *PostgresStore) Put(ctx context.Context, env *protocol.Envelope) (PutOutcome, error) {
	rawClock, err := json.Marshal(env.Clock)
	if err != nil {
		return 0, err
	}
	ct, err := s.pool.Exec(ctx,
		fmt.Sprintf(`INSERT INTO %s (message_id, sender, clock, payload, payload_hash, created_at, status)
			VALUES ($1, $2, $3, $4, $5, $6, $7)
			ON CONFLICT (message_id) DO NOTHING`, s.tblMessages),
		env.MessageID, env.Sender, rawClock, env.Payload, env.PayloadHash, env.CreatedAt, string(StatusPending))
	if err != nil {
		return 0, fmt.Errorf("insert %s: %w", env.MessageID, err)
	}
	if ct.RowsAffected() == 1 {
		return PutInserted, nil
	}
	var existingHash string
	if err := s.pool.QueryRow(ctx,
		fmt.Sprintf(`SELECT payload_hash FROM %s WHERE message_id = $1`, s.tblMessages),
		env.MessageID).Scan(&existingHash); err != nil {
		return 0, fmt.Errorf("re-read %s: %w", env.MessageID, err)
	}
	if existingHash == env.PayloadHash {
		return PutDuplicate, nil
	}
	return PutConflict, nil
}

func (s *PostgresStore) ListPending(ctx context.Context) ([]*protocol.Envelope, error) {
	rows, err := s.pool.Query(ctx,
		fmt.Sprintf(`SELECT message_id, sender, clock, payload, payload_hash, created_at
			FROM %s WHERE status = $1 ORDER BY received_seq`, s.tblMessages), string(StatusPending))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEnvelopes(rows, s.members)
}

func (s *PostgresStore) ListDeliveredSince(ctx context.Context, sinceSeq uint64) ([]*protocol.Envelope, error) {
	rows, err := s.pool.Query(ctx,
		fmt.Sprintf(`SELECT message_id, sender, clock, payload, payload_hash, created_at
			FROM %s WHERE status = $1 AND deliver_seq > $2 ORDER BY deliver_seq`, s.tblMessages),
		string(StatusDelivered), sinceSeq)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEnvelopes(rows, s.members)
}

type rowScanner interface {
	Next() bool
	Scan(dest ...any) error
	Err() error
}

func scanEnvelopes(rows rowScanner, members []string) ([]*protocol.Envelope, error) {
	var out []*protocol.Envelope
	for rows.Next() {
		var (
			id, sender, hash     string
			rawClock, payload    []byte
			createdAt            time.Time
		)
		if err := rows.Scan(&id, &sender, &rawClock, &payload, &hash, &createdAt); err != nil {
			return nil, err
		}
		vc := clock.New(members)
		if err := json.Unmarshal(rawClock, &vc); err != nil {
			return nil, fmt.Errorf("decode clock for %s: %w", id, err)
		}
		out = append(out, &protocol.Envelope{
			MessageID:   id,
			Sender:      sender,
			Clock:       vc,
			Payload:     payload,
			PayloadHash: hash,
			CreatedAt:   createdAt,
		})
	}
	return out, rows.Err()
}

func (s *PostgresStore) State(ctx context.Context) (State, error) {
	var rawClock []byte
	var st State
	if err := s.pool.QueryRow(ctx,
		fmt.Sprintf(`SELECT del_clock, seq_hw FROM %s WHERE id = 1`, s.tblClock),
	).Scan(&rawClock, &st.DeliverSeq); err != nil {
		return State{}, err
	}
	st.DeliveredClock = clock.New(s.members)
	if err := json.Unmarshal(rawClock, &st.DeliveredClock); err != nil {
		return State{}, fmt.Errorf("decode delivered clock: %w", err)
	}
	return st, nil
}

func (s *PostgresStore) Close() error {
	s.pool.Close()
	return nil
}
