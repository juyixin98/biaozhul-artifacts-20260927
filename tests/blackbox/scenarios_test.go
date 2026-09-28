package blackbox_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
)

type snap struct {
	TotalActive  int `json:"total_active"`
	NewActive    int `json:"new_active"`
	OldActive    int `json:"old_active"`
	NewAvailable int `json:"new_available"`
	OldAvailable int `json:"old_available"`
	Available    int `json:"available"`
	Desired      int `json:"desired"`
	MaxTotal     int `json:"max_total"`
	MinAvailable int `json:"min_available"`
	Tick         int `json:"tick"`
}

type ev struct {
	ID         int64  `json:"id"`
	RequestID  string `json:"request_id"`
	Kind       string `json:"kind"`
	InstanceID string `json:"instance_id"`
	Note       string `json:"note"`
	Snapshot   snap   `json:"snapshot"`
}

func parseEvents(t *testing.T, raw []any) []ev {
	t.Helper()
	out := make([]ev, 0, len(raw))
	for _, item := range raw {
		b, err := json.Marshal(item)
		if err != nil {
			t.Fatalf("marshal event: %v", err)
		}
		var e ev
		if err := json.Unmarshal(b, &e); err != nil {
			t.Fatalf("unmarshal event: %v", err)
		}
		out = append(out, e)
	}
	return out
}

// assertTimeline 对事件时间线做独立的逐步副本约束走查。
func assertTimeline(t *testing.T, events []ev) {
	t.Helper()
	tracker := -1
	lastTick := 0
	for _, e := range events {
		if e.Snapshot.Tick == 0 {
			continue
		}
		if e.Snapshot.Tick != lastTick {
			lastTick = e.Snapshot.Tick
			tracker = -1
		}
		// 1) 任何步骤都不能超过最大额外副本。
		if e.Snapshot.TotalActive > e.Snapshot.MaxTotal {
			t.Fatalf("tick %d event %s: total_active=%d > max_total=%d (note=%q)",
				e.Snapshot.Tick, e.Kind, e.Snapshot.TotalActive, e.Snapshot.MaxTotal, e.Note)
		}
		switch e.Kind {
		case "tick_begin":
			tracker = e.Snapshot.Available
		case "become_ready":
			if e.Snapshot.Available != tracker+1 {
				t.Fatalf("tick %d become_ready: available=%d, want %d (must be exactly +1)",
					e.Snapshot.Tick, e.Snapshot.Available, tracker+1)
			}
			tracker = e.Snapshot.Available
		case "start_new":
			// 创建成功不算就绪：可用数不得变化。
			if e.Snapshot.Available != tracker {
				t.Fatalf("tick %d start_new: available changed %d->%d; create != ready",
					e.Snapshot.Tick, tracker, e.Snapshot.Available)
			}
		case "start_failed", "ready_demoted", "reattach", "rollout_failed":
			tracker = e.Snapshot.Available
		case "remove_old":
			// 2) 控制器主动缩容每一步都不得击穿最小可用线。
			if e.Snapshot.Available < e.Snapshot.MinAvailable {
				t.Fatalf("tick %d remove_old: available=%d < min_available=%d",
					e.Snapshot.Tick, e.Snapshot.Available, e.Snapshot.MinAvailable)
			}
			tracker = e.Snapshot.Available
		case "rollout_succeeded":
			tracker = e.Snapshot.Available
		}
	}
}

// bootstrap 创建应用并把首个版本发布成功，返回应用名。
func bootstrap(t *testing.T, c *client, name string, replicas int, policy map[string]any) {
	t.Helper()
	if policy == nil {
		policy = map[string]any{
			"replicas": replicas, "ready_threshold": 1,
			"max_surge": 1, "max_unavailable": 0, "failure_limit": 5,
		}
	}
	c.createApp(t, name, "v1", policy, "bootstrap-"+name)
	res := c.driveUntil(t, name, 60)
	if res["status"] != "succeeded" {
		t.Fatalf("bootstrap failed: %v", res)
	}
	v := c.appView(t, name)
	if asInt(v["replicas"]) != replicas {
		t.Fatalf("bootstrap replicas=%v want %d", v["replicas"], replicas)
	}
	if cv, _ := v["current_version"].(string); cv != "v1" {
		t.Fatalf("bootstrap current_version=%q want v1", cv)
	}
}

