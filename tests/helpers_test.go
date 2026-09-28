package tests

import (
	"context"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
	"clsnap/internal/store"
	"clsnap/tests/harness"
)

// multiSnapshotStore is a read-only union of the three node stores for
// replay.Assemble (each node record lives in that node's own store).
type multiSnapshotStore struct {
	nodes  []protocol.NodeID
	stores map[protocol.NodeID]store.SnapshotStore
}

func storesView(h *harness.Harness) store.SnapshotStore {
	m := map[protocol.NodeID]store.SnapshotStore{}
	for _, n := range h.NodeIDs() {
		m[n] = h.Store(n)
	}
	return multiSnapshotStore{nodes: h.NodeIDs(), stores: m}
}

func (m multiSnapshotStore) SaveRecord(context.Context, protocol.NodeRecord) error {
	return apperr.Failure(apperr.CodeNotPermitted, "multiSnapshotStore", "read-only", nil)
}
func (m multiSnapshotStore) GetRecord(ctx context.Context, node protocol.NodeID, snap protocol.SnapshotID) (protocol.NodeRecord, error) {
	return m.stores[node].GetRecord(ctx, node, snap)
}
func (m multiSnapshotStore) ListRecords(ctx context.Context, snap protocol.SnapshotID) ([]protocol.NodeRecord, error) {
	var out []protocol.NodeRecord
	for _, n := range m.nodes {
		rec, err := m.stores[n].GetRecord(ctx, n, snap)
		if err == nil {
			out = append(out, rec)
		}
	}
	return out, nil
}
func (m multiSnapshotStore) ListSnapshots(context.Context) ([]protocol.SnapshotID, error) {
	return nil, apperr.Failure(apperr.CodeNotPermitted, "multiSnapshotStore", "read-only", nil)
}
func (m multiSnapshotStore) Abort(context.Context, protocol.NodeID, protocol.SnapshotID, string) error {
	return apperr.Failure(apperr.CodeNotPermitted, "multiSnapshotStore", "read-only", nil)
}

// drainAll flushes every outbox and pumps every directed channel until no
// envelope remains anywhere. Used when a test wants a fully converged cluster.
func drainAll(ctx context.Context, h *harness.Harness) {
	nodes := h.NodeIDs()
	for i := 0; i < 60; i++ {
		for _, s := range nodes {
			for _, d := range nodes {
				if s == d {
					continue
				}
				_, _ = h.Coord(s).FlushPeer(ctx, d)
			}
		}
		any := false
		for _, s := range nodes {
			for _, d := range nodes {
				if s == d {
					continue
				}
				for h.Cluster().Pending(s, d) > 0 {
					any = true
					if _, err := h.Cluster().Pump(ctx, s, d, 1); err != nil {
						return
					}
				}
			}
		}
		if !any {
			return
		}
	}
	panic("drainAll did not converge")
}
