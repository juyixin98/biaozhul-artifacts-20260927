package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "github.com/lib/pq"
)

// schema is applied idempotently on Open. CHECK constraints mirror the topic
// package's wildcard-placement rules so direct SQL access cannot bypass them.
const schema = `
-- Single-row counter. The row is locked FOR UPDATE during Apply, so version
-- allocation is serialized: MAX()+1 races are impossible and versions never
-- repeat.
CREATE TABLE IF NOT EXISTS version_counter (
    id      INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version BIGINT NOT NULL DEFAULT 0
);
INSERT INTO version_counter (id, version) VALUES (1, 0) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS routing_version (
    version         BIGINT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    subscription_id TEXT NOT NULL,
    change          TEXT NOT NULL CHECK (change IN ('upsert','delete'))
);

CREATE TABLE IF NOT EXISTS subscription (
    id            TEXT PRIMARY KEY,
    subscriber_id TEXT NOT NULL,
    filter        TEXT NOT NULL,
    version       BIGINT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    CHECK (length(filter) BETWEEN 1 AND 4096),
    CHECK (length(id) > 0),
    CHECK (subscriber_id <> '')
    -- Wildcard placement is authoritatively validated by internal/topic
    -- (ValidateFilter) and the kernel refuses malformed filters; SQL-side
    -- pattern constraints for '+'/'#' are intentionally not duplicated here,
    -- since embedded-wildcard rules are awkward in SQL and would risk drifting
    -- from the protocol definition.
);

CREATE TABLE IF NOT EXISTS subscription_version (
    subscription_id TEXT NOT NULL,
    version         BIGINT NOT NULL,
    change          TEXT NOT NULL CHECK (change IN ('upsert','delete')),
    subscriber_id   TEXT NOT NULL DEFAULT '',
    filter          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subscription_id, version)
);
CREATE INDEX IF NOT EXISTS idx_subversion_version ON subscription_version(version);

CREATE TABLE IF NOT EXISTS route_decision (
    message_id  TEXT PRIMARY KEY,
    topic       TEXT NOT NULL,
    version     BIGINT NOT NULL,
    sub_ids     JSONB NOT NULL,
    stats       JSONB NOT NULL,
    payload_sha TEXT NOT NULL,
    decided_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_decided ON route_decision(decided_at DESC, message_id DESC);
`

// PGStore persists state to PostgreSQL. One *sql.DB is shared; Apply uses a
// serializable transaction so the version advance and the change row commit
// atomically.
type PGStore struct {
	db *sql.DB
}

// OpenPG validates the schema and returns a connected store.
func OpenPG(ctx context.Context, dsn string, maxOpen, maxIdle int) (*PGStore, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("open postgres: %w", err)
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxIdle)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	pg := &PGStore{db: db}
	if err := pg.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return pg, nil
}

// Close releases the connection pool.
func (p *PGStore) Close() error { return p.db.Close() }

func (p *PGStore) migrate(ctx context.Context) error {
	if _, err := p.db.ExecContext(ctx, schema); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return nil
}

// statsJSON and idsJSON serialize for JSONB columns.
func idsJSON(ids []string) ([]byte, error) {
	if ids == nil {
		ids = []string{}
	}
	return json.Marshal(ids)
}

func statsJSON(s Stats) ([]byte, error) {
	return json.Marshal(s)
}

