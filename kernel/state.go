package kernel

import (
	"sort"
	"time"

	"dsnet/proto"
)

// task 是状态机内部的节点视图。
type task struct {
	spec proto.TaskSpec

	status proto.TaskStatus

	// engaged：DS 意义上节点是否已介入。根创建时即为 true；
	// 子任务派生时为 true；settle（向父发出信号）后为 false。
	engaged bool
	// passive：节点自身工作是否已完成并被确认。
	passive bool
	// settled：该节点是否已向父发出过信号（信号一生一次，重复确认不重发）。
	settled bool

	// deficit：本节点已派生、尚未被清偿的子边数。
	deficit int64

	// 当前在途租约（status=claimed 时有效）。
	leaseID     proto.LeaseID
	workerID    proto.WorkerID
	leaseUntil  time.Time

	createdAt time.Time
	updatedAt time.Time
}

// State 是单个作业的完整 DS 状态。它的所有变更都通过 Apply* 命令进行，
// 命令是确定性的纯归约：相同 (State, Command) 永远产生相同 (Decision)。
type State struct {
	JobID      proto.JobID
	Phase      proto.JobPhase
	BudgetDeadline time.Time

	rootID proto.TaskID
	tasks  map[proto.TaskID]*task

	// touched 记录当前命令修改过的节点身份（每次 Apply 开头清空），
	// 供持久层只 UPDATE 真正变化的行。
	touched map[proto.TaskID]struct{}

	// stats 仅用于证据/日志，DS 判定不依赖它们。
	spawnedEdges  int64 // 已登记因果边总数（不含根）
	signals       int64 // 已发送信号总数
	inFlight      int64 // ready+claimed 任务数（在途）
	reports       int64 // 被接受的完成报告数
	dupAcks       int64 // 被识别并忽略的重复/过期确认数
	unack         int64 // timed_out 任务数（未确认）

	now func() time.Time // 可注入时钟，便于确定性测试
}

// NewState 用根任务初始化一个作业的网络：根 engaged、无入边、无 deficit。
func NewState(jobID proto.JobID, rootSpec proto.TaskSpec, budgetDeadline time.Time, now time.Time) *State {
	rootSpec.JobID = jobID
	if rootSpec.TaskID == "" {
		rootSpec.TaskID = proto.TaskID(NewID("t"))
	}
	root := &task{
		spec:      rootSpec,
		status:    proto.StatusReady,
		engaged:   true, // DS：环境向网络注入根
		createdAt: now,
		updatedAt: now,
	}
	s := &State{
		JobID:          jobID,
		Phase:          proto.PhaseRunning,
		BudgetDeadline: budgetDeadline,
		rootID:         rootSpec.TaskID,
		tasks:          map[proto.TaskID]*task{rootSpec.TaskID: root},
		touched:        map[proto.TaskID]struct{}{},
		inFlight:       1,
		now:            func() time.Time { return now },
	}
	return s
}

// SetClock 注入时钟（仅测试/重放需要；默认取构造时刻）。
func (s *State) SetClock(now func() time.Time) { s.now = now }

// SetPhase 供存储在从物化行加载时恢复作业阶段。
func (s *State) SetPhase(p proto.JobPhase) { s.Phase = p }

// SetCounters 供存储从事件日志恢复派生计数。
func (s *State) SetCounters(spawned, signals, reports, dup, unack int64) {
	s.spawnedEdges = spawned
	s.signals = signals
	s.reports = reports
	s.dupAcks = dup
	s.unack = unack
	s.recomputeInFlight()
}

// InsertTask 供存储把非根任务行装入状态（加载路径）。
func (s *State) InsertTask(v TaskView) {
	s.insertView(v)
}

// ReplaceTask 供存储用物化行覆盖构造时放入的根任务。
func (s *State) ReplaceTask(v TaskView) {
	old := s.tasks[v.Spec.TaskID]
	createdAt := time.Time{}
	if old != nil {
		createdAt = old.createdAt
	}
	delete(s.tasks, v.Spec.TaskID)
	s.insertViewWithCreated(v, createdAt)
}

