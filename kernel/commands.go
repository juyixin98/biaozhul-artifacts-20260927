package kernel

import (
	"encoding/json"
	"errors"
	"sort"
	"time"

	"dsnet/compute"
	"dsnet/proto"
)

// ErrQueueEmpty 表示本地队列当前没有可认领任务。
// 注意：空队列不是终止信号，调用方不得据此宣布完成。
var ErrQueueEmpty = errors.New("kernel: local queue empty")

// PopReady 在状态机内原子地选取并认领一个 ready 任务（供存储事务使用）。
// 队列为空返回 ErrQueueEmpty。
func (s *State) PopReady(workerID proto.WorkerID, lease time.Duration, requestID string) (*Decision, error) {
	id, ok := s.NextReadyTask()
	if !ok {
		return nil, ErrQueueEmpty
	}
	return s.ApplyClaim(ClaimTask{
		TaskID:   id,
		LeaseID:  proto.LeaseID(NewID("l")),
		WorkerID: workerID,
		Lease:    lease,
	}, requestID)
}

// 命令：状态机的所有输入。命令只携带事实，时钟由状态机内部注入。

// CreateJob 是建网输入（NewState 已覆盖，保留类型用于事件回放）。
type CreateJob struct {
	RootSpec       proto.TaskSpec
	BudgetDeadline time.Time
}

// ClaimTask 是一次认领事实。
type ClaimTask struct {
	TaskID   proto.TaskID
	LeaseID  proto.LeaseID
	WorkerID proto.WorkerID
	Lease    time.Duration
}

// ReportTask 是一次完成报告事实。
//
// 派生结构由计算内核的纯函数决定：状态机调用 compute.Execute 依据
// 任务自带的分层计划与深度重新计算应有的子任务结构，工作者只报告
// “完成”，无权编造因果边。叶子输出（哈希摘要）也由内核重算。
type ReportTask struct {
	TaskID  proto.TaskID
	LeaseID proto.LeaseID
}

// RequeueExpired 把一个到期未确认的认领重新置为 ready。
type RequeueExpired struct {
	TaskID proto.TaskID
}

// BudgetDeadlineReached 表示预算时刻到达：状态机冻结并给出确定结论。
type BudgetDeadlineReached struct{}

// ReportOutcome 是一个任务被确认后的产出记录（供存储与核对）。
type ReportOutcome struct {
	TaskID proto.TaskID `json:"task_id"`
	Kind   proto.TaskKind `json:"kind"`
	// OutputDigest 是叶子确定性输出（哈希链摘要）；fanout 节点为结构摘要。
	OutputDigest string `json:"output_digest,omitempty"`
	Spawned      int    `json:"spawned"`
}

// Decision 是命令的结果。事件按顺序描述“关键步骤”，状态字段给出
// 派生身份与结论；错误为 *proto.Error 时类别可断言。
type Decision struct {
	// Events 是本次命令产生的因果事件（有序）。
	Events []proto.Event
	// Spawned 是本次新派生任务的完整规格（供存储 INSERT）。
	Spawned []proto.TaskSpec
	// Reported 非空时记录被确认任务的确定性产出。
	Reported []ReportOutcome
	// Claimed 非空时表示认领成功，返回给工作者。
	Claimed *proto.Claim
	// Terminal 非空时表示作业到达终态。
	Terminal *proto.JobPhase
	// Ignored 表示提交被幂等忽略（重复确认/过期租约/终态作业）。
	Ignored bool
	// Reason 解释忽略或失败原因；与错误分开列示。
	Reason string
}

// NextReadyTask 确定性地返回一个可认领任务：创建最早，身份次序兜底。
// 返回 false 表示本地队列当前为空（但绝不代表全局终止）。
func (s *State) NextReadyTask() (proto.TaskID, bool) {
	var best proto.TaskID
	var bestAt time.Time
	found := false
	for id, t := range s.tasks {
		if t.status != proto.StatusReady {
			continue
		}
		if !found || t.createdAt.Before(bestAt) ||
			(t.createdAt.Equal(bestAt) && id < best) {
			best, bestAt, found = id, t.createdAt, true
		}
	}
	return best, found
}

// View 返回单个任务的只读视图。
func (s *State) View(id proto.TaskID) (TaskView, bool) {
	t, ok := s.tasks[id]
	if !ok {
		return TaskView{}, false
	}
	return TaskView{
		Spec: t.spec, Status: t.status, Engaged: t.engaged, Passive: t.passive,
		Settled: t.settled, Deficit: t.deficit, LeaseID: t.leaseID,
		WorkerID: t.workerID, LeaseUntil: t.leaseUntil,
	}, true
}

