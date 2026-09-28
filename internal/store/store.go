// Package store is the SQLite persistence adapter for clusters, running
// instances, placement plans and reconciliation runs.
//
// The store deliberately knows nothing about scheduling; it validates and
// persists records. Plans are committed transactionally together with their
// decision steps so that a plan and its reasoning can never diverge.
package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	_ "modernc.org/sqlite"

	"opp284/placement/internal/model"
)

// Store wraps a *sql.DB backed by SQLite (pure-Go modernc driver, no cgo).
type Store struct {
	db *sql.DB
}

// Open opens the DSN and ensures the schema exists. When reset is true the
// schema is dropped and re-created (test/demo convenience).
func Open(ctx context.Context, dsn string, reset bool) (*Store, error) {
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", dsn, err)
	}
	if _, err := db.ExecContext(ctx, "PRAGMA foreign_keys=ON"); err != nil {
		db.Close()
		return nil, fmt.Errorf("enable foreign keys: %w", err)
	}
	s := &Store{db: db}
	if reset {
		if err := s.drop(ctx); err != nil {
			db.Close()
			return nil, err
		}
	}
	if err := s.migrate(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) drop(ctx context.Context) error {
	stmts := []string{
		"DROP TABLE IF EXISTS plan_steps",
		"DROP TABLE IF EXISTS placements",
		"DROP TABLE IF EXISTS plans",
		"DROP TABLE IF EXISTS groups",
		"DROP TABLE IF EXISTS running_instances",
		"DROP TABLE IF EXISTS nodes",
		"DROP TABLE IF EXISTS clusters",
		"DROP TABLE IF EXISTS reconcile_runs",
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("reset: %w", err)
		}
	}
	return nil
}

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS clusters (
			id TEXT PRIMARY KEY,
			declared_zones TEXT NOT NULL DEFAULT '[]',
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
		`CREATE TABLE IF NOT EXISTS nodes (
			cluster_id TEXT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
			id TEXT NOT NULL,
			zone TEXT NOT NULL,
			capacity TEXT NOT NULL,
			used TEXT NOT NULL,
			labels TEXT NOT NULL DEFAULT '{}',
			eligible INTEGER NOT NULL,
			PRIMARY KEY (cluster_id, id)
		)`,
		`CREATE TABLE IF NOT EXISTS running_instances (
			cluster_id TEXT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
			id TEXT NOT NULL,
			node_id TEXT NOT NULL,
			request TEXT NOT NULL,
			groups TEXT NOT NULL DEFAULT '[]',
			PRIMARY KEY (cluster_id, id)
		)`,
		`CREATE TABLE IF NOT EXISTS groups (
			cluster_id TEXT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
			id TEXT NOT NULL,
			mode TEXT NOT NULL,
			member_ids TEXT NOT NULL,
			PRIMARY KEY (cluster_id, id)
		)`,
		`CREATE TABLE IF NOT EXISTS plans (
			cluster_id TEXT NOT NULL,
			plan_id TEXT PRIMARY KEY,
			request_id TEXT NOT NULL,
			status TEXT NOT NULL,
			exhaustive INTEGER NOT NULL,
			leaf_visits INTEGER NOT NULL,
			score TEXT NOT NULL,
			domains TEXT NOT NULL,
			failure TEXT NOT NULL DEFAULT '',
			allow_recreate INTEGER NOT NULL DEFAULT 0,
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
		`CREATE TABLE IF NOT EXISTS placements (
			plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
			instance_id TEXT NOT NULL,
			node_id TEXT NOT NULL,
			zone TEXT NOT NULL,
			replaces_id TEXT NOT NULL DEFAULT '',
			PRIMARY KEY (plan_id, instance_id)
		)`,
		`CREATE TABLE IF NOT EXISTS plan_steps (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
			seq INTEGER NOT NULL,
			payload TEXT NOT NULL
		)`,
		`CREATE TABLE IF NOT EXISTS reconcile_runs (
			run_id TEXT PRIMARY KEY,
			request_id TEXT NOT NULL,
			status TEXT NOT NULL,
			detail TEXT NOT NULL DEFAULT '',
			created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
		)`,
	}
	for _, q := range stmts {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("migrate: %w\n%s", err, q)
		}
	}
	return nil
}

// UpsertCluster persists a cluster definition (nodes/running/groups),
// replacing any prior definition with the same id.
func (s *Store) UpsertCluster(ctx context.Context, cid string, declaredZones []string,
	nodes []model.Node, running []RunningRecord, groups []model.Group) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `INSERT INTO clusters(id, declared_zones) VALUES(?,?)
		ON CONFLICT(id) DO UPDATE SET declared_zones=excluded.declared_zones`,
		cid, mustJSON(declaredZones)); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM nodes WHERE cluster_id=?", cid); err != nil {
		return err
	}
	for _, n := range nodes {
		if err := n.Validate(); err != nil {
			return fmt.Errorf("node %s: %w", n.ID, err)
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO nodes(cluster_id,id,zone,capacity,used,labels,eligible) VALUES(?,?,?,?,?,?,?)`,
			cid, n.ID, n.Zone, mustJSON(n.Capacity), mustJSON(n.Used), mustJSON(n.Labels), boolInt(n.Eligible)); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM running_instances WHERE cluster_id=?", cid); err != nil {
		return err
	}
	for _, r := range running {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO running_instances(cluster_id,id,node_id,request,groups) VALUES(?,?,?,?,?)`,
			cid, r.ID, r.NodeID, mustJSON(r.Request), mustJSON(r.Groups)); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM groups WHERE cluster_id=?", cid); err != nil {
		return err
	}
	for _, g := range groups {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO groups(cluster_id,id,mode,member_ids) VALUES(?,?,?,?)`,
			cid, g.ID, string(g.Mode), mustJSON(g.MemberIDs)); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// RunningRecord is a running instance as stored.