func countInstances(v map[string]any) (ready int, phases map[string]int, versions map[string]int) {
	phases = map[string]int{}
	versions = map[string]int{}
	for _, it := range v["instances"].([]any) {
		m := it.(map[string]any)
		phases[m["phase"].(string)]++
		versions[m["version"].(string)]++
		if m["phase"] == "ready" {
			ready++
		}
	}
	return
}

// terminalRolloutID 取应用最近一次发布（在途或终态）的 id。
func terminalRolloutID(t *testing.T, v map[string]any) string {
	t.Helper()
	if a := v["active_rollout"]; a != nil {
		return a.(map[string]any)["id"].(string)
	}
	if l := v["last_rollout"]; l != nil {
		return l.(map[string]any)["id"].(string)
	}
	t.Fatal("app view exposes neither active_rollout nor last_rollout")
	return ""
}

// TestBlackBoxStartFailure 夹具：新版本启动即失败。
// 断言具体结果：failed + start_failure；无任何新版本就绪；旧版本仍全部可用；
// 历史中每步满足副本约束；请求身份贯穿事件。
func TestBlackBoxStartFailure(t *testing.T) {
	c, _, _, cleanup := startServer(t, fixtureConfig{
		behaviorKind: map[string]string{"v1": "always_ok", "v2": "fail_start"},
	})
	defer cleanup()
	const D = 3
	bootstrap(t, c, "appfail", D, nil)

	c.deploy(t, "appfail", "v2", map[string]any{
		"ready_threshold": 1, "failure_limit": 2, "max_surge": 1, "max_unavailable": 0,
		"progress_ticks": 40,
	}, "req-fail-start-001")
	final := c.driveUntil(t, "appfail", 40)
	if final["status"] != "failed" {
		t.Fatalf("status=%v want failed (%v)", final["status"], final)
	}
	if final["failure_category"] != "start_failure" {
		t.Fatalf("failure_category=%v want start_failure", final["failure_category"])
	}
	if final["failure_reason"] == nil || final["failure_reason"] == "" {
		t.Fatal("failure_reason must explain the failure")
	}

	v := c.appView(t, "appfail")
	if asInt(v["snapshot"].(map[string]any)["new_available"]) != 0 {
		t.Fatalf("new_available must be 0 for fail_start, got %v", v["snapshot"])
	}
	ready, phases, versions := countInstances(v)
	if ready != D || versions["v1"] != D {
		t.Fatalf("old revision must stay fully available: ready=%d versions=%v phases=%v", ready, versions, phases)
	}
	if n := versions["v2"]; n != 0 {
		t.Fatalf("failed-start must leave no live v2 instances, got %d", n)
	}

	// 事件：拿到在途（现终态）发布 id 并逐步断言 + 请求身份关联。
	roID := terminalRolloutID(t, v)
	events := parseEvents(t, c.events(t, roID))
	assertTimeline(t, events)
	hasReqID, hasStartFailed := false, false
	for _, e := range events {
		if e.RequestID == "req-fail-start-001" {
			hasReqID = true
		}
		if e.Kind == "start_failed" {
			hasStartFailed = true
			if e.Note == "" {
				t.Fatal("start_failed event must carry a reason note")
			}
		}
	}
	if !hasReqID {
		t.Fatal("deploy request id was not correlated into rollout events")
	}
	if !hasStartFailed {
		t.Fatal("expected start_failed event in history")
	}
}

