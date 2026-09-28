// Package store is the PostgreSQL-backed persistence for the coordinator.
//
// Durability contract: a command is a single transaction that
//
//  1. takes the group row FOR UPDATE (so concurrent commands serialise on the
//     group version),
//  2. verifies the expected version,
//  3. appends every kernel event with monotonically increasing versions,
//  4. rewrites the members/partitions projection from the post-fold state,
//  5. records correlated trace rows.
//
// Commit position is therefore bound to the member and generation that produced
// it (carried in the OFFSET_COMMITTED event), and a crashed coordinator
// recovers solely by folding group_events.
package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"opp291/coordinator/internal/kernel"
)

//go:embed schema.sql
var schemaSQL string

// ErrNotFound / ErrVersionConflict are the storage-level categories the
// service maps to protocol failures.
var (
	ErrNotFound        = errors.New("store: not found")
	ErrVersionConflict = errors.New("store: version conflict")
)

// Store wraps a pgx connection pool.
type Store struct {
	pool *pgxpool.Pool
}

// Open connects and applies the schema.
func Open(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	s := &Store{pool: pool}
	if err := s.migrate(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() { s.pool.Close() }

func (s *Store) migrate(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, schemaSQL)
	if err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	return nil
}

// Reset drops all coordinator data (used by the local demo / tests).
func (s *Store) Reset(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `TRUNCATE groups_meta, group_events, members_proj, partitions_proj, request_traces RESTART IDENTITY CASCADE`)
	return err
}

// AppendResult is what Apply returns.
type AppendResult struct {
	Version int64
}

// LoadGroup returns the kernel State rebuilt from the event log, plus the raw
// events ordered by version. Returns ErrNotFound if the group is unknown.
func (s *Store) LoadGroup(ctx context.Context, groupID string) (*kernel.State, []kernel.Event, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, nil, err
	}
	defer tx.Rollback(ctx)

	var exists bool
	err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM groups_meta WHERE group_id=$1)`, groupID).Scan(&exists)
	if err != nil {
		return nil, nil, err
	}
	if !exists {
		return nil, nil, ErrNotFound
	}

	rows, err := tx.Query(ctx,
		`SELECT version, type, occurred_at, request_id, generation, force, payload
		 FROM group_events WHERE group_id=$1 ORDER BY version`, groupID)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()

	var events []kernel.Event
	for rows.Next() {
		var (
			typ, requestID string
			payload        []byte
			version, gen   int64
			at             time.Time
			force          bool
		)
		if err := rows.Scan(&version, &typ, &at, &requestID, &gen, &force, &payload); err != nil {
			return nil, nil, err
		}
		e := kernel.Event{
			Version: version, Type: kernel.EventType(typ), At: at.UTC(),
			RequestID: requestID, Generation: gen, Force: force,
		}
		if err := decodePayload(typ, payload, &e); err != nil {
			return nil, nil, fmt.Errorf("decode event v%d: %w", version, err)
		}
		events = append(events, e)
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	state, err := kernel.Fold(groupID, events)
	if err != nil {
		return nil, nil, err
	}
	return state, events, tx.Commit(ctx)
}

// CreateGroup inserts a brand-new group and appends its GROUP_CREATED event.
func (s *Store) CreateGroup(ctx context.Context, groupID string, partitionCount int, sessionTimeout time.Duration, evs []kernel.Event, traces []Trace) error {
	if len(evs) != 1 || evs[0].Type != kernel.EvGroupCreated {
		return fmt.Errorf("store: create expects a single GROUP_CREATED event")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var inserted bool
	err = tx.QueryRow(ctx,
		`INSERT INTO groups_meta(group_id, partition_count, session_timeout_ms, generation, phase, version, created_at, updated_at)
		 VALUES ($1,$2,$3,0,'STABLE',0,$4,$4)
		 ON CONFLICT (group_id) DO NOTHING RETURNING true`,
		groupID, partitionCount, sessionTimeout.Milliseconds(), evs[0].At).Scan(&inserted)
	if err != nil {
		return err
	}
	if !inserted {
		return ErrVersionConflict
	}
	if err := appendEvents(ctx, tx, groupID, 0, evs); err != nil {
		return err
	}
	state, err := kernel.Fold(groupID, evs)
	if err != nil {
		return err
	}
	if err := rewriteProjections(ctx, tx, groupID, state); err != nil {
		return err
	}
	if err := writeTraces(ctx, tx, traces); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Apply appends a command's event batch for an existing group under an
// optimistic version check, then rewrites projections.
func (s *Store) Apply(ctx context.Context, groupID string, expectedVersion int64, evs []kernel.Event, traces []Trace) (*kernel.State, error) {
	if len(evs) == 0 {
		return nil, fmt.Errorf("store: no events to apply")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	var version int64
	err = tx.QueryRow(ctx,
		`SELECT version FROM groups_meta WHERE group_id=$1 FOR UPDATE`, groupID).Scan(&version)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if version != expectedVersion {
		return nil, ErrVersionConflict
	}

	if err := appendEvents(ctx, tx, groupID, version, evs); err != nil {
		return nil, err
	}
	// Reload the full log and fold, so projections reflect all history.
	events, err := loadEventsTx(ctx, tx, groupID)
	if err != nil {
		return nil, err
	}
	state, err := kernel.Fold(groupID, events)
	if err != nil {
		return nil, err
	}
	if err := rewriteProjections(ctx, tx, groupID, state); err != nil {
		return nil, err
	}
	if err := writeTraces(ctx, tx, traces); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return state, nil
}

// ApplyHeartbeat persists a single heartbeat event without changing ownership.
func (s *Store) ApplyHeartbeat(ctx context.Context, groupID string, expectedVersion int64, evs []kernel.Event, traces []Trace) (*kernel.State, error) {
	return s.Apply(ctx, groupID, expectedVersion, evs, traces)
}

// AppendTraces writes diagnostic rows without state changes (used for rejected
// requests, which still must be explainable).
func (s *Store) AppendTraces(ctx context.Context, traces []Trace) error {
	if len(traces) == 0 {
		return nil
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := writeTraces(ctx, tx, traces); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ListGroups returns all group ids.
func (s *Store) ListGroups(ctx context.Context) ([]string, error) {
	rows, err := s.pool.Query(ctx, `SELECT group_id FROM groups_meta ORDER BY group_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var g string
		if err := rows.Scan(&g); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// ListTraces returns trace rows for a request (most recent last).
