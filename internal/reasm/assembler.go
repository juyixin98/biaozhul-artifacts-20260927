// Package reasm 实现离线 IPv4 分片重组后端的核心算法。
//
// 算法假设（与 RFC 791 / RFC 815 的关系）：
//   - 分组键 = 源地址、目的地址、协议、16 位标识；超时附着在“组实例”上。
//   - 偏移以 8 字节为单位（13 位字段）；MF=1 的非末片载荷长度必须是 8 的倍数，
//     否则后续片的偏移无法表达，仅拒绝该单片（422），不污染整组。
//   - 重叠策略采用“整组拒绝”：只要新片与任一已接受片的字节区间相交且不是
//     完全重复片，整组立即终结为 rejected，已收字节不输出、不做择优保留。
//   - “完全重复片”单独识别：起始偏移、长度、MF 三者相同且载荷字节全等时，
//     计为一次重传（duplicate），幂等且不改变组状态。
//   - 末片先到或缺少任意片时绝不提前输出：只有 MF=0 已到且 [0,totalLen)
//     连续无缺口覆盖时才输出重组字节。
//   - 明确超时：自首个分片到达起计算 deadline；超时后 pending 组终结为
//     timed_out 并回收活动分片，分组键随之可被新 ID 周期复用。
//   - 本实现是网络层（IP）数据报重组，不是 TCP 流重组：不看端口、不看 TCP
//     序列号，载荷只按 IP 偏移拼接。
package reasm

import (
	"bytes"
	"context"
	"fmt"
	"sync"
	"time"

	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/store"
)

// Clock 抽象时间来源，便于测试与虚拟时间戳回放。
type Clock interface{ Now() time.Time }

// wallClock 是默认的墙钟实现。
type wallClock struct{}

func (wallClock) Now() time.Time { return time.Now() }

// Config 控制重组器行为。
type Config struct {
	Timeout          time.Duration
	ResultTTL        time.Duration
	MaxDatagramBytes int
}

// Assembler 是线程安全的重组器。
type Assembler struct {
	mu     sync.Mutex
	cfg    Config
	clk    Clock
	st     store.Store
	active map[netmodel.FragKey]*group
}

// block 是一片在重组坐标（字节偏移）下的已接受数据。
type block struct {
	start     int
	data      []byte
	more      bool
	duplicate bool
}

// group 是一个组实例的权威内存状态。
type group struct {
	key        netmodel.FragKey
	state      State
	reason     string
	blocks     map[int]block // 以 start 为键，完全重复片不新增块
	seq        int
	dupCount   int
	hasLast    bool
	lastStart  int
	totalLen   int
	startedAt  time.Time
	deadline   time.Time
	terminalAt time.Time
	expiresAt  time.Time
	assembled  []byte
}

// New 构造重组器并从 Store 恢复未终结组。
func New(cfg Config, st store.Store, clk Clock) (*Assembler, error) {
	if cfg.MaxDatagramBytes <= 0 {
		cfg.MaxDatagramBytes = 65535
	}
	if clk == nil {
		clk = wallClock{}
	}
	a := &Assembler{cfg: cfg, clk: clk, st: st, active: make(map[netmodel.FragKey]*group)}
	if err := a.recover(context.Background()); err != nil {
		return nil, err
	}
	return a, nil
}

// recover 从存储重建所有 pending 组（终结组不重新进入活动表，只供 Lookup）。
func (a *Assembler) recover(ctx context.Context) error {
	groups, err := a.st.ListOpenGroups(ctx)
	if err != nil {
		return err
	}
	for _, gr := range groups {
		frags, err := a.st.ListFragments(ctx, gr.Key)
		if err != nil {
			return err
		}
		g := &group{
			key: gr.Key, state: StatePending,
			blocks:    make(map[int]block),
			startedAt: gr.StartedAt,
			deadline:  gr.Deadline,
			hasLast:   gr.HasLast,
			lastStart: gr.LastOffset,
			totalLen:  gr.TotalLength,
		}
		for _, f := range frags {
			g.seq = f.Seq + 1
			if f.Duplicate {
				g.dupCount++
				continue
			}
			g.blocks[f.Offset] = block{start: f.Offset, data: f.Payload, more: f.More}
		}
		a.active[g.key] = g
	}
	return nil
}