// TestBlackBoxReadinessJitter 夹具：探针周期抖动。
// 断言：ready_demoted 出现且最终成功；最终版本为 v2 且全部就绪；
// become_ready 严格 +1、start_new 绝不增加可用。
func TestBlackBoxReadinessJitter(t *testing.T) {
	c, _, _, cleanup := startServer(t, fixtureConfig{
		behaviorKind: map[string]string{"v1": "always_ok", "v2": "flaky"},
		behaviors:    map[string]map[string]int{"v2": {"parameter": 3}},
	})
	defer cleanup()
	const D = 2
	bootstrap(t, c, "appjitter", D, nil)

	c.deploy(t, "appjitter", "v2", map[string]any{
		"ready_threshold": 2, "failure_limit": 9, "max_surge": 1, "max_unavailable": 0,
		"progress_ticks": 120,
	}, "req-jitter-002")
	final := c.driveUntil(t, "appjitter", 120)
	if final["status"] != "succeeded" {
		t.Fatalf("status=%v reason=%v", final["status"], final["failure_reason"])
	}
	v := c.appView(t, "appjitter")
	if cv, _ := v["current_version"].(string); cv != "v2" {
		t.Fatalf("current_version=%q want v2", cv)
	}
	ready, _, versions := countInstances(v)
	if ready != D || versions["v2"] != D || versions["v1"] != 0 {
		t.Fatalf("final state ready=%d versions=%v want %d x v2", ready, versions, D)
	}
	roID := terminalRolloutID(t, v)
	events := parseEvents(t, c.events(t, roID))
	assertTimeline(t, events)
	demoted := 0
	for _, e := range events {
		if e.Kind == "ready_demoted" {
			demoted++
		}
	}
	if demoted == 0 {
		t.Fatal("flaky fixture must produce at least one ready_demoted event")
	}
}

// TestBlackBoxControllerRestart 夹具：滚动中途杀掉服务进程再以同一数据目录启动。
// 断言：重启后无重复/丢失进程，继续推进并最终成功，事件中可见 request id 关联。
func TestBlackBoxControllerRestart(t *testing.T) {
	fx := fixtureConfig{behaviorKind: map[string]string{"v1": "always_ok", "v2": "always_ok"}}
	c, dir, addr, stopOld := startServer(t, fx)
	defer stopOld()
	const D = 3
	bootstrap(t, c, "apprestart", D, nil)

	c.deploy(t, "apprestart", "v2", map[string]any{
		"ready_threshold": 2, "failure_limit": 5, "max_surge": 1, "max_unavailable": 0,
		"progress_ticks": 120,
	}, "req-restart-003")

	// 推进 3 个滴答后停止客户端侧记录。
	for i := 0; i < 3; i++ {
		c.tick(t, "apprestart", fmt.Sprintf("req-restart-pre-%d", i))
	}
	vBefore := c.appView(t, "apprestart")
	snapBefore := vBefore["snapshot"].(map[string]any)
	activeBefore := asInt(snapBefore["total_active"])
	procsBefore := len(c.procList(t))
	if activeBefore < D {
		t.Fatalf("pre-restart expected >= %d active, got %d", D, activeBefore)
	}

	// 停掉旧进程，再以同一数据目录、同一地址重启（控制器重启夹具）。
	stopOld()
	c2, cleanup2 := restartServer(t, dir, addr, fx)
	defer cleanup2()

	vAfter := c2.appView(t, "apprestart")
	snapAfter := vAfter["snapshot"].(map[string]any)
	if asInt(snapAfter["total_active"]) != activeBefore {
		t.Fatalf("restart changed active count: %d -> %d", activeBefore, asInt(snapAfter["total_active"]))
	}
	procsAfter := len(c2.procList(t))
	if procsAfter != procsBefore {
		t.Fatalf("restart duplicated/lost synthetic processes: %d -> %d", procsBefore, procsAfter)
	}

	final := c2.driveUntil(t, "apprestart", 80)
	if final["status"] != "succeeded" {
		t.Fatalf("post-restart status=%v reason=%v", final["status"], final["failure_reason"])
	}
	vEnd := c2.appView(t, "apprestart")
	if cv, _ := vEnd["current_version"].(string); cv != "v2" {
		t.Fatalf("current_version=%q want v2 after restart", cv)
	}
	ready, _, versions := countInstances(vEnd)
	if ready != D || versions["v2"] != D {
		t.Fatalf("post-restart final ready=%d versions=%v", ready, versions)
	}
}

