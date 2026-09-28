// 黑盒测试：仅通过标准库 HTTP/JSON 与真实服务二进制交互，
// 不 import 任何被测包；期望值在本文件内自行计算。
package blackbox_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

type client struct {
	base string
	http *http.Client
}

func newClient(base string) *client {
	return &client{base: base, http: &http.Client{Timeout: 10 * time.Second}}
}

// waitHealthy 非致命轮询健康端点（服务启动期间连接拒绝是正常现象）。
func (c *client) waitHealthy(deadline time.Time) bool {
	for time.Now().Before(deadline) {
		req, _ := http.NewRequest("GET", c.base+"/healthz", nil)
		resp, err := c.http.Do(req)
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return true
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	return false
}

func (c *client) do(t *testing.T, method, path, reqID string, body any) (int, []byte, http.Header) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.base+path, rdr)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if reqID != "" {
		req.Header.Set("X-Request-Id", reqID)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data, resp.Header
}

func (c *client) mustJSON(t *testing.T, method, path, reqID string, body any, wantCode int) map[string]any {
	t.Helper()
	code, data, hdr := c.do(t, method, path, reqID, body)
	if code != wantCode {
		t.Fatalf("%s %s: code=%d want %d body=%s (request_id=%s)",
			method, path, code, wantCode, string(data), hdr.Get("X-Request-Id"))
	}
	var m map[string]any
	if len(data) > 0 {
		if err := json.Unmarshal(data, &m); err != nil {
			t.Fatalf("decode %s: %v body=%s", path, err, string(data))
		}
	}
	return m
}

func (c *client) getJSON(t *testing.T, path string, wantCode int) []any {
	t.Helper()
	code, data, _ := c.do(t, "GET", path, "", nil)
	if code != wantCode {
		t.Fatalf("GET %s: code=%d want %d body=%s", path, code, wantCode, string(data))
	}
	var arr []any
	if err := json.Unmarshal(data, &arr); err != nil {
		t.Fatalf("decode %s: %v body=%s", path, err, string(data))
	}
	return arr
}

func (c *client) createApp(t *testing.T, name, version string, policy map[string]any, reqID string) map[string]any {
	body := map[string]any{"name": name, "version": version}
	for k, v := range policy {
		body[k] = v
	}
	return c.mustJSON(t, "POST", "/api/apps", reqID, body, http.StatusAccepted)
}

func (c *client) deploy(t *testing.T, name, version string, policy map[string]any, reqID string) map[string]any {
	body := map[string]any{"version": version}
	for k, v := range policy {
		body[k] = v
	}
	return c.mustJSON(t, "POST", fmt.Sprintf("/api/apps/%s/deployments", name), reqID, body, http.StatusAccepted)
}

func (c *client) rollback(t *testing.T, name, reqID string) map[string]any {
	return c.mustJSON(t, "POST", fmt.Sprintf("/api/apps/%s/rollback", name), reqID, nil, http.StatusAccepted)
}

func (c *client) appView(t *testing.T, name string) map[string]any {
	return c.mustJSON(t, "GET", "/api/apps/"+name, "", nil, http.StatusOK)
}

func (c *client) rolloutView(t *testing.T, id string) map[string]any {
	return c.mustJSON(t, "GET", "/api/rollouts/"+id, "", nil, http.StatusOK)
}

func (c *client) events(t *testing.T, id string) []any {
	return c.getJSON(t, "/api/rollouts/"+id+"/events", http.StatusOK)
}

func (c *client) procList(t *testing.T) []any {
	return c.getJSON(t, "/api/debug/procman/list", http.StatusOK)
}

func (c *client) wipeHost(t *testing.T) {
	c.mustJSON(t, "POST", "/api/debug/procman/wipe-host", "test-wipe", nil, http.StatusOK)
}

// tick 手动推进一个滴答，返回该滴答结果。
func (c *client) tick(t *testing.T, name, reqID string) map[string]any {
	return c.mustJSON(t, "POST", fmt.Sprintf("/api/apps/%s/reconcile", name), reqID, nil, http.StatusOK)
}

// driveUntil 持续推进直到发布离开 running/pending，或超过 maxTicks。
// 发布终态后再 reconcile 会返回 409 no_in_flight，这是正常结束信号。
func (c *client) driveUntil(t *testing.T, name string, maxTicks int) map[string]any {
	t.Helper()
	for i := 0; i < maxTicks; i++ {
		code, data, _ := c.do(t, "POST", fmt.Sprintf("/api/apps/%s/reconcile", name),
			fmt.Sprintf("tick-%s-%d", name, i+1), nil)
		if code == http.StatusOK {
			var res map[string]any
			if err := json.Unmarshal(data, &res); err != nil {
				t.Fatalf("decode reconcile: %v body=%s", err, string(data))
			}
			st, _ := res["status"].(string)
			if st == "succeeded" || st == "failed" {
				return res
			}
			continue
		}
		if code == http.StatusConflict {
			break // 已无在途发布：终态在 app 视图里
		}
		t.Fatalf("reconcile %s: code=%d body=%s", name, code, string(data))
	}
	v := c.appView(t, name)
	if l := v["last_rollout"]; l != nil {
		return l.(map[string]any)
	}
	t.Fatalf("rollout for %s has no terminal state after %d ticks", name, maxTicks)
	return nil
}

func asInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}

// ---------- 服务进程装配 ----------

var (
	serverBin string
)

// TestMain 在所有测试前构建一次真实服务二进制，放到所有用例共享的稳定目录
// （不能放 t.TempDir，否则首个用例结束后二进制被清理，后续用例无法启动）。
func TestMain(m *testing.M) {
	root := findRepoRoot()
	dir, err := os.MkdirTemp("", "rctl-blackbox-")
	if err != nil {
		fmt.Fprintln(os.Stderr, "mktemp:", err)
		os.Exit(1)
	}
	serverBin = filepath.Join(dir, "rctl-server")
	cmd := exec.Command("go", "build", "-o", serverBin, "./cmd/server")
	cmd.Dir = root
	if out, err := cmd.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "build server: %v\n%s\n", err, out)
		os.Exit(1)
	}
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

// findRepoRoot 从本文件位置向上找到主模块 go.mod。
func findRepoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	dir := filepath.Dir(file)
	for i := 0; i < 6; i++ {
		b, err := os.ReadFile(filepath.Join(dir, "go.mod"))
		if err == nil && bytes.Contains(b, []byte("module rollingdeploy\n")) {
			return dir
		}
		dir = filepath.Dir(dir)
	}
	panic("cannot locate repo root (module rollingdeploy)")
}

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

type fixtureConfig struct {
	capacity     int
	behaviors    map[string]map[string]int // version -> {"behavior": 见下, "parameter": n}
	behaviorKind map[string]string
}

// startServer 用临时目录 + 指定夹具启动一个真实服务进程；tick 间隔设大，
// 所有推进都走显式 /reconcile，保证测试确定性。
func startServer(t *testing.T, fx fixtureConfig) (*client, string, string, func()) {
	t.Helper()
	bin := serverBin
	dir := t.TempDir()

	behaviors := map[string]any{}
	for v, kind := range fx.behaviorKind {
		entry := map[string]any{"behavior": kind}
		if p, ok := fx.behaviors[v]; ok {
			if n, ok := p["parameter"]; ok {
				entry["parameter"] = n
			}
		}
		behaviors[v] = entry
	}
	cfg := map[string]any{
		"http_addr":               "127.0.0.1:0",
		"tick_interval":           "3600s",
		"data_dir":                dir,
		"fixture":                 map[string]any{"capacity": fx.capacity, "behaviors": behaviors},
		"default_max_surge":       1,
		"default_max_unavailable": 0,
		"default_ready_threshold": 2,
		"default_failure_limit":   2,
		"default_progress_ticks":  40,
	}
	cfgPath := filepath.Join(dir, "config.json")
	b, _ := json.MarshalIndent(cfg, "", "  ")
	if err := os.WriteFile(cfgPath, b, 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	addr := freePort(t)

	cmd := exec.Command(bin, "--config", cfgPath, "--addr", addr)
	logFile, err := os.Create(filepath.Join(dir, "server.log"))
	if err != nil {
		t.Fatalf("log file: %v", err)
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		t.Fatalf("start server: %v", err)
	}
	cl := newClient("http://" + addr)

	if !cl.waitHealthy(time.Now().Add(15 * time.Second)) {
		_ = logFile.Close()
		_ = cmd.Process.Kill()
		t.Fatalf("server never became healthy; see %s", logFile.Name())
	}

	cleanup := func() {
		_ = cmd.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { _ = cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			_ = cmd.Process.Kill()
			<-done
		}
		_ = logFile.Close()
	}
	stop := cleanup
	return cl, dir, addr, stop
}

// restartServer 用同一 dataDir 重启服务（控制器重启夹具）。
func restartServer(t *testing.T, dir, addr string, fx fixtureConfig) (*client, func()) {
	t.Helper()
	bin := serverBin
	cfgPath := filepath.Join(dir, "config.json")
	cmd := exec.Command(bin, "--config", cfgPath, "--addr", addr)
	logFile, err := os.Create(filepath.Join(dir, "server2.log"))
	if err != nil {
		t.Fatalf("log file: %v", err)
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		t.Fatalf("restart server: %v", err)
	}
	cl := newClient("http://" + addr)
	if !cl.waitHealthy(time.Now().Add(15 * time.Second)) {
		_ = cmd.Process.Kill()
		t.Fatalf("restarted server never became healthy; see %s", logFile.Name())
	}
	cleanup := func() {
		_ = cmd.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { _ = cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			_ = cmd.Process.Kill()
			<-done
		}
		_ = logFile.Close()
	}
	return cl, cleanup
}