// Process 处理一个已解析的 IPv4 分片，返回结构化结果。
//
// 返回错误时 Outcome 可能为零值；错误分为两类：
//   - netmodel.ParseError / KindUnalignedFragment：单片非法，不影响组；
//   - 其余 *Error：组级终结（拒绝/过早复用/超长）。
func (a *Assembler) Process(ctx context.Context, pkt *netmodel.Packet) (*Outcome, error) {
	return a.ProcessAt(ctx, pkt, a.clk.Now())
}

// ProcessAt 以显式给定时间处理分片，供 pcap 虚拟时间戳回放使用。
func (a *Assembler) ProcessAt(ctx context.Context, pkt *netmodel.Packet, now time.Time) (*Outcome, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	start := int(pkt.FragmentOffset) * 8
	payload := pkt.Payload
	end := start + len(payload)

	// RFC 791：MF=1 的片载荷长度必须是 8 字节倍数，否则其后续偏移不可表示。
	if pkt.MoreFragments && len(payload)%8 != 0 {
		return nil, &Error{
			Kind:   KindUnalignedFragment,
			Key:    pkt.Key,
			Detail: detailf("MF=1 片偏移=%d 载荷长度=%d 不是 8 字节倍数", start, len(payload)),
		}
	}

	// 组级超长检查：任何片越过配置上限都拒绝整组（IPv4 总长硬约束）。
	if end > a.cfg.MaxDatagramBytes {
		return a.rejectNewOrExisting(ctx, pkt, now, KindDatagramTooLarge,
			detailf("片覆盖 [%d,%d) 越过重组上限 %d 字节", start, end, a.cfg.MaxDatagramBytes))
	}

	g, exists := a.active[pkt.Key]
	if !exists {
		// 活动表无组：检查存储中的终结留存。
		if rec, found, err := a.st.GetGroup(ctx, pkt.Key); err != nil {
			return nil, err
		} else if found && store.IsTerminalState(rec.State) {
			return nil, &Error{
				Kind: KindGroupAlreadyTerminal, Key: pkt.Key,
				Detail: detailf("上一个组 state=%s 留存至 %s 才回收，期间 ID 不可复用",
					rec.State, rec.ExpiresAt.Format(time.RFC3339Nano)),
			}
		}

		g = &group{
			key:       pkt.Key,
			state:     StatePending,
			blocks:    make(map[int]block),
			startedAt: now,
			deadline:  now.Add(a.cfg.Timeout),
		}
		a.active[pkt.Key] = g
		if err := a.persistGroup(ctx, g); err != nil {
			return nil, err
		}
	}

	// 活动组只可能是 pending（终结即移出活动表）。
	if g.state != StatePending {
		return nil, &Error{Kind: KindGroupAlreadyTerminal, Key: pkt.Key,
			Detail: detailf("活动组状态异常 state=%s", g.state)}
	}

	// 完全重复片识别：同 (offset, 长度, MF) 且字节全等。
	if dup, identical := classifyDuplicate(g, start, payload, pkt.MoreFragments); identical {
		g.dupCount++
		_ = a.st.AddFragment(ctx, store.FragmentRecord{
			Key: g.key, Seq: g.seq, Offset: start, Length: len(payload),
			More: pkt.MoreFragments, Duplicate: true, SeenAt: now,
		})
		g.seq++
		_ = a.persistGroup(ctx, g)
		return a.outcome(g, now, true, false, "", ""), nil
	} else if dup {
		// 同区间但内容/标志不一致：按覆盖冲突处理，整组拒绝。
		return a.rejectGroup(ctx, g, now, KindOverlapGroupRejected,
			detailf("区间 [%d,%d) 与已存片相同但内容/MF 不一致（非完全重复）", start, end))
	}

	// 覆盖检查：与任何已接受块相交即整组拒绝。
	if errKind, detail := detectOverlap(g, start, end); errKind != "" {
		return a.rejectGroup(ctx, g, now, ErrorKind(errKind), detail)
	}

	// 末片一致性检查。
	if !pkt.MoreFragments {
		if g.hasLast {
			if end != g.totalLen {
				return a.rejectGroup(ctx, g, now, KindConflictingLastFragment,
					detailf("新末片终点=%d 与已宣告总长度=%d 冲突", end, g.totalLen))
			}
			// 终点相同却未被完全重复逻辑命中，说明起点不同且构成覆盖：已在上面拦截。
		}
	}

	// 若已知总长度，任何片越过它即冲突。
	if g.hasLast && end > g.totalLen {
		return a.rejectGroup(ctx, g, now, KindConflictingLastFragment,
			detailf("片覆盖 [%d,%d) 越过末片宣告总长度 %d", start, end, g.totalLen))
	}

	// 接受新片。
	g.blocks[start] = block{start: start, data: payload, more: pkt.MoreFragments}
	g.seq++
	if err := a.st.AddFragment(ctx, store.FragmentRecord{
		Key: g.key, Seq: g.seq - 1, Offset: start, Length: len(payload),
		More: pkt.MoreFragments, Duplicate: false, SeenAt: now, Payload: payload,
	}); err != nil {
		return nil, err
	}
	if !pkt.MoreFragments {
		g.hasLast = true
		g.lastStart = start
		g.totalLen = end
	}
	if err := a.persistGroup(ctx, g); err != nil {
		return nil, err
	}

	// 完整性判定：末片已到 + [0,totalLen) 连续覆盖。
	if g.hasLast && covered(g) == g.totalLen {
		assembled, ok := assemble(g)
		if !ok {
			return a.rejectGroup(ctx, g, now, KindOverlapGroupRejected,
				"拼接阶段发现空洞/越界（内部一致性检查）")
		}
		return a.complete(ctx, g, now, assembled)
	}

	return a.outcome(g, now, false, false, "", ""), nil
}