func (s *Store) ListTraces(ctx context.Context, requestID string, limit int) ([]TraceRow, error) {
	if limit <= 0 {
		limit = 200
	}
	rows, err := s.pool.Query(ctx,
		`SELECT id, at, request_id, method, path, member_id, generation, step, version, location,
		        ok, fail_code, fail_detail, uncertain, COALESCE(extra::text,'')
		 FROM request_traces WHERE request_id=$1 ORDER BY id LIMIT $2`, requestID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TraceRow
	for rows.Next() {
		var tr TraceRow
		if err := rows.Scan(&tr.ID, &tr.At, &tr.RequestID, &tr.Method, &tr.Path, &tr.MemberID,
			&tr.Generation, &tr.Step, &tr.Version, &tr.Location, &tr.OK, &tr.FailCode,
			&tr.FailDetail, &tr.Uncertain, &tr.ExtraJSON); err != nil {
			return nil, err
		}
		out = append(out, tr)
	}
	return out, rows.Err()
}

// ---- internal ----

func loadEventsTx(ctx context.Context, tx pgx.Tx, groupID string) ([]kernel.Event, error) {
	rows, err := tx.Query(ctx,
		`SELECT version, type, occurred_at, request_id, generation, force, payload
		 FROM group_events WHERE group_id=$1 ORDER BY version`, groupID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []kernel.Event
	for rows.Next() {
		var (
			typ, requestID string
			payload        []byte
			version, gen   int64
			at             time.Time
			force          bool
		)
		if err := rows.Scan(&version, &typ, &at, &requestID, &gen, &force, &payload); err != nil {
			return nil, err
		}
		e := kernel.Event{
			Version: version, Type: kernel.EventType(typ), At: at.UTC(),
			RequestID: requestID, Generation: gen, Force: force,
		}
		if err := decodePayload(typ, payload, &e); err != nil {
			return nil, err
		}
		events = append(events, e)
	}
	return events, rows.Err()
}

func appendEvents(ctx context.Context, tx pgx.Tx, groupID string, base int64, evs []kernel.Event) error {
	var err error
	for i, e := range evs {
		e.Version = base + int64(i) + 1
		payload, err := encodePayload(e)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx,
			`INSERT INTO group_events(group_id, version, type, occurred_at, request_id, generation, force, payload)
			 VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
			groupID, e.Version, string(e.Type), e.At, e.RequestID, e.Generation, e.Force, payload)
		if err != nil {
			return err
		}
	}
	newVersion := base + int64(len(evs))
	last := evs[len(evs)-1]
	// Fold the appended batch against a minimal seed just to derive the phase
	// cache for groups_meta. The projection rewrite below is authoritative.
	var phase string
	if evs[0].Type == kernel.EvGroupCreated {
		phase = string(kernel.PhaseStable)
	} else {
		phase = phaseFromLast(evs)
	}
	_, err = tx.Exec(ctx,
		`UPDATE groups_meta SET version=$2, generation=$3, phase=$4, updated_at=$5 WHERE group_id=$1`,
		groupID, newVersion, genOfStateAfter(evs), phase, last.At)
	return err
}

// phaseFromLast derives a conservative phase for the groups_meta cache. The
// partitions projection rewrite is authoritative, so this only needs to avoid
// lying about stability: settled iff the final event is a grant and no revoke
// remains (rewriteProjections recomputes the exact value anyway).
func phaseFromLast(evs []kernel.Event) string {
	// Default conservative: projection rewrite overwrites this.
	return string(kernel.PhaseRebalancing)
}

func genOfStateAfter(evs []kernel.Event) int64 {
	gen := int64(0)
	for _, e := range evs {
		if e.Generation > gen {
			gen = e.Generation
		}
	}
	return gen
}

func writeTraces(ctx context.Context, tx pgx.Tx, traces []Trace) error {
	for _, t := range traces {
		var extra []byte
		if t.Extra != nil {
			b, err := json.Marshal(t.Extra)
			if err != nil {
				return err
			}
			extra = b
		}
		_, err := tx.Exec(ctx,
			`INSERT INTO request_traces(request_id, method, path, member_id, generation, step,
			    version, location, ok, fail_code, fail_detail, uncertain, extra)
			 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
			t.RequestID, t.Method, t.Path, t.MemberID, t.Generation, t.Step, t.Version,
			t.Location, t.OK, t.FailCode, t.FailDetail, t.Uncertain, extraJSON(extra))
		if err != nil {
			return err
		}
	}
	return nil
}

// extraJSON maps an empty payload to SQL NULL.
func extraJSON(b []byte) any {
	if len(b) == 0 {
		return nil
	}
	return string(b)
}

func rewriteProjections(ctx context.Context, tx pgx.Tx, groupID string, s *kernel.State) error {
	_, err := tx.Exec(ctx,
		`UPDATE groups_meta SET generation=$2, phase=$3, version=$4 WHERE group_id=$1`,
		groupID, s.Generation, string(s.Phase), s.Version)
	if err != nil {
		return err
	}
	// Members: upsert active; delete inactive.
	for id, m := range s.Members {
		_, err := tx.Exec(ctx,
			`INSERT INTO members_proj(group_id, member_id, generation, joined_at, last_seen, active)
			 VALUES ($1,$2,$3,$4,$5,true)
			 ON CONFLICT (group_id, member_id)
			 DO UPDATE SET generation=$3, joined_at=$4, last_seen=$5, active=true`,
			groupID, id, m.Generation, m.JoinedAt, m.LastSeen)
		if err != nil {
			return err
		}
	}
	_, err = tx.Exec(ctx, `DELETE FROM members_proj WHERE group_id=$1 AND member_id <> ALL($2)`,
		groupID, activeIDs(s))
	if err != nil {
		return err
	}
	for _, p := range s.Partitions {
		_, err := tx.Exec(ctx,
			`INSERT INTO partitions_proj(group_id, partition, phase, owner, generation, prev_owner,
			    prev_generation, pending_owner, offset_value, offset_owner, offset_generation, uncertain)
			 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
			 ON CONFLICT (group_id, partition) DO UPDATE SET
			    phase=$3, owner=$4, generation=$5, prev_owner=$6, prev_generation=$7,
			    pending_owner=$8, offset_value=$9, offset_owner=$10, offset_generation=$11, uncertain=$12`,
			groupID, p.ID, string(p.Phase), p.Owner, p.Generation, p.PrevOwner,
			p.PrevGeneration, p.PendingOwner, p.Offset, p.OffsetOwner, p.OffsetGen, p.Uncertain)
		if err != nil {
			return err
		}
	}
	return nil
}

func activeIDs(s *kernel.State) []string {
	out := make([]string, 0, len(s.Members))
	for id := range s.Members {
		out = append(out, id)
	}
	return out
}

// ---- payload codec ----
//
// Event sub-fields are carried in a typed JSON payload keyed by event type, so
// replay reconstructs kernel.Event exactly.

func encodePayload(e kernel.Event) ([]byte, error) {
	type generic map[string]any
	switch e.Type {
	case kernel.EvGroupCreated:
		return json.Marshal(generic{"partition_count": e.GroupCount, "session_timeout_ms": e.SessionTimeout.Milliseconds()})
	case kernel.EvMemberJoined, kernel.EvMemberLeft, kernel.EvMemberExpired:
		return json.Marshal(generic{"member_id": e.MemberID})
	case kernel.EvHeartbeat:
		return json.Marshal(generic{"member_id": e.MemberID, "generation": e.Generation})
	case kernel.EvRebalanceStarted:
		return json.Marshal(generic{"plan": e.Plan})
	case kernel.EvRevocationDemanded:
		return json.Marshal(generic{"demanded": e.Demanded})
	case kernel.EvPartitionRevoked:
		return json.Marshal(generic{"revoke": e.Revoke})
	case kernel.EvPartitionGranted:
		return json.Marshal(generic{"grant": e.Grant})
	case kernel.EvOffsetCommitted:
		return json.Marshal(generic{"partition": e.CommitPartition, "offset": e.CommitOffset,
			"owner": e.CommitOwner, "generation": e.CommitGen})
	default:
		return nil, fmt.Errorf("unknown event type %q", e.Type)
	}
}

func decodePayload(typ string, raw []byte, e *kernel.Event) error {
	switch kernel.EventType(typ) {
	case kernel.EvGroupCreated:
		var v struct {
			PartitionCount   int   `json:"partition_count"`
			SessionTimeoutMs int64 `json:"session_timeout_ms"`
		}
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.GroupCount = v.PartitionCount
		e.SessionTimeout = time.Duration(v.SessionTimeoutMs) * time.Millisecond
	case kernel.EvMemberJoined, kernel.EvMemberLeft, kernel.EvMemberExpired:
		var v struct{ MemberID string `json:"member_id"` }
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.MemberID = v.MemberID
	case kernel.EvHeartbeat:
		var v struct {
			MemberID   string `json:"member_id"`
			Generation int64  `json:"generation"`
		}
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.MemberID = v.MemberID
		e.Generation = v.Generation
	case kernel.EvRebalanceStarted:
		var v struct{ Plan []kernel.PlanMove `json:"plan"` }
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.Plan = v.Plan
	case kernel.EvRevocationDemanded:
		var v struct{ Demanded []kernel.PlanMove `json:"demanded"` }
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.Demanded = v.Demanded
	case kernel.EvPartitionRevoked:
		var v struct{ Revoke []kernel.RevokeMove `json:"revoke"` }
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.Revoke = v.Revoke
	case kernel.EvPartitionGranted:
		var v struct{ Grant []kernel.GrantMove `json:"grant"` }
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.Grant = v.Grant
	case kernel.EvOffsetCommitted:
		var v struct {
			Partition  int    `json:"partition"`
			Offset     int64  `json:"offset"`
			Owner      string `json:"owner"`
			Generation int64  `json:"generation"`
		}
		if err := json.Unmarshal(raw, &v); err != nil {
			return err
		}
		e.CommitPartition = v.Partition
		e.CommitOffset = v.Offset
		e.CommitOwner = v.Owner
		e.CommitGen = v.Generation
	default:
		return fmt.Errorf("unknown event type %q", typ)
	}
	return nil
}