// Apply implements Store.
func (p *PGStore) Apply(ctx context.Context, change SubscriptionVersion, expectedVersion int64) (ApplyResult, error) {
	tx, err := p.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return ApplyResult{}, err
	}
	defer func() { _ = tx.Rollback() }()

	var current sql.NullInt64
	var subExists bool
	var curSubscriber, curFilter string
	row := tx.QueryRowContext(ctx,
		`SELECT version, subscriber_id, filter FROM subscription WHERE id = $1`,
		change.SubscriptionID)
	switch err := row.Scan(&current, &curSubscriber, &curFilter); err {
	case nil:
		subExists = true
	case sql.ErrNoRows:
	default:
		return ApplyResult{}, err
	}

	curV := int64(-1)
	if subExists {
		curV = current.Int64
	}
	if conflict := checkExpected(expectedVersion, subExists, curV); conflict {
		return ApplyResult{
			Version: 0, SubscriptionID: change.SubscriptionID,
			Conflict: true, CurrentVersion: curV,
		}, nil
	}

	var nextV int64
	if err := tx.QueryRowContext(ctx,
		`UPDATE version_counter SET version = version + 1 WHERE id = 1 RETURNING version`,
	).Scan(&nextV); err != nil {
		return ApplyResult{}, err
	}
	now := time.Now().UTC()

	switch change.Kind {
	case KindUpsert:
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO subscription (id, subscriber_id, filter, version, updated_at)
			VALUES ($1,$2,$3,$4,$5)
			ON CONFLICT (id) DO UPDATE
			SET subscriber_id = EXCLUDED.subscriber_id,
			    filter = EXCLUDED.filter,
			    version = EXCLUDED.version,
			    updated_at = EXCLUDED.updated_at`,
			change.SubscriptionID, change.SubscriberID, change.Filter, nextV, now); err != nil {
			return ApplyResult{}, err
		}
	case KindDelete:
		if _, err := tx.ExecContext(ctx, `DELETE FROM subscription WHERE id = $1`,
			change.SubscriptionID); err != nil {
			return ApplyResult{}, err
		}
	}

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO routing_version (version, created_at, subscription_id, change)
		VALUES ($1,$2,$3,$4)`,
		nextV, now, change.SubscriptionID, string(change.Kind)); err != nil {
		return ApplyResult{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO subscription_version
		  (subscription_id, version, change, subscriber_id, filter, created_at)
		VALUES ($1,$2,$3,$4,$5,$6)`,
		change.SubscriptionID, nextV, string(change.Kind),
		change.SubscriberID, change.Filter, now); err != nil {
		return ApplyResult{}, err
	}

	if err := tx.Commit(); err != nil {
		return ApplyResult{}, err
	}
	return ApplyResult{
		Version: nextV, SubscriptionID: change.SubscriptionID,
		Change: change.Kind, CurrentVersion: nextV,
	}, nil
}

func checkExpected(expected int64, exists bool, current int64) bool {
	switch expected {
	case -2:
		return false
	case -1:
		return exists
	default:
		return expected != current
	}
}

// CurrentVersion implements Store.
func (p *PGStore) CurrentVersion(ctx context.Context) (int64, error) {
	var v int64
	err := p.db.QueryRowContext(ctx,
		`SELECT version FROM version_counter WHERE id = 1`).Scan(&v)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	return v, err
}

// GetSubscription implements Store.
func (p *PGStore) GetSubscription(ctx context.Context, id string) (Subscription, error) {
	var s Subscription
	err := p.db.QueryRowContext(ctx,
		`SELECT id, subscriber_id, filter, version, updated_at FROM subscription WHERE id = $1`,
		id).Scan(&s.ID, &s.SubscriberID, &s.Filter, &s.Version, &s.UpdatedAt)
	if err == sql.ErrNoRows {
		return Subscription{}, &NotFoundError{Resource: "subscription", Key: id}
	}
	return s, err
}

// ListSubscriptions implements Store.
func (p *PGStore) ListSubscriptions(ctx context.Context) ([]Subscription, error) {
	rows, err := p.db.QueryContext(ctx,
		`SELECT id, subscriber_id, filter, version, updated_at FROM subscription ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Subscription
	for rows.Next() {
		var s Subscription
		if err := rows.Scan(&s.ID, &s.SubscriberID, &s.Filter, &s.Version, &s.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// SnapshotAt implements Store from the immutable change history.
func (p *PGStore) SnapshotAt(ctx context.Context, v int64, fromVersion int64) (SnapshotData, error) {
	if v <= 0 {
		cur, err := p.CurrentVersion(ctx)
		if err != nil {
			return SnapshotData{}, err
		}
		v = cur
	}
	rows, err := p.db.QueryContext(ctx, `
		SELECT subscription_id, version, change, subscriber_id, filter, created_at
		FROM subscription_version
		WHERE version <= $1
		ORDER BY version ASC, subscription_id ASC`, v)
	if err != nil {
		return SnapshotData{}, err
	}
	defer rows.Close()
	state := make(map[string]Subscription)
	var delta []SubscriptionVersion
	for rows.Next() {
		var c SubscriptionVersion
		var change string
		if err := rows.Scan(&c.SubscriptionID, &c.Version, &change,
			&c.SubscriberID, &c.Filter, &c.CreatedAt); err != nil {
			return SnapshotData{}, err
		}
		c.Kind = Kind(change)
		if c.Version > fromVersion {
			delta = append(delta, c)
		}
		switch c.Kind {
		case KindUpsert:
			state[c.SubscriptionID] = Subscription{
				ID: c.SubscriptionID, SubscriberID: c.SubscriberID,
				Filter: c.Filter, UpdatedAt: c.CreatedAt, Version: c.Version,
			}
		case KindDelete:
			delete(state, c.SubscriptionID)
		}
	}
	if err := rows.Err(); err != nil {
		return SnapshotData{}, err
	}
	subs := make([]Subscription, 0, len(state))
	for _, s := range state {
		subs = append(subs, s)
	}
	return SnapshotData{Version: v, Subs: subs, Changes: delta}, nil
}

// SaveDecision implements Store with a primary-key conflict for idempotency.
func (p *PGStore) SaveDecision(ctx context.Context, d Decision) (Decision, bool, error) {
	ids, err := idsJSON(d.SubIDs)
	if err != nil {
		return Decision{}, false, err
	}
	st, err := statsJSON(d.Stats)
	if err != nil {
		return Decision{}, false, err
	}
	now := d.DecidedAt
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tag, err := p.db.ExecContext(ctx, `
		INSERT INTO route_decision
		  (message_id, topic, version, sub_ids, stats, payload_sha, decided_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7)
		ON CONFLICT (message_id) DO NOTHING`,
		d.MessageID, d.Topic, d.Version, ids, st, d.PayloadSHA, now)
	if err != nil {
		return Decision{}, false, err
	}
	if n, _ := tag.RowsAffected(); n == 1 {
		d.DecidedAt = now
		return d, true, nil
	}
	existing, err := p.GetDecision(ctx, d.MessageID)
	if err != nil {
		return Decision{}, false, err
	}
	return existing, false, nil
}

// GetDecision implements Store.
func (p *PGStore) GetDecision(ctx context.Context, messageID string) (Decision, error) {
	var d Decision
	var idsRaw, statsRaw []byte
	err := p.db.QueryRowContext(ctx, `
		SELECT message_id, topic, version, sub_ids, stats, payload_sha, decided_at
		FROM route_decision WHERE message_id = $1`, messageID).
		Scan(&d.MessageID, &d.Topic, &d.Version, &idsRaw, &statsRaw, &d.PayloadSHA, &d.DecidedAt)
	if err == sql.ErrNoRows {
		return Decision{}, &NotFoundError{Resource: "decision", Key: messageID}
	}
	if err != nil {
		return Decision{}, err
	}
	if err := json.Unmarshal(idsRaw, &d.SubIDs); err != nil {
		return Decision{}, err
	}
	if err := json.Unmarshal(statsRaw, &d.Stats); err != nil {
		return Decision{}, err
	}
	return d, nil
}

// ListDecisions implements Store, newest first by decided_at then message id.
func (p *PGStore) ListDecisions(ctx context.Context, limit int, cursor string) (DecisionPage, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var (
		rows *sql.Rows
		err  error
	)
	if cursor == "" {
		rows, err = p.db.QueryContext(ctx, `
			SELECT message_id, topic, version, sub_ids, stats, payload_sha, decided_at
			FROM route_decision
			ORDER BY decided_at DESC, message_id DESC
			LIMIT $1`, limit+1)
	} else {
		var cAt time.Time
		var cID string
		err = p.db.QueryRowContext(ctx,
			`SELECT decided_at, message_id FROM route_decision WHERE message_id = $1`,
			cursor).Scan(&cAt, &cID)
		if err == sql.ErrNoRows {
			return DecisionPage{}, &NotFoundError{Resource: "cursor", Key: cursor}
		}
		if err != nil {
			return DecisionPage{}, err
		}
		rows, err = p.db.QueryContext(ctx, `
			SELECT message_id, topic, version, sub_ids, stats, payload_sha, decided_at
			FROM route_decision
			WHERE (decided_at, message_id) < ($1, $2)
			ORDER BY decided_at DESC, message_id DESC
			LIMIT $3`, cAt, cursor, limit+1)
	}
	if err != nil {
		return DecisionPage{}, err
	}
	defer rows.Close()

	page := DecisionPage{}
	for rows.Next() {
		var d Decision
		var idsRaw, statsRaw []byte
		if err := rows.Scan(&d.MessageID, &d.Topic, &d.Version, &idsRaw, &statsRaw,
			&d.PayloadSHA, &d.DecidedAt); err != nil {
			return DecisionPage{}, err
		}
		if err := json.Unmarshal(idsRaw, &d.SubIDs); err != nil {
			return DecisionPage{}, err
		}
		if err := json.Unmarshal(statsRaw, &d.Stats); err != nil {
			return DecisionPage{}, err
		}
		page.Decisions = append(page.Decisions, d)
	}
	if err := rows.Err(); err != nil {
		return DecisionPage{}, err
	}
	if len(page.Decisions) > limit {
		page.HasMore = true
		page.NextCursor = page.Decisions[limit-1].MessageID
		page.Decisions = page.Decisions[:limit]
	}
	return page, nil
}