// TestBlackBoxInsufficientCapacity 夹具：容量池只能容纳旧副本且不允许牺牲可用性。
// 断言：failed + insufficient_capacity（区别于 start_failure）；
// 全程不发生 remove_old；旧副本全部存活可用。
func TestBlackBoxInsufficientCapacity(t *testing.T) {
	const D = 3
	c, _, _, cleanup := startServer(t, fixtureConfig{
		capacity:     D,
		behaviorKind: map[string]string{"v1": "always_ok", "v2": "always_ok"},
	})
	defer cleanup()
	bootstrap(t, c, "appcap", D, nil)

	c.deploy(t, "appcap", "v2", map[string]any{
		"ready_threshold": 1, "failure_limit": 9, "max_surge": 1, "max_unavailable": 0,
		"progress_ticks": 8,
	}, "req-capacity-004")
	final := c.driveUntil(t, "appcap", 20)
	if final["status"] != "failed" {
		t.Fatalf("status=%v want failed", final["status"])
	}
	if final["failure_category"] != "insufficient_capacity" {
		t.Fatalf("failure_category=%v want insufficient_capacity", final["failure_category"])
	}
	v := c.appView(t, "appcap")
	ready, phases, versions := countInstances(v)
	if ready != D || versions["v1"] != D {
		t.Fatalf("capacity-exhausted must preserve old replicas: ready=%d versions=%v phases=%v",
			ready, versions, phases)
	}
	if versions["v2"] != 0 {
		t.Fatalf("no v2 process can exist when pool is full, got %d", versions["v2"])
	}
	roID := terminalRolloutID(t, v)
	events := parseEvents(t, c.events(t, roID))
	rejected, removed := 0, 0
	for _, e := range events {
		switch e.Kind {
		case "start_rejected":
			rejected++
		case "remove_old":
			removed++
		}
	}
	if rejected == 0 {
		t.Fatal("expected start_rejected events in history")
	}
	if removed != 0 {
		t.Fatalf("maxUnavailable=0 forbids removal under full pool, saw %d remove_old", removed)
	}
	assertTimeline(t, events)
}