// Counts 返回核心计数（spawned/signals/inflight/reports/dup/unack）。
func (s *State) Counts() (spawned, signals, inFlight, reports, dup, unack int64) {
	return s.spawnedEdges, s.signals, s.inFlight, s.reports, s.dupAcks, s.unack
}

func (s *State) cur() time.Time { return s.now() }

func (s *State) newEvent(typ proto.EventType, requestID string) proto.Event {
	return proto.Event{
		JobID:      s.JobID,
		Type:       typ,
		RequestID:  requestID,
		OccurredAt: s.cur(),
	}
}

// ApplyClaim 处理认领。空队列时返回 Ignored（“先空闲”），但绝不动 DS 计数。
func (s *State) ApplyClaim(cmd ClaimTask, requestID string) (*Decision, error) {
	s.beginMutation()
	if s.Phase != proto.PhaseRunning {
		return nil, phaseClosed()
	}
	t, ok := s.tasks[cmd.TaskID]
	if !ok {
		return nil, proto.Fail(proto.FailNotFound, "任务不存在: %s", cmd.TaskID)
	}
	if t.status != proto.StatusReady {
		return nil, proto.Fail(proto.FailValidation,
			"任务 %s 状态为 %s，不可认领", cmd.TaskID, t.status)
	}
	if cmd.LeaseID == "" || cmd.WorkerID == "" || cmd.Lease <= 0 {
		return nil, proto.Fail(proto.FailValidation, "lease_id/worker_id/lease 均必须非空")
	}
	t.status = proto.StatusClaimed
	t.leaseID = cmd.LeaseID
	t.workerID = cmd.WorkerID
	t.leaseUntil = s.cur().Add(cmd.Lease)
	t.updatedAt = s.cur()
	s.touch(t.spec.TaskID)
	// 在途身份从 ready 转移到 claimed：inFlight 总数不变。
	ev := s.newEvent(proto.EvTaskClaimed, requestID)
	ev.TaskID = t.spec.TaskID
	ev.LeaseID = cmd.LeaseID
	ev.WorkerID = cmd.WorkerID
	ev.Detail = map[string]any{"lease_until": t.leaseUntil}
	return &Decision{
		Events:   []proto.Event{ev},
		Claimed:  &proto.Claim{TaskID: t.spec.TaskID, LeaseID: cmd.LeaseID, WorkerID: cmd.WorkerID, LeaseUntil: t.leaseUntil, Spec: t.spec},
	}, nil
}

