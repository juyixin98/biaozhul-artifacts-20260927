package server_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"fieldmerge/internal/adapter"
	"fieldmerge/internal/log"
	"fieldmerge/internal/reconcile"
	"fieldmerge/internal/server"
	"fieldmerge/internal/store"
)

type harness struct {
	t   *testing.T
	srv *httptest.Server
	st  *store.Store
	dir string
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "db.sqlite"), store.Limits{})
	if err != nil {
		t.Fatal(err)
	}
	ad := &adapter.FileAdapter{RootDir: filepath.Join(dir, "applied")}
	cfg := reconcile.DefaultConfig()
	cfg.DuePollEvery = 30 * time.Millisecond
	cfg.BaseBackoff = 10 * time.Millisecond
	loop := reconcile.New(st, ad, cfg, logx.New(nil, "http-test"))
	srv := server.New(st, loop, logx.New(nil, "http-test"), server.Options{})
	ts := httptest.NewServer(srv.Routes())
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() {
		cancel()
		ts.Close()
		loop.Stop()
		_ = st.Close()
	})
	loop.Start(ctx)
	return &harness{t: t, srv: ts, st: st, dir: dir}
}

func (h *harness) do(method, path, body string, headers ...[2]string) (int, map[string]any, string) {
	h.t.Helper()
	var rdr io.Reader
	if body != "" {
		rdr = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, h.srv.URL+path, rdr)
	if err != nil {
		h.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	for _, hh := range headers {
		req.Header.Set(hh[0], hh[1])
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var parsed map[string]any
	_ = json.Unmarshal(raw, &parsed)
	return resp.StatusCode, parsed, resp.Header.Get("X-Run-Id")
}

func putSchema(t *testing.T, h *harness, kind, body string) {
	t.Helper()
	status, resp, _ := h.do(http.MethodPut, "/v1/schemas/"+kind, body)
	if status != http.StatusOK {
		t.Fatalf("put schema: status=%d body=%v", status, resp)
	}
}

func TestHTTP_EndToEndApplyConflictForceAndAudit(t *testing.T) {
	h := newHarness(t)
	putSchema(t, h, "widget", `{"lists":{"tags":{"type":"set"},"ingresses":{"type":"map","key":"name"}}}`)

	applyBody := func(manager string, force bool, config string) string {
		b := map[string]any{"manager": manager, "config": raw(config)}
		if force {
			b["force"] = true
		}
		bb, _ := json.Marshal(b)
		return string(bb)
	}

	// net establishes baseline.
	status, resp, run1 := h.do(http.MethodPost, "/v1/widget/w1/apply",
		applyBody("net", false, `{"image":"v1","tags":["a"]}`))
	if status != http.StatusOK {
		t.Fatalf("first apply status=%d body=%v", status, resp)
	}
	if run1 == "" {
		t.Fatal("every response must carry X-Run-Id")
	}
	if claims := resp["ownership"].([]any); len(claims) != 2 {
		t.Fatalf("want 2 ownership claims, got %v", resp["ownership"])
	}

	// sre conflicts on image.
	status, resp, run2 := h.do(http.MethodPost, "/v1/widget/w1/apply",
		applyBody("sre", false, `{"image":"v2"}`))
	if status != http.StatusConflict {
		t.Fatalf("conflict status = %d, want 409; body=%v", status, resp)
	}
	errObj := resp["error"].(map[string]any)
	if errObj["category"] != "conflict" || errObj["code"] != "ownership_conflict" {
		t.Fatalf("error contract wrong: %v", errObj)
	}
	details := errObj["details"].(map[string]any)
	conflicts := details["conflicts"].([]any)
	c0 := conflicts[0].(map[string]any)
	if c0["path"] != "image" {
		t.Fatalf("conflict path = %v", c0["path"])
	}
	owners := c0["owners"].([]any)
	if owners[0] != "net" {
		t.Fatalf("conflict must report original manager net, got %v", owners)
	}
	if run2 == run1 {
		t.Fatal("each request must get its own run id by default")
	}

	// Client-supplied run id must be honored for retry correlation.
	status, _, runClient := h.do(http.MethodPost, "/v1/widget/w1/apply",
		applyBody("sre", false, `{"image":"v2"}`), [2]string{"X-Run-Id", "client-retry-7"})
	if status != http.StatusConflict || runClient != "client-retry-7" {
		t.Fatalf("client run id not honored: status=%d run=%s", status, runClient)
	}

	// Force takes over.
	status, resp, _ = h.do(http.MethodPost, "/v1/widget/w1/apply",
		applyBody("sre", true, `{"image":"v2"}`))
	if status != http.StatusOK {
		t.Fatalf("force apply status=%d body=%v", status, resp)
	}
	live := resp["live"].(map[string]any)
	if live["image"] != "v2" {
		t.Fatalf("live after force = %v", live)
	}
	tags := live["tags"].([]any)
	if len(tags) != 1 || tags[0] != "a" {
		t.Fatalf("net's unrelated tag must survive: %v", tags)
	}

	// Ownership and history endpoints reflect the same committed state.
	status, ownResp, _ := h.do(http.MethodGet, "/v1/widget/w1/ownership", "")
	if status != 200 {
		t.Fatalf("ownership status=%d", status)
	}
	claims := ownResp["ownership"].([]any)
	ownerOf := func(path string) []any {
		for _, c := range claims {
			cm := c.(map[string]any)
			if cm["path"] == path {
				return cm["managers"].([]any)
			}
		}
		return nil
	}
	if m := ownerOf("image"); len(m) != 1 || m[0] != "sre" {
		t.Fatalf("image owners = %v, want [sre]", m)
	}
	if m := ownerOf(`tags[^"a"]`); len(m) != 1 || m[0] != "net" {
		t.Fatalf("tag owners = %v, want [net]", m)
	}

	status, histResp, _ := h.do(http.MethodGet, "/v1/widget/w1/history", "")
	if status != 200 {
		t.Fatalf("history status=%d", status)
	}
	hist := histResp["history"].([]any)
	// Successful applies: 2 (net baseline + sre force); conflict applies do not
	// write history rows.
	if len(hist) != 2 {
		t.Fatalf("history rows = %d, want 2 (conflicts not audited as commits)", len(hist))
	}
}

func TestHTTP_ErrorCategoryStatusCodes(t *testing.T) {
	h := newHarness(t)

	cases := []struct {
		name       string
		method     string
		path       string
		body       string
		wantStatus int
		wantCat    string
		wantCode   string
	}{
		{"missing resource", http.MethodGet, "/v1/widget/nope", "", 404, "not_found", "resource_missing"},
		{"bad json", http.MethodPost, "/v1/widget/x/apply", `{bad`, 400, "invalid_input", "bad_json"},
		{"missing manager", http.MethodPost, "/v1/widget/x/apply", `{"config":{}}`, 400, "invalid_input", "manager_required"},
		{"bad path", http.MethodGet, "/v1/", "", 400, "invalid_input", "bad_path"},
		{"wrong method", http.MethodDelete, "/v1/widget/x", "", 400, "invalid_input", "method_not_allowed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, resp, _ := h.do(tc.method, tc.path, tc.body)
			if status != tc.wantStatus {
				t.Fatalf("status=%d want %d body=%v", status, tc.wantStatus, resp)
			}
			if tc.wantCat != "" {
				errObj := resp["error"].(map[string]any)
				if errObj["category"] != tc.wantCat || errObj["code"] != tc.wantCode {
					t.Fatalf("error = %v, want %s/%s", errObj, tc.wantCat, tc.wantCode)
				}
			}
		})
	}
}

func TestHTTP_PayloadTooLarge(t *testing.T) {
	h := newHarness(t)
	// Server default body cap is 2 MiB.
	big := bytes.Repeat([]byte("a"), 3*1024*1024)
	body := `{"manager":"net","config":"` + string(big) + `"}`
	status, resp, _ := h.do(http.MethodPost, "/v1/widget/x/apply", body)
	if status != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", status)
	}
	if resp["error"].(map[string]any)["category"] != "resource_exhausted" {
		t.Fatalf("body = %v", resp)
	}
}

func TestHTTP_ReconcileReachesSynced(t *testing.T) {
	h := newHarness(t)
	status, resp, _ := h.do(http.MethodPost, "/v1/widget/sync-me/apply",
		`{"manager":"net","config":{"image":"v1"}}`)
	if status != 200 {
		t.Fatalf("apply: %d %v", status, resp)
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		status, got, _ := h.do(http.MethodGet, "/v1/widget/sync-me", "")
		if status == 200 && got["status"] == "synced" {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("resource never reached synced via loop")
}

func raw(s string) json.RawMessage { return json.RawMessage(s) }