// rejectNewOrExisting 处理“到达时即非法且可能超长”的片：无组则先建组再拒绝，
// 保证拒绝事实可审计、可查询；有组走普通拒绝路径。
func (a *Assembler) rejectNewOrExisting(ctx context.Context, pkt *netmodel.Packet, now time.Time,
	kind ErrorKind, detail string) (*Outcome, error) {
	g, exists := a.active[pkt.Key]
	if !exists {
		if rec, found, err := a.st.GetGroup(ctx, pkt.Key); err != nil {
			return nil, err
		} else if found && store.IsTerminalState(rec.State) {
			return nil, &Error{Kind: KindGroupAlreadyTerminal, Key: pkt.Key,
				Detail: detailf("终结留存未回收 state=%s", rec.State)}
		}
		g = &group{
			key: pkt.Key, state: StatePending, blocks: make(map[int]block),
			startedAt: now, deadline: now.Add(a.cfg.Timeout),
		}
		a.active[pkt.Key] = g
	}
	return a.rejectGroup(ctx, g, now, kind, detail)
}

// classifyDuplicate 报告：
//   - sameRange=true 表示存在同 (offset,len) 的块；
//   - identical=true 进一步要求 MF 与载荷字节全等（完全重复片）。
func classifyDuplicate(g *group, start int, payload []byte, more bool) (sameRange, identical bool) {
	b, ok := g.blocks[start]
	if !ok || len(b.data) != len(payload) {
		return false, false
	}
	if b.more != more {
		return true, false
	}
	return true, bytes.Equal(b.data, payload)
}

// detectOverlap 检查新区间 [start,end) 是否与已存块相交。
// 已存在同起点块的情形由 classifyDuplicate 先处理。
func detectOverlap(g *group, start, end int) (string, string) {
	for _, b := range g.blocks {
		bStart, bEnd := b.start, b.start+len(b.data)
		if start < bEnd && bStart < end {
			return string(KindOverlapGroupRejected),
				detailf("新区间 [%d,%d) 与已接受片 [%d,%d) 发生字节重叠，按整组拒绝处理",
					start, end, bStart, bEnd)
		}
	}
	return "", ""
}

// covered 返回自 0 起无空洞可确认覆盖到的最远字节位置。
func covered(g *group) int {
	pos := 0
	for {
		advanced := false
		for _, b := range g.blocks {
			if b.start == pos {
				pos = b.start + len(b.data)
				advanced = true
				break
			}
		}
		if !advanced {
			return pos
		}
	}
}