// ApplyReport 处理完成报告：登记子因果边、标记被动、按 DS 规则结算级联。
func (s *State) ApplyReport(cmd ReportTask, requestID string) (*Decision, error) {
	s.beginMutation()
	if s.Phase != proto.PhaseRunning {
		// 终态后迟到的报告不改变任何计数，明确标记为忽略。
		return &Decision{Ignored: true, Reason: "作业已处于终态 " + string(s.Phase)}, nil
	}
	t, ok := s.tasks[cmd.TaskID]
	if !ok {
		return nil, proto.Fail(proto.FailNotFound, "任务不存在: %s", cmd.TaskID)
	}
	switch t.status {
	case proto.StatusReported:
		// 重复确认：幂等忽略，不再次结算，deficit 不重复递减。
		s.dupAcks++
		ev := s.newEvent(proto.EvDuplicateIgnored, requestID)
		ev.TaskID = t.spec.TaskID
		ev.LeaseID = cmd.LeaseID
		ev.Detail = map[string]any{"why": "already_reported"}
		return &Decision{Events: []proto.Event{ev}, Ignored: true,
			Reason: "任务已确认，重复提交不计数"}, nil
	case proto.StatusTimedOut:
		s.dupAcks++
		ev := s.newEvent(proto.EvDuplicateIgnored, requestID)
		ev.TaskID = t.spec.TaskID
		ev.Detail = map[string]any{"why": "timed_out_late_report"}
		return &Decision{Events: []proto.Event{ev}, Ignored: true,
			Reason: "任务已超时，迟到报告不计数"}, nil
	case proto.StatusReady:
		// 未持有有效租约却报告：无法确认因果身份。
		return nil, proto.Fail(proto.FailUnknownLease, "任务 %s 未被认领", cmd.TaskID)
	case proto.StatusClaimed:
		if t.leaseID != cmd.LeaseID {
			return nil, proto.Fail(proto.FailUnknownLease,
				"租约不匹配：当前 %s，报告携带 %s", t.leaseID, cmd.LeaseID)
		}
	}

	// 由计算内核确定性地重新计算结果与应派生的子结构。
	res, err := compute.Execute(t.spec)
	if err != nil {
		return nil, err
	}

	d := &Decision{}

	// 1) 登记新派生的因果边。父 deficit += N，子节点 engaged。
	for i := range res.Children {
		child := res.Children[i]
		cid := proto.TaskID(NewID("t"))
		cspec := proto.TaskSpec{
			JobID:    s.JobID,
			TaskID:   cid,
			ParentID: t.spec.TaskID,
			Kind:     child.Kind,
			Depth:    child.Depth,
			Payload:  child.Payload,
		}
		if err := compute.Validate(cspec); err != nil {
			return nil, err
		}
		edgeID := proto.EdgeID(NewID("e"))
		s.spawnChild(cspec)
		sp := s.newEvent(proto.EvTaskSpawned, requestID)
		sp.TaskID = cspec.TaskID
		sp.ParentID = t.spec.TaskID
		sp.EdgeID = edgeID
		sp.Detail = map[string]any{"kind": string(cspec.Kind), "depth": cspec.Depth}
		d.Events = append(d.Events, sp)
		d.Spawned = append(d.Spawned, cspec)
	}

	// 2) 节点被动：自身工作完成并被确认。
	worker := t.workerID
	t.status = proto.StatusReported
	t.passive = true
	t.workerID = ""
	t.leaseID = ""
	t.leaseUntil = time.Time{}
	t.updatedAt = s.cur()
	s.inFlight-- // 自身不再在途（新子任务在 spawnChild 中已计入）
	s.reports++
	s.touch(t.spec.TaskID)
	rep := s.newEvent(proto.EvTaskReported, requestID)
	rep.TaskID = t.spec.TaskID
	rep.WorkerID = worker
	rep.Detail = map[string]any{"spawned": len(res.Children), "output_digest": outputDigest(res.Output)}
	d.Events = append(d.Events, rep)
	d.Reported = append(d.Reported, ReportOutcome{
		TaskID:       t.spec.TaskID,
		Kind:         t.spec.Kind,
		OutputDigest: digestOf(res.Output),
		Spawned:      len(res.Children),
	})

	// 3) 若该节点全部依赖清偿，向上结算并级联（信号乱序安全）。
	d.Events = append(d.Events, s.settleCascade(t, requestID)...)

	// 4) 根结算 => 全局终止。
	if rt := s.tasks[s.rootID]; rt.settled {
		s.Phase = proto.PhaseComplete
		term := proto.PhaseComplete
		d.Terminal = &term
		ev := s.newEvent(proto.EvJobComplete, requestID)
		ev.Detail = map[string]any{
			"reason":          "root passive and deficit=0",
			"in_flight":       s.inFlight,
			"engaged":         s.countEngaged(),
			"unacknowledged":  s.unack,
			"spawned_edges":   s.spawnedEdges,
			"signals":         s.signals,
		}
		d.Events = append(d.Events, ev)
	}
	return d, nil
}

// ApplyRequeue 处理租约到期：任务重新入队。在途身份转移、计数不变。
func (s *State) ApplyRequeue(cmd RequeueExpired, requestID string) (*Decision, error) {
	s.beginMutation()
	if s.Phase != proto.PhaseRunning {
		return &Decision{Ignored: true, Reason: "作业已处于终态 " + string(s.Phase)}, nil
	}
	t, ok := s.tasks[cmd.TaskID]
	if !ok {
		return nil, proto.Fail(proto.FailNotFound, "任务不存在: %s", cmd.TaskID)
	}
	if t.status != proto.StatusClaimed {
		return &Decision{Ignored: true, Reason: "任务不在认领中，无需重派"}, nil
	}
	t.status = proto.StatusReady
	t.leaseID = ""
	t.workerID = ""
	t.leaseUntil = time.Time{}
	t.updatedAt = s.cur()
	s.touch(t.spec.TaskID)
	ev := s.newEvent(proto.EvTaskRequeued, requestID)
	ev.TaskID = t.spec.TaskID
	ev.Detail = map[string]any{"why": "lease_expired"}
	return &Decision{Events: []proto.Event{ev}}, nil
}