// TestBlackBoxRollbackIsNewOperation 回退必须是一次新操作：新 rollout、新 revision 行，
// 历史完整保留（失败的那次仍在），最终版本恢复 v1。
func TestBlackBoxRollbackIsNewOperation(t *testing.T) {
	c, _, _, cleanup := startServer(t, fixtureConfig{
		behaviorKind: map[string]string{"v1": "always_ok", "v2": "fail_start"},
	})
	defer cleanup()
	const D = 2
	bootstrap(t, c, "apprb", D, nil)

	// v2 发布失败。
	c.deploy(t, "apprb", "v2", map[string]any{
		"ready_threshold": 1, "failure_limit": 1, "progress_ticks": 30,
	}, "req-rb-fail")
	failed := c.driveUntil(t, "apprb", 30)
	if failed["status"] != "failed" {
		t.Fatalf("expected v2 deploy to fail, got %v", failed)
	}
	failedRolloutID := terminalRolloutID(t, c.appView(t, "apprb"))

	// 发起回退：应被接受为新操作。
	rb := c.rollback(t, "apprb", "req-rb-go")
	if rb["op"] != "rollback" {
		t.Fatalf("rollback op=%v want rollback", rb["op"])
	}
	if rb["rollout_id"] == failedRolloutID {
		t.Fatal("rollback must create a new rollout id, not reuse the failed one")
	}
	final := c.driveUntil(t, "apprb", 60)
	if final["status"] != "succeeded" {
		t.Fatalf("rollback status=%v reason=%v", final["status"], final["failure_reason"])
	}
	v := c.appView(t, "apprb")
	if cv, _ := v["current_version"].(string); cv != "v1" {
		t.Fatalf("current_version=%q want v1 after rollback", cv)
	}
	ready, _, versions := countInstances(v)
	if ready != D || versions["v1"] != D {
		t.Fatalf("post-rollback ready=%d versions=%v", ready, versions)
	}

	// 发布历史必须同时保留：create(succeeded) / rollout(failed) / rollback(succeeded)。
	raw := c.getJSON(t, "/api/apps/apprb/rollouts", http.StatusOK)
	if len(raw) != 3 {
		t.Fatalf("rollout history length=%d want 3 (failed rollout must be retained)", len(raw))
	}
	ops := []string{}
	statuses := []string{}
	for _, r := range raw {
		m := r.(map[string]any)
		ops = append(ops, m["op"].(string))
		statuses = append(statuses, m["status"].(string))
	}
	wantOps := []string{"create", "rollout", "rollback"}
	for i := range wantOps {
		if ops[i] != wantOps[i] {
			t.Fatalf("history ops=%v want %v", ops, wantOps)
		}
	}
	if statuses[1] != "failed" || statuses[2] != "succeeded" {
		t.Fatalf("history statuses=%v want [succeeded failed succeeded]", statuses)
	}
}

// TestBlackBoxErrorSemantics 断言错误语义（HTTP 状态码 + 错误体类别），
// 而不是只检查“接口能调用”。
func TestBlackBoxErrorSemantics(t *testing.T) {
	c, _, _, cleanup := startServer(t, fixtureConfig{
		behaviorKind: map[string]string{"v1": "always_ok", "vslow": "flaky_first"},
		behaviors:    map[string]map[string]int{"vslow": {"parameter": 500}},
	})
	defer cleanup()

	// 400：非法字段（名称含空格）。
	code, body, hdr := c.do(t, "POST", "/api/apps", "req-err-400", map[string]any{"name": "Bad Name!", "version": "v1"})
	if code != http.StatusBadRequest {
		t.Fatalf("invalid app payload code=%d want 400", code)
	}
	var eb map[string]any
	_ = json.Unmarshal(body, &eb)
	if eb["error"] != "bad_request" || eb["request_id"] != "req-err-400" {
		t.Fatalf("error body=%v want bad_request + echoed request id", eb)
	}
	if hdr.Get("X-Request-Id") != "req-err-400" {
		t.Fatalf("response X-Request-Id=%q want req-err-400", hdr.Get("X-Request-Id"))
	}

	// 404：未知应用。
	code, _, _ = c.do(t, "GET", "/api/apps/nope", "", nil)
	if code != http.StatusNotFound {
		t.Fatalf("missing app code=%d want 404", code)
	}

	// 409：在途发布期间再次发起。先让一个永不就绪的版本停在 running。
	bootstrap(t, c, "appbusy", 2, nil)
	c.mustJSON(t, "POST", "/api/apps/appbusy/deployments", "req-busy-1",
		map[string]any{"version": "vslow", "ready_threshold": 2, "failure_limit": 9, "progress_ticks": 500},
		http.StatusAccepted)
	res := c.tick(t, "appbusy", "req-busy-tick")
	if res["status"] != "running" {
		t.Fatalf("expected slow rollout to stay running, got %v", res)
	}
	code, body, _ = c.do(t, "POST", "/api/apps/appbusy/deployments", "req-busy-2",
		map[string]any{"version": "v3"})
	if code != http.StatusConflict {
		t.Fatalf("concurrent deploy code=%d want 409 body=%s", code, body)
	}
}
