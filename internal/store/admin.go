package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"placer/internal/model"
)

// UpsertNode inserts or replaces a node row.
func (s *Store) UpsertNode(ctx context.Context, n model.Node) error {
	if err := n.Validate(); err != nil {
		return err
	}
	capJSON, _ := json.Marshal(n.Capacity)
	labels := n.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	labelsJSON, _ := json.Marshal(labels)
	taintsJSON, _ := json.Marshal(n.Taints)

	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO nodes(id, region, zone, status, capacity, labels, taints, version)
		VALUES (?,?,?,?,?,?,?,1)
		ON CONFLICT(id) DO UPDATE SET
		  region=excluded.region, zone=excluded.zone, status=excluded.status,
		  capacity=excluded.capacity, labels=excluded.labels, taints=excluded.taints,
		  version=nodes.version+1`,
		n.ID, n.Region, n.Zone, string(n.Status), string(capJSON),
		string(labelsJSON), string(taintsJSON))
	if err != nil {
		return fmt.Errorf("upsert node %s: %w", n.ID, err)
	}
	return nil
}

// SetNodeStatus updates lifecycle status only.
func (s *Store) SetNodeStatus(ctx context.Context, id string, status model.NodeStatus) error {
	switch status {
	case model.NodeReady, model.NodeDisabled, model.NodeNotReady:
	default:
		return fmt.Errorf("unknown node status %q", status)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx,
		`UPDATE nodes SET status=?, version=version+1 WHERE id=?`, string(status), id)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return fmt.Errorf("node %q not found", id)
	}
	return nil
}

// UpsertInstance inserts an instance. Pending instances must not carry a
// node; bound instances must.
func (s *Store) UpsertInstance(ctx context.Context, in model.Instance) error {
	if err := in.Validate(); err != nil {
		return err
	}
	if in.State == "" {
		in.State = model.StatePending
	}
	reqJSON, _ := json.Marshal(in.Request)
	sel := in.NodeSelector
	if sel == nil {
		sel = map[string]string{}
	}
	selJSON, _ := json.Marshal(sel)
	tolJSON, _ := json.Marshal(in.Tolerations)
	groups := in.Groups
	if groups == nil {
		groups = map[string]string{}
	}
	groupsJSON, _ := json.Marshal(groups)

	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO instances(id, state, node_id, request, zone, selector,
		    tolerations, groups_json, attempts, last_code, updated_at)
		VALUES (?,?,?,?,?,?,?,?,0,'',?)
		ON CONFLICT(id) DO UPDATE SET
		  state=excluded.state, node_id=excluded.node_id, request=excluded.request,
		  zone=excluded.zone, selector=excluded.selector,
		  tolerations=excluded.tolerations, groups_json=excluded.groups_json,
		  updated_at=excluded.updated_at`,
		in.ID, string(in.State), in.NodeID, string(reqJSON), in.Zone,
		string(selJSON), string(tolJSON), string(groupsJSON), now)
	if err != nil {
		return fmt.Errorf("upsert instance %s: %w", in.ID, err)
	}
	return nil
}

// SetPolicy replaces the cluster group policy document.
func (s *Store) SetPolicy(ctx context.Context, p model.Policy) error {
	if err := p.Validate(); err != nil {
		return err
	}
	if p.Groups == nil {
		p.Groups = []model.GroupRule{}
	}
	doc, err := json.Marshal(p)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO policy_doc(id, doc, version) VALUES (1, ?, 1)
		ON CONFLICT(id) DO UPDATE SET doc=excluded.doc, version=policy_doc.version+1`,
		string(doc))
	return err
}

// DeleteNode removes a node; bound instances make this fail to avoid
// orphaning occupants.
func (s *Store) DeleteNode(ctx context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var bound int
	if err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM instances WHERE node_id=? AND state=?`,
		id, string(model.StateBound)).Scan(&bound); err != nil {
		return err
	}
	if bound > 0 {
		return fmt.Errorf("cannot delete node %q: %d bound instance(s)", id, bound)
	}
	res, err := s.db.ExecContext(ctx, `DELETE FROM nodes WHERE id=?`, id)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

// DeleteInstance removes an instance row.
func (s *Store) DeleteInstance(ctx context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx, `DELETE FROM instances WHERE id=?`, id)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

// Reset empties all mutable tables (used by fixture loading and tests).
func (s *Store) Reset(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
		DELETE FROM events; DELETE FROM runs; DELETE FROM instances;
		DELETE FROM nodes; DELETE FROM policy_doc;`)
	return err
}