// ApplyBudgetDeadline 处理预算时刻。
//
// 规则：预算超时返回未确认——凡仍是 ready/claimed 的任务一律永久
// unacknowledged，作业判 failed；根绝不宣布完成。
// 已 reported 但因果链尚未结算（子树仍有在途）的情况也会被
// unack 任务的存在自然阻断：那些在途任务超时后其节点永不 settle，
// 父边永远开放。
func (s *State) ApplyBudgetDeadline(requestID string) (*Decision, error) {
	s.beginMutation()
	if s.Phase != proto.PhaseRunning {
		return &Decision{Ignored: true, Reason: "预算时刻到达时作业已终态"}, nil
	}
	d := &Decision{}
	var open []proto.TaskID
	for id, t := range s.tasks {
		if t.status == proto.StatusReady || t.status == proto.StatusClaimed {
			open = append(open, id)
		}
	}
	sortID(open)
	for _, id := range open {
		t := s.tasks[id]
		t.status = proto.StatusTimedOut
		t.leaseID = ""
		t.workerID = ""
		t.leaseUntil = time.Time{}
		t.updatedAt = s.cur()
		s.inFlight--
		s.unack++
		s.touch(id)
		ev := s.newEvent(proto.EvTaskTimedOut, requestID)
		ev.TaskID = id
		ev.Detail = map[string]any{"why": "budget_deadline", "was": string(t.status)}
		d.Events = append(d.Events, ev)
	}
	phase := proto.PhaseFailed
	s.Phase = phase
	d.Terminal = &phase
	ev := s.newEvent(proto.EvJobFailed, requestID)
	ev.Detail = map[string]any{
		"reason":         "budget deadline reached with unacknowledged tasks",
		"unacknowledged": s.unack,
		"engaged":        s.countEngaged(), // >0：未确认因果边仍开放
		"open_edges":     s.spawnedEdges - s.signals,
	}
	d.Events = append(d.Events, ev)
	return d, nil
}

// spawnChild 登记一条父→子因果边：父 deficit++，子 engaged、计入在途。
func (s *State) spawnChild(cspec proto.TaskSpec) {
	parent := s.tasks[cspec.ParentID]
	parent.deficit++
	s.spawnedEdges++
	now := s.cur()
	s.tasks[cspec.TaskID] = &task{
		spec:      cspec,
		status:    proto.StatusReady,
		engaged:   true, // DS：收到消息的节点进入介入
		createdAt: now,
		updatedAt: now,
	}
	s.inFlight++
	s.touch(cspec.TaskID)
	s.touch(cspec.ParentID)
}

// settleCascade 从刚变被动的节点开始，沿父链结算所有“被动且 deficit=0”
// 的节点。每个节点一生只向父发一次信号；步骤对确认/信号的乱序天然安全：
// 只有最后一条依赖清偿时结算才会发生。
func (s *State) settleCascade(start *task, requestID string) []proto.Event {
	var events []proto.Event
	cur := start
	for {
		if cur.settled || !cur.passive || cur.deficit != 0 {
			break
		}
		cur.settled = true
		cur.engaged = false
		s.touch(cur.spec.TaskID)
		if cur.spec.TaskID == s.rootID {
			// 根没有父节点，不发送信号；settled 即表示终止条件成立。
			break
		}
		s.signals++
		parent := s.tasks[cur.spec.ParentID]
		parent.deficit-- // 清偿父→本节点的因果边
		s.touch(parent.spec.TaskID)
		ev := s.newEvent(proto.EvSignal, requestID)
		ev.TaskID = cur.spec.TaskID
		ev.ParentID = cur.spec.ParentID
		ev.Detail = map[string]any{
			"parent_deficit_after": parent.deficit,
		}
		events = append(events, ev)
		cur = parent
	}
	return events
}

func (s *State) countEngaged() int64 {
	var n int64
	for _, t := range s.tasks {
		if t.engaged {
			n++
		}
	}
	return n
}

func phaseClosed() error {
	return proto.Fail(proto.FailJobNotRunning, "作业不在 running 阶段")
}

func digestOf(m map[string]any) string {
	if v, ok := m["digest"].(string); ok {
		return v
	}
	return outputDigest(m)
}

func outputDigest(m map[string]any) string {
	b, _ := json.Marshal(m)
	if len(b) > 80 {
		return string(b[:77]) + "..."
	}
	return string(b)
}

func sortID(ids []proto.TaskID) {
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
}