type RunningRecord struct {
	ID     string
	NodeID string
	Request model.Resources
	Groups []string
}

// LoadCluster reads back a full cluster snapshot for scheduling.
func (s *Store) LoadCluster(ctx context.Context, cid string) ([]model.Node, []RunningRecord, []model.Group, []string, error) {
	var zonesJSON string
	err := s.db.QueryRowContext(ctx, "SELECT declared_zones FROM clusters WHERE id=?", cid).Scan(&zonesJSON)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, nil, nil, ErrNotFound
	}
	if err != nil {
		return nil, nil, nil, nil, err
	}
	var zones []string
	if err := json.Unmarshal([]byte(zonesJSON), &zones); err != nil {
		return nil, nil, nil, nil, err
	}
	rows, err := s.db.QueryContext(ctx,
		"SELECT id,zone,capacity,used,labels,eligible FROM nodes WHERE cluster_id=? ORDER BY id", cid)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	defer rows.Close()
	var nodes []model.Node
	for rows.Next() {
		var n model.Node
		var capJSON, usedJSON, labJSON string
		var elig int
		if err := rows.Scan(&n.ID, &n.Zone, &capJSON, &usedJSON, &labJSON, &elig); err != nil {
			return nil, nil, nil, nil, err
		}
		if err := json.Unmarshal([]byte(capJSON), &n.Capacity); err != nil {
			return nil, nil, nil, nil, err
		}
		if err := json.Unmarshal([]byte(usedJSON), &n.Used); err != nil {
			return nil, nil, nil, nil, err
		}
		if err := json.Unmarshal([]byte(labJSON), &n.Labels); err != nil {
			return nil, nil, nil, nil, err
		}
		n.Eligible = elig != 0
		nodes = append(nodes, n)
	}
	if err := rows.Err(); err != nil {
		return nil, nil, nil, nil, err
	}

	grows, err := s.db.QueryContext(ctx,
		"SELECT id,node_id,request,groups FROM running_instances WHERE cluster_id=? ORDER BY id", cid)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	defer grows.Close()
	var running []RunningRecord
	for grows.Next() {
		var r RunningRecord
		var reqJSON, grpJSON string
		if err := grows.Scan(&r.ID, &r.NodeID, &reqJSON, &grpJSON); err != nil {
			return nil, nil, nil, nil, err
		}
		if err := json.Unmarshal([]byte(reqJSON), &r.Request); err != nil {
			return nil, nil, nil, nil, err
		}
		if err := json.Unmarshal([]byte(grpJSON), &r.Groups); err != nil {
			return nil, nil, nil, nil, err
		}
		running = append(running, r)
	}
	if err := grows.Err(); err != nil {
		return nil, nil, nil, nil, err
	}

	ggrows, err := s.db.QueryContext(ctx, "SELECT id,mode,member_ids FROM groups WHERE cluster_id=? ORDER BY id", cid)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	defer ggrows.Close()
	var groups []model.Group
	for ggrows.Next() {
		var g model.Group
		var mode, membersJSON string
		if err := ggrows.Scan(&g.ID, &mode, &membersJSON); err != nil {
			return nil, nil, nil, nil, err
		}
		g.Mode = model.GroupMode(mode)
		if err := json.Unmarshal([]byte(membersJSON), &g.MemberIDs); err != nil {
			return nil, nil, nil, nil, err
		}
		groups = append(groups, g)
	}
	if err := ggrows.Err(); err != nil {
		return nil, nil, nil, nil, err
	}
	return nodes, running, groups, zones, nil
}

