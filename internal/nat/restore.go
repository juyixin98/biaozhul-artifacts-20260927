package nat

import (
	"context"

	"natlab/internal/model"
	"natlab/internal/storage"
)

// Restore rebuilds in-memory connection tables and allocator bookkeeping from
// the store, skipping mappings whose persisted deadline has already passed at
// the stored watermark. It lets a trace continue after reopening a SQLite file.
func (e *Engine) Restore(ctx context.Context, st storage.Store, runID string) error {
	e.mu.Lock()
	defer e.mu.Unlock()

	info, err := st.GetRun(ctx, runID)
	if err != nil {
		return err
	}
	wm := info.Watermark

	ms, err := st.ListMappings(ctx, runID)
	if err != nil {
		return err
	}
	now := wm
	for _, sm := range ms {
		m := &mapping{
			id: sm.ID, proto: sm.Proto,
			intSrcIP: sm.IntSrcIP, intSrcPort: sm.IntSrcPort,
			remIP: sm.ExtDstIP, remPort: sm.ExtDstPort,
			extPort: sm.ExternalPort, state: sm.State,
			createdAt: sm.CreatedAt, lastSeen: sm.LastSeen, expiresAt: sm.ExpiresAt,
		}
		if !sm.ExpiresAt.IsZero() && !wm.Before(sm.ExpiresAt) {
			// Persisted as active but deadline has passed: reap directly. The
			// port was never held in this fresh pool, so do not release it.
			_ = st.DeleteMapping(ctx, runID, m.id, wm)
			_ = st.AddTombstone(ctx, storage.StoredTombstone{
				RunID: runID, Proto: m.proto, ExternalPort: m.extPort,
				RemoteIP: m.remIP, RemotePort: m.remPort,
				ClosedAt: wm, RetainUntil: wm.Add(tombstoneRetain),
			})
			e.stats.MappingsExpired++
			continue
		}
		fk := flowKey{proto: m.proto, intIP: m.intSrcIP, intPort: m.intSrcPort,
			remIP: m.remIP, remPort: m.remPort}
		e.flows[fk] = m
		e.ports[portKey{proto: m.proto, port: m.extPort}] = m
		pool := e.tcpPool
		if m.proto == model.UDP {
			pool = e.udpPool
		}
		pool.Hold(m.extPort)
	}
	if now.After(e.watermark) {
		e.watermark = now
	}
	return nil
}