func (s *State) insertView(v TaskView) {
	s.insertViewWithCreated(v, time.Time{})
}

func (s *State) insertViewWithCreated(v TaskView, createdAt time.Time) {
	if createdAt.IsZero() {
		createdAt = s.cur()
	}
	s.tasks[v.Spec.TaskID] = &task{
		spec:       v.Spec,
		status:     v.Status,
		engaged:    v.Engaged,
		passive:    v.Passive,
		settled:    v.Settled,
		deficit:    v.Deficit,
		leaseID:    v.LeaseID,
		workerID:   v.WorkerID,
		leaseUntil: v.LeaseUntil,
		createdAt:  createdAt,
		updatedAt:  s.cur(),
	}
}

// recomputeInFlight 依据任务状态推导在途数（ready+claimed）。
func (s *State) recomputeInFlight() {
	var n int64
	for _, t := range s.tasks {
		if t.status == proto.StatusReady || t.status == proto.StatusClaimed {
			n++
		}
	}
	s.inFlight = n
}

// beginMutation 在每条命令开始时清空“本命令触及节点”集合。
func (s *State) beginMutation() { s.touched = map[proto.TaskID]struct{}{} }

func (s *State) touch(id proto.TaskID) { s.touched[id] = struct{}{} }

// Touched 返回本命令修改过的节点身份（新建/状态或计数变化）。
func (s *State) Touched() []proto.TaskID {
	out := make([]proto.TaskID, 0, len(s.touched))
	for id := range s.touched {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// RootID 返回根任务身份。
func (s *State) RootID() proto.TaskID { return s.rootID }

// taskView 是快照中的只读任务视图。
type TaskView struct {
	Spec       proto.TaskSpec
	Status     proto.TaskStatus
	Engaged    bool
	Passive    bool
	Settled    bool
	Deficit    int64
	LeaseID    proto.LeaseID
	WorkerID   proto.WorkerID
	LeaseUntil time.Time
}

// Snapshot 是状态的只读证据快照，用于持久化加载、重放核对与接口解释。
type Snapshot struct {
	JobID          proto.JobID
	Phase          proto.JobPhase
	BudgetDeadline time.Time
	RootID         proto.TaskID
	SpawnedEdges   int64
	Signals        int64
	InFlight       int64
	Reports        int64
	DupAcks        int64
	Unacknowledged int64
	Tasks          []TaskView
}

// Evidence 是“依赖计数证据”：把 DS 判据涉及的每个计数单列出来，
// 使“为什么此刻可以/不可以宣布完成”是可复核的，而不是黑盒结论。
type Evidence struct {
	KernelVersion  string         `json:"kernel_version"`
	JobID          proto.JobID    `json:"job_id"`
	Phase          proto.JobPhase `json:"phase"`
	RootID         proto.TaskID   `json:"root_id"`
	RootPassive    bool           `json:"root_passive"`     // 根本身工作已确认
	RootDeficit    int64          `json:"root_deficit"`     // 根的未清偿因果边
	RootSettled    bool           `json:"root_settled"`     // 根已结算（宣布终止）
	InFlight       int64          `json:"in_flight"`        // 在途任务 ready+claimed
	Engaged        int64          `json:"engaged_nodes"`    // 仍 engaged 的节点
	Unacknowledged int64          `json:"unacknowledged"`   // 超时未确认任务
	SpawnedEdges   int64          `json:"spawned_edges"`    // 因果边总数
	Signals        int64          `json:"signals"`          // 已清偿边总数
	OpenEdges      int64          `json:"open_edges"`       // spawned-signals，必须等于根deficit+非根engaged
	Reports        int64          `json:"reports_accepted"`
	DupAcks        int64          `json:"duplicate_acks_ignored"`
	ByTask         []TaskEvidence `json:"by_task"`
}

// TaskEvidence 是单个节点的依赖计数行。
type TaskEvidence struct {
	TaskID   proto.TaskID   `json:"task_id"`
	ParentID proto.TaskID   `json:"parent_id,omitempty"`
	Kind     proto.TaskKind `json:"kind"`
	Depth    int            `json:"depth"`
	Status   proto.TaskStatus `json:"status"`
	Engaged  bool           `json:"engaged"`
	Passive  bool           `json:"passive"`
	Settled  bool           `json:"settled"`
	Deficit  int64          `json:"deficit"`
}

// Evidence 产出依赖计数证据。
func (s *State) Evidence() Evidence {
	e := Evidence{
		KernelVersion: Version,
		JobID:         s.JobID,
		Phase:         s.Phase,
		RootID:        s.rootID,
		SpawnedEdges:  s.spawnedEdges,
		Signals:       s.signals,
		InFlight:      s.inFlight,
		Reports:       s.reports,
		DupAcks:       s.dupAcks,
		Unacknowledged: s.unack,
		OpenEdges:     s.spawnedEdges - s.signals,
	}
	for _, t := range s.tasks {
		if t.engaged {
			e.Engaged++
		}
		if t.spec.TaskID == s.rootID {
			e.RootPassive = t.passive
			e.RootDeficit = t.deficit
			e.RootSettled = t.settled
		}
		e.ByTask = append(e.ByTask, TaskEvidence{
			TaskID:   t.spec.TaskID,
			ParentID: t.spec.ParentID,
			Kind:     t.spec.Kind,
			Depth:    t.spec.Depth,
			Status:   t.status,
			Engaged:  t.engaged,
			Passive:  t.passive,
			Settled:  t.settled,
			Deficit:  t.deficit,
		})
	}
	sort.Slice(e.ByTask, func(i, j int) bool {
		if e.ByTask[i].Depth != e.ByTask[j].Depth {
			return e.ByTask[i].Depth < e.ByTask[j].Depth
		}
		return e.ByTask[i].TaskID < e.ByTask[j].TaskID
	})
	return e
}

// Snapshot 导出可序列化状态（供存储加载比对）。
func (s *State) Snapshot() Snapshot {
	snap := Snapshot{
		JobID:          s.JobID,
		Phase:          s.Phase,
		BudgetDeadline: s.BudgetDeadline,
		RootID:         s.rootID,
		SpawnedEdges:   s.spawnedEdges,
		Signals:        s.signals,
		InFlight:       s.inFlight,
		Reports:        s.reports,
		DupAcks:        s.dupAcks,
		Unacknowledged: s.unack,
	}
	for _, t := range s.tasks {
		snap.Tasks = append(snap.Tasks, TaskView{
			Spec:       t.spec,
			Status:     t.status,
			Engaged:    t.engaged,
			Passive:    t.passive,
			Settled:    t.settled,
			Deficit:    t.deficit,
			LeaseID:    t.leaseID,
			WorkerID:   t.workerID,
			LeaseUntil: t.leaseUntil,
		})
	}
	sort.Slice(snap.Tasks, func(i, j int) bool {
		return snap.Tasks[i].Spec.TaskID < snap.Tasks[j].Spec.TaskID
	})
	return snap
}

// Restore 从快照恢复状态（启动恢复 / 重放交叉核对用）。
func Restore(snap Snapshot, now func() time.Time) *State {
	if now == nil {
		now = time.Now
	}
	s := &State{
		JobID:          snap.JobID,
		Phase:          snap.Phase,
		BudgetDeadline: snap.BudgetDeadline,
		rootID:         snap.RootID,
		tasks:          make(map[proto.TaskID]*task, len(snap.Tasks)),
		touched:        map[proto.TaskID]struct{}{},
		spawnedEdges:   snap.SpawnedEdges,
		signals:        snap.Signals,
		inFlight:       snap.InFlight,
		reports:        snap.Reports,
		dupAcks:        snap.DupAcks,
		unack:          snap.Unacknowledged,
		now:            now,
	}
	for _, v := range snap.Tasks {
		s.tasks[v.Spec.TaskID] = &task{
			spec:       v.Spec,
			status:     v.Status,
			engaged:    v.Engaged,
			passive:    v.Passive,
			settled:    v.Settled,
			deficit:    v.Deficit,
			leaseID:    v.LeaseID,
			workerID:   v.WorkerID,
			leaseUntil: v.LeaseUntil,
		}
	}
	return s
}
