// Package procman 是本地合成的“进程管理器”适配器后端。
//
// 它在一个 JSON 文件里维护模拟进程状态：Start/Status/Stop 都落盘，
// 因此控制器重启后可以重新挂载已有进程（或发现进程已随宿主消失）。
// 行为完全由 config.Fixture 决定，不需要任何真实业务依赖。
package procman

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"rollingdeploy/internal/config"
	"rollingdeploy/internal/model"
)

// ErrCapacity 表示容量池已满，新建被拒绝（明确区别于启动失败）。
var ErrCapacity = errors.New("procman: capacity exhausted")

// Fail 是带类别的进程管理器错误。
type Fail struct {
	Kind string // "start_error" | "crashed" | "not_found"
	Msg  string
}

func (e *Fail) Error() string { return fmt.Sprintf("procman(%s): %s", e.Kind, e.Msg) }

// IsStartError 报告错误是否属于“新实例启动/崩溃失败”类别。
func IsStartError(err error) bool {
	var f *Fail
	if errors.As(err, &f) {
		return f.Kind == "start_error" || f.Kind == "crashed"
	}
	return false
}

// Status 是单个模拟进程的观测结果。
type Status struct {
	ProcID        string
	AppVersion    string // scope，形如 "appName@version"
	Behavior      string
	Running       bool // 进程是否存活
	Ready         bool // 就绪探针当前是否成功
	Checks        int  // 该进程累计被探测次数
	StartObserved bool // Start 是否已被后续探针观察到（慢启动）
	Missing       bool // 控制器记录了它，但状态文件里不存在（宿主重启丢失）
}

// proc 是落盘的进程记录。
type proc struct {
	ProcID     string `json:"proc_id"`
	AppVersion string `json:"app_version"`
	Behavior   string `json:"behavior"`
	Parameter  int    `json:"parameter"`
	StartDelay int    `json:"start_delay_checks"`
	Checks     int    `json:"checks"`
	Crashed    bool   `json:"crashed"` // 已崩溃（存活期结束）
}

type fileState struct {
	Procs map[string]*proc `json:"procs"`
}

// Manager 是模拟进程管理器。
type Manager struct {
	mu        sync.Mutex
	path      string
	capacity  int
	behaviors map[string]config.VersionBehavior
}

// New 创建/加载管理器。stateFile 不存在时按空环境初始化。
func New(stateFile string, fx config.Fixture) (*Manager, error) {
	m := &Manager{path: stateFile, capacity: fx.Capacity, behaviors: fx.Behaviors}
	if err := os.MkdirAll(filepath.Dir(stateFile), 0o755); err != nil {
		return nil, err
	}
	if _, err := os.Stat(stateFile); errors.Is(err, os.ErrNotExist) {
		if err := m.persist(&fileState{Procs: map[string]*proc{}}); err != nil {
			return nil, err
		}
	}
	return m, nil
}

// Reset 清空全部模拟进程（供独立测试隔离夹具）。
func (m *Manager) Reset() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.persist(&fileState{Procs: map[string]*proc{}})
}

// WipeHost 模拟“宿主重启”：删除底层状态文件全部进程，
// 控制器之后对旧 ProcID 的 Status 观测将得到 Missing=true。
func (m *Manager) WipeHost() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.persist(&fileState{Procs: map[string]*proc{}})
}

func newID() string {
	var b [6]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

func (m *Manager) load() (*fileState, error) {
	b, err := os.ReadFile(m.path)
	if err != nil {
		return nil, err
	}
	var st fileState
	if len(b) == 0 {
		st.Procs = map[string]*proc{}
	} else if err := json.Unmarshal(b, &st); err != nil {
		return nil, err
	}
	if st.Procs == nil {
		st.Procs = map[string]*proc{}
	}
	return &st, nil
}

func (m *Manager) persist(st *fileState) error {
	tmp := m.path + ".tmp"
	b, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, m.path)
}

