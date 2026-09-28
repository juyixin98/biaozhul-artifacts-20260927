// Package replay 提供事件日志回放：从首个事件重建 RIB，并与“当前表”
// （或路由快照表装载结果）逐项对照，证明日志完整、状态可重建。
// 它是独立于 HTTP 的纯逻辑模块，便于在测试中直接断言。
package replay

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"

	"rib/internal/netmodel"
	"rib/internal/rib"
	"rib/internal/store"
)

// HopReport 是单条事件应用结果。
type HopReport struct {
	Seq       int64  `json:"seq"`
	Type      string `json:"type"`
	Version   int64  `json:"version"`
	RequestID string `json:"request_id"`
	Applied   bool   `json:"applied"`
	Error     string `json:"error,omitempty"`
}

// Report 是一次回放的完整结论。
type Report struct {
	EventsPlayed int         `json:"events_played"`
	LastVersion  int64       `json:"last_version"`
	ReplayedV4   int         `json:"replayed_v4_count"`
	ReplayedV6   int         `json:"replayed_v6_count"`
	CurrentV4    int         `json:"current_v4_count"`
	CurrentV6    int         `json:"current_v6_count"`
	Hops         []HopReport `json:"hops"`
	// Consistent 表示重放重建出的路由集合与当前路由集合完全一致。
	Consistent bool     `json:"consistent"`
	Mismatches []string `json:"mismatches,omitempty"`
}

// Engine 依赖最小化：只消费 store 读取接口所需方法。
type Engine struct {
	st *store.Store
}

func New(st *store.Store) *Engine { return &Engine{st: st} }

// Run 读取 sinceSeq 之后的全部事件（上限 limit，<=0 表示不限），
// 在空 RIB 上重放，再与 current（通常是运行中 RIB 的当前路由集）对照。
func (e *Engine) Run(ctx context.Context, sinceSeq int64, limit int, current []netmodel.Route, maxDepth int) (*Report, error) {
	events, err := e.st.EventsSince(ctx, sinceSeq, limit)
	if err != nil {
		return nil, err
	}
	r := rib.New().WithMaxDepth(maxDepth)
	rep := &Report{Hops: make([]HopReport, 0, len(events))}

	for _, ev := range events {
		hop := HopReport{Seq: ev.Seq, Type: string(ev.Type), Version: ev.Version, RequestID: ev.RequestID}
		if err := applyEvent(r, ev); err != nil {
			hop.Error = err.Error()
		} else {
			hop.Applied = true
			rep.LastVersion = ev.Version
		}
		rep.Hops = append(rep.Hops, hop)
	}
	rep.EventsPlayed = len(events)

	replayed := append(r.Routes(netmodel.AFIPv4), r.Routes(netmodel.AFIPv6)...)
	rep.ReplayedV4, rep.ReplayedV6 = countFamilies(replayed)
	rep.CurrentV4, rep.CurrentV6 = countFamilies(current)
	rep.Mismatches = diffRoutes(replayed, current)
	rep.Consistent = len(rep.Mismatches) == 0
	return rep, nil
}

func applyEvent(r *rib.RIB, ev store.Event) error {
	switch ev.Type {
	case store.EventUpsert:
		var rt netmodel.Route
		if err := json.Unmarshal(ev.Payload, &rt); err != nil {
			return err
		}
		return r.Upsert(rt)
	case store.EventDelete:
		var d store.DeletePayload
		if err := json.Unmarshal(ev.Payload, &d); err != nil {
			return err
		}
		p, err := netmodel.ParsePrefix(d.Prefix)
		if err != nil {
			return err
		}
		if err := r.Delete(p, d.ID); err != nil {
			return fmt.Errorf("seq %d: %w", ev.Seq, err)
		}
		return nil
	case store.EventReplaceAll:
		var p store.ReplacePayload
		if err := json.Unmarshal(ev.Payload, &p); err != nil {
			return err
		}
		return r.ReplaceAll(rib.ReplaceRequest{V4: p.V4, V6: p.V6})
	default:
		return fmt.Errorf("unknown event type %q", ev.Type)
	}
}

func countFamilies(rs []netmodel.Route) (v4, v6 int) {
	for _, rt := range rs {
		if rt.Prefix.Family() == netmodel.AFIPv4 {
			v4++
		} else {
			v6++
		}
	}
	return
}

// routeKey 是路由集合对照的稳定键。
func routeKey(rt netmodel.Route) string {
	return fmt.Sprintf("%d|%s|%s|ad=%d|m=%d|%s|%s|%s",
		rt.Prefix.Family(), rt.Prefix.String(), rt.ID,
		rt.AdminDistance, rt.Metric, rt.Protocol,
		rt.Nexthop.Kind, nhText(rt))
}

func nhText(rt netmodel.Route) string {
	nh := rt.Nexthop
	if nh.HasAddress() {
		return nh.Address.String()
	}
	return nh.Iface
}

func diffRoutes(replayed, current []netmodel.Route) []string {
	key := func(rs []netmodel.Route) map[string]int {
		m := map[string]int{}
		for _, rt := range rs {
			m[routeKey(rt)]++
		}
		return m
	}
	a, b := key(replayed), key(current)
	var mm []string
	for k, n := range a {
		if b[k] != n {
			mm = append(mm, fmt.Sprintf("replay-only/count-diff: %s (replay=%d current=%d)", k, n, b[k]))
		}
	}
	for k, n := range b {
		if _, ok := a[k]; !ok {
			mm = append(mm, fmt.Sprintf("current-only: %s (current=%d)", k, n))
		}
	}
	sort.Strings(mm)
	return mm
}