// PlanRecord is the persisted form of one solve outcome (success or failure).
type PlanRecord struct {
	ClusterID     string
	PlanID        string
	RequestID     string
	Status        string // succeeded | failed
	Exhaustive    bool
	LeafVisits    int
	Score         any
	Domains       []string
	Failure       *model.PlanFailure
	Placements    []model.Placement
	Steps         any
	AllowRecreate bool
}

// SavePlan commits a plan record, its placements and decision steps
// atomically. A failed plan is persisted with status=failed and its failure
// JSON; it never masquerades as a success.
func (s *Store) SavePlan(ctx context.Context, p PlanRecord) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	failureJSON := ""
	if p.Failure != nil {
		failureJSON = string(mustJSON(p.Failure))
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO plans(cluster_id,plan_id,request_id,status,exhaustive,leaf_visits,score,domains,failure,allow_recreate)
		 VALUES(?,?,?,?,?,?,?,?,?,?)`,
		p.ClusterID, p.PlanID, p.RequestID, p.Status, boolInt(p.Exhaustive), p.LeafVisits,
		mustJSON(p.Score), mustJSON(p.Domains), failureJSON, boolInt(p.AllowRecreate)); err != nil {
		return err
	}
	for i, pl := range p.Placements {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO placements(plan_id,instance_id,node_id,zone,replaces_id) VALUES(?,?,?,?,?)`,
			p.PlanID, pl.InstanceID, pl.NodeID, pl.Zone, pl.ReplacesID); err != nil {
			return fmt.Errorf("placement %d: %w", i, err)
		}
	}
	stepsJSON := mustJSON(p.Steps)
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO plan_steps(plan_id,seq,payload) VALUES(?,0,?)`, p.PlanID, string(stepsJSON)); err != nil {
		return err
	}
	return tx.Commit()
}

// PlanSummary is a persisted plan listing row.
type PlanSummary struct {
	PlanID    string `json:"plan_id"`
	ClusterID string `json:"cluster_id"`
	Status    string `json:"status"`
	CreatedAt string `json:"created_at"`
}

// ListPlans returns recent plans, newest first.
func (s *Store) ListPlans(ctx context.Context, limit int) ([]PlanSummary, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		"SELECT plan_id, cluster_id, status, created_at FROM plans ORDER BY created_at DESC, plan_id DESC LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PlanSummary
	for rows.Next() {
		var p PlanSummary
		if err := rows.Scan(&p.PlanID, &p.ClusterID, &p.Status, &p.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// GetPlan loads one plan with placements, score and steps.
func (s *Store) GetPlan(ctx context.Context, planID string) (map[string]json.RawMessage, error) {
	var clusterID, reqID, status, scoreJSON, domainsJSON, failureJSON string
	var exhaustive, leafVisits, recreate int
	err := s.db.QueryRowContext(ctx,
		`SELECT cluster_id,request_id,status,exhaustive,leaf_visits,score,domains,failure,allow_recreate
		 FROM plans WHERE plan_id=?`, planID).
		Scan(&clusterID, &reqID, &status, &exhaustive, &leafVisits, &scoreJSON, &domainsJSON, &failureJSON, &recreate)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	out := map[string]json.RawMessage{}
	put := func(k, v string) { out[k] = json.RawMessage(v) }
	put("cluster_id", `"`+clusterID+`"`)
	put("plan_id", `"`+planID+`"`)
	put("request_id", `"`+reqID+`"`)
	put("status", `"`+status+`"`)
	put("exhaustive", boolJSON(exhaustive != 0))
	put("leaf_visits", fmt.Sprintf("%d", leafVisits))
	put("score", scoreJSON)
	put("domains", domainsJSON)
	if failureJSON != "" {
		put("failure", failureJSON)
	}
	put("allow_recreate", boolJSON(recreate != 0))

	rows, err := s.db.QueryContext(ctx,
		"SELECT instance_id,node_id,zone,replaces_id FROM placements WHERE plan_id=? ORDER BY instance_id", planID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	placements := []map[string]string{}
	for rows.Next() {
		var iid, nid, zone, rep string
		if err := rows.Scan(&iid, &nid, &zone, &rep); err != nil {
			return nil, err
		}
		m := map[string]string{"instance_id": iid, "node_id": nid, "zone": zone}
		if rep != "" {
			m["replaces_id"] = rep
		}
		placements = append(placements, m)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	put("placements", string(mustJSON(placements)))

	var storedSteps string
	if err := s.db.QueryRowContext(ctx, "SELECT payload FROM plan_steps WHERE plan_id=? ORDER BY seq LIMIT 1", planID).
		Scan(&storedSteps); err == nil {
		put("steps", storedSteps)
	}
	return out, nil
}

// RemoveRunning deletes a running instance (post-replacement eviction or
// desired-state shrink).
func (s *Store) RemoveRunning(ctx context.Context, clusterID, id string) error {
	res, err := s.db.ExecContext(ctx,
		"DELETE FROM running_instances WHERE cluster_id=? AND id=?", clusterID, id)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return fmt.Errorf("%w: running instance %s", ErrNotFound, id)
	}
	return nil
}

// UpsertRunning inserts or replaces a running instance row (used by the
// reconciliation loop after a successful placement).
func (s *Store) UpsertRunning(ctx context.Context, clusterID string, r RunningRecord) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO running_instances(cluster_id,id,node_id,request,groups) VALUES(?,?,?,?,?)
		 ON CONFLICT(cluster_id,id) DO UPDATE SET
		   node_id=excluded.node_id, request=excluded.request, groups=excluded.groups`,
		clusterID, r.ID, r.NodeID, mustJSON(r.Request), mustJSON(r.Groups))
	return err
}

// RecordReconcileRun persists one reconciliation outcome.
func (s *Store) RecordReconcileRun(ctx context.Context, runID, requestID, status, detail string) error {
	_, err := s.db.ExecContext(ctx,
		"INSERT INTO reconcile_runs(run_id,request_id,status,detail) VALUES(?,?,?,?)",
		runID, requestID, status, detail)
	return err
}

// ErrNotFound is returned for missing rows.
var ErrNotFound = errors.New("not found")

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic("store: json marshal: " + err.Error())
	}
	return b
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func boolJSON(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