// Start 在指定版本作用域内创建一个模拟进程。
// 返回 ErrCapacity 表示容量拒绝；返回 *Fail(Kind=start_error) 表示启动即失败。
// 成功返回 ProcID。
func (m *Manager) Start(appName, version string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, err := m.load()
	if err != nil {
		return "", err
	}
	scope := appName + "@" + version
	beh := config.VersionBehavior{Behavior: model.BehaviorAlwaysOK}
	if b, ok := m.behaviors[version]; ok {
		beh = b
	}
	alive := 0
	for _, p := range st.Procs {
		if !p.Crashed {
			alive++
		}
	}
	if m.capacity > 0 && alive >= m.capacity {
		return "", ErrCapacity
	}
	if beh.Behavior == model.BehaviorFailStart {
		// 启动即失败：进程不留存活记录，返回确定的启动错误。
		return "", &Fail{Kind: "start_error", Msg: "synthetic fixture: binary exits on start for version " + version}
	}
	p := &proc{
		ProcID:     newID(),
		AppVersion: scope,
		Behavior:   beh.Behavior,
		Parameter:  beh.Parameter,
		StartDelay: beh.StartDelayChecks,
	}
	st.Procs[p.ProcID] = p
	if err := m.persist(st); err != nil {
		return "", err
	}
	return p.ProcID, nil
}

// Status 观测进程。Missing=true 表示控制器侧的引用在模拟器中已不存在。
func (m *Manager) Status(procID string) (Status, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, err := m.load()
	if err != nil {
		return Status{}, err
	}
	p, ok := st.Procs[procID]
	if !ok {
		return Status{ProcID: procID, Missing: true}, nil
	}

	// 每观测一次推进一个探针节拍。
	p.Checks++
	switch p.Behavior {
	case model.BehaviorCrashAfter:
		if p.Checks > p.Parameter {
			p.Crashed = true
		}
	}
	if err := m.persist(st); err != nil {
		return Status{}, err
	}

	if p.Crashed {
		return Status{
			ProcID: p.ProcID, AppVersion: p.AppVersion, Behavior: p.Behavior,
			Running: false, Ready: false, Checks: p.Checks,
		}, &Fail{Kind: "crashed", Msg: fmt.Sprintf("process crashed after %d checks", p.Parameter)}
	}

	ready := false
	switch p.Behavior {
	case model.BehaviorAlwaysOK, model.BehaviorCrashAfter:
		ready = true
	case model.BehaviorFlaky:
		// 每 Parameter 次探针有一次失败（parameter<=1 时永远失败）。
		ready = p.Parameter <= 0 || p.Checks%p.Parameter != 0
	case model.BehaviorFlakyFirst:
		ready = p.Checks > p.Parameter
	}
	startObserved := p.StartDelay <= 0 || p.Checks > p.StartDelay
	return Status{
		ProcID: p.ProcID, AppVersion: p.AppVersion, Behavior: p.Behavior,
		Running: true, Ready: ready && startObserved, Checks: p.Checks,
		StartObserved: startObserved,
	}, nil
}

// Stop 终止进程；不存在不算错误（幂等）。
func (m *Manager) Stop(procID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, err := m.load()
	if err != nil {
		return err
	}
	delete(st.Procs, procID)
	return m.persist(st)
}

// PathForTest 返回状态文件路径（测试模拟控制器重启用）。
func (m *Manager) PathForTest() string { return m.path }

// LiveCountForTest 返回当前存活模拟进程数（测试用）。
func (m *Manager) LiveCountForTest() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, err := m.load()
	if err != nil {
		return -1
	}
	n := 0
	for _, p := range st.Procs {
		if !p.Crashed {
			n++
		}
	}
	return n
}

// List 按作用域前缀列出存活/崩溃进程（调试与演示用）。
func (m *Manager) List() ([]Status, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	st, err := m.load()
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(st.Procs))
	for id := range st.Procs {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]Status, 0, len(ids))
	for _, id := range ids {
		p := st.Procs[id]
		out = append(out, Status{
			ProcID: id, AppVersion: p.AppVersion, Behavior: p.Behavior,
			Running: !p.Crashed, Checks: p.Checks,
		})
	}
	return out, nil
}