// assemble 按偏移拼接所有块并校验恰好无空洞、无越界。
func assemble(g *group) ([]byte, bool) {
	starts := make([]int, 0, len(g.blocks))
	for s := range g.blocks {
		starts = append(starts, s)
	}
	// 插入排序即可：组数与片数都很小。
	for i := 1; i < len(starts); i++ {
		for j := i; j > 0 && starts[j-1] > starts[j]; j-- {
			starts[j-1], starts[j] = starts[j], starts[j-1]
		}
	}

	out := make([]byte, 0, g.totalLen)
	pos := 0
	for _, s := range starts {
		if s != pos {
			return nil, false // 空洞或乱序空洞
		}
		out = append(out, g.blocks[s].data...)
		pos = s + len(g.blocks[s].data)
	}
	if pos != g.totalLen {
		return nil, false
	}
	return out, true
}

func (a *Assembler) complete(ctx context.Context, g *group, now time.Time, assembled []byte) (*Outcome, error) {
	g.state = StateComplete
	g.assembled = assembled
	g.terminalAt = now
	g.expiresAt = now.Add(a.cfg.ResultTTL)
	if err := a.terminalize(ctx, g); err != nil {
		return nil, err
	}
	return a.outcome(g, now, false, false, "", ""), nil
}

func (a *Assembler) rejectGroup(ctx context.Context, g *group, now time.Time, kind ErrorKind, detail string) (*Outcome, error) {
	g.state = StateRejected
	g.reason = string(kind)
	g.terminalAt = now
	g.expiresAt = now.Add(a.cfg.ResultTTL)
	if err := a.terminalize(ctx, g); err != nil {
		return nil, err
	}
	return nil, &Error{Kind: kind, Key: g.key, Detail: detail}
}

// terminalize 终结组：活动分片落库后清空（结果/组行保留到留存到期），组移出活动表。
func (a *Assembler) terminalize(ctx context.Context, g *group) error {
	if err := a.persistGroup(ctx, g); err != nil {
		return err
	}
	if err := a.st.DeleteFragments(ctx, g.key); err != nil {
		return err
	}
	delete(a.active, g.key)
	return nil
}

func (a *Assembler) persistGroup(ctx context.Context, g *group) error {
	var lastOff int
	if g.hasLast {
		lastOff = g.lastStart
	}
	return a.st.UpsertGroup(ctx, store.GroupRecord{
		Key: g.key, State: string(g.state),
		StartedAt: g.startedAt, Deadline: g.deadline,
		TerminalAt: g.terminalAt, ExpiresAt: g.expiresAt,
		HasLast: g.hasLast, LastOffset: lastOff, TotalLength: g.totalLen,
		Reason: g.reason, Assembled: g.assembled,
	})
}

func (a *Assembler) outcome(g *group, now time.Time, dup, recycled bool, kind, detail string) *Outcome {
	o := &Outcome{
		Key:       g.key,
		KeyText:   g.key.String(),
		Accepted:  !dup && g.state == StatePending,
		Duplicate: dup,
		Recycled:  recycled,
		Reason:    kind,
		Detail:    detail,
		Progress:  g.progress(),
		StartedAt: g.startedAt,
		Deadline:  g.deadline,
	}
	switch g.state {
	case StateComplete:
		o.State = StateComplete
		o.Assembled = g.assembled
		exp := g.expiresAt
		o.ExpiresAt = &exp
	case StatePending:
		o.State = StatePending
	default:
		o.State = g.state
		exp := g.expiresAt
		o.ExpiresAt = &exp
	}
	_ = now
	return o
}

func (g *group) progress() Progress {
	var lastOff *int
	if g.hasLast {
		v := g.lastStart
		lastOff = &v
	}
	return Progress{
		Received:    len(g.blocks),
		Duplicates:  g.dupCount,
		HasLast:     g.hasLast,
		LastOffset:  lastOff,
		TotalLength: g.totalLen,
		Covered:     covered(g),
	}
}

// Lookup 查询组视图：先活动表，后存储中的终结留存。
func (a *Assembler) Lookup(ctx context.Context, key netmodel.FragKey) (*Snapshot, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if g, ok := a.active[key]; ok {
		return &Snapshot{
			Key: key, KeyText: key.String(), State: g.state,
			Progress: g.progress(), StartedAt: g.startedAt, Deadline: g.deadline,
		}, nil
	}
	rec, found, err := a.st.GetGroup(ctx, key)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, &Error{Kind: KindGroupNotFound, Key: key, Detail: "活动表与终结留存均无此组"}
	}
	snap := &Snapshot{
		Key: key, KeyText: key.String(), State: State(rec.State),
		Reason: rec.Reason, Assembled: rec.Assembled,
		StartedAt: rec.StartedAt, Deadline: rec.Deadline,
		Progress: Progress{
			Received: -1, HasLast: rec.HasLast,
			LastOffset:  intPtr(rec.HasLast, rec.LastOffset),
			TotalLength: rec.TotalLength,
			Covered:     rec.TotalLength,
		},
	}
	if !rec.ExpiresAt.IsZero() {
		exp := rec.ExpiresAt
		snap.ExpiresAt = &exp
	}
	return snap, nil
}

func intPtr(has bool, v int) *int {
	if !has {
		return nil
	}
	return &v
}

// SweepResult 报告一次回收扫描的动作，供日志/报告关联。
type SweepResult struct {
	TimedOut []netmodel.FragKey
	Recycled []netmodel.FragKey
}

// Sweep 执行两类回收：
//   - pending 组超过明确超时 -> 终结为 timed_out 并“彻底删除”该组行与活动分片，
//     分组键随之立即可复用（对齐真实内核对超时缓冲的处理）；
//   - complete/rejected 组留存超过 ResultTTL -> 彻底删除（Recycled）。
//
// 超时事实只在审计日志（testlog/replay Report 的 sweep_timed_out）中留痕，
// 不占用状态存储的组生命周期，避免“已超时却仍阻止 ID 复用”的语义矛盾。
func (a *Assembler) Sweep(ctx context.Context) (SweepResult, error) {
	return a.SweepAt(ctx, a.clk.Now())
}

// SweepAt 以显式给定时间执行回收（虚拟时间回放使用）。
func (a *Assembler) SweepAt(ctx context.Context, now time.Time) (SweepResult, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	res := SweepResult{}

	for key, g := range a.active {
		if g.state == StatePending && !g.deadline.After(now) {
			// 先把 timed_out 状态落一次库（审计可在外部报告中观察），
			// 随后彻底删除，释放分组键与活动分片。
			g.state = StateTimedOut
			g.reason = "timeout"
			g.terminalAt = now
			if err := a.persistGroup(ctx, g); err != nil {
				return res, err
			}
			if err := a.st.DeleteGroup(ctx, key); err != nil {
				return res, err
			}
			delete(a.active, key)
			res.TimedOut = append(res.TimedOut, key)
		}
	}

	expired, err := a.st.ListTerminalExpired(ctx, now)
	if err != nil {
		return res, err
	}
	for _, rec := range expired {
		if err := a.st.DeleteGroup(ctx, rec.Key); err != nil {
			return res, err
		}
		if g, ok := a.active[rec.Key]; ok {
			delete(a.active, rec.Key)
			_ = g
		}
		res.Recycled = append(res.Recycled, rec.Key)
	}
	return res, nil
}

// Purge 立即彻底删除一个组（管理接口使用）。
func (a *Assembler) Purge(ctx context.Context, key netmodel.FragKey) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.active, key)
	return a.st.DeleteGroup(ctx, key)
}

// Stats 返回资源占用快照。
func (a *Assembler) Stats(ctx context.Context) (Stats, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	pending := 0
	for _, g := range a.active {
		if g.state == StatePending {
			pending++
		}
	}
	all, err := a.st.ListAllGroups(ctx)
	if err != nil {
		return Stats{}, err
	}
	nfrag, err := a.st.CountFragments(ctx)
	if err != nil {
		return Stats{}, err
	}
	return Stats{ActivePending: pending, StoreGroups: len(all), StoreFragments: nfrag}, nil
}

func detailf(format string, args ...any) string {
	return fmt.Sprintf(format, args...)
}
