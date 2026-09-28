package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

// FaultCase is one asserted failure category observed through the real binary.
type FaultCase struct {
	Name     string `json:"name"`
	HTTPCode int    `json:"http_code"`
	Category string `json:"category"`
	Passed   bool   `json:"passed"`
	Mismatch string `json:"mismatch,omitempty"`
}

// FaultRunConfig locates the binary and its scratch space.
type FaultRunConfig struct {
	RepoDir string
	WorkDir string
	BinPath string
}

// RunFaultScenario starts a fresh binary per case (each on its own SQLite
// file so prior faults and fleet changes cannot leak between cases), injects
// one fault through the admin HTTP endpoint and asserts the exact failure
// category plus 409 status on the following reconcile.
func RunFaultScenario(cfg FaultRunConfig) ([]FaultCase, error) {
	bin := cfg.BinPath
	if bin == "" {
		var err error
		bin, err = buildBinary(cfg.RepoDir, cfg.WorkDir)
		if err != nil {
			return nil, err
		}
	}

	type spec struct {
		name, field, category string
		// setup describes the pre-tick state the case needs.
		replicas  int
		load      float64
		zeroFleet bool // reconcile a zero-sized fleet (zero policy path)
	}
	cases := []spec{
		{name: "metric_read", field: "metric_read", category: "METRIC_READ_FAILED", replicas: 2, load: 30},
		{name: "current_read", field: "current_read", category: "FLEET_READ_FAILED", replicas: 2, load: 30},
		{name: "set_replicas", field: "set_replicas", category: "ADAPTER_APPLY_FAILED", replicas: 2, load: 30},
		{name: "decision_append", field: "decision_append", category: "STORE_FAILED", replicas: 2, load: 30},
		{name: "history_read", field: "history_read", category: "STORE_FAILED", replicas: 2, load: 0},
		{name: "demand_read", field: "demand_read", category: "METRIC_READ_FAILED", zeroFleet: true},
	}

	var out []FaultCase
	for i, tc := range cases {
		fc := FaultCase{Name: tc.name, Category: tc.category}
		caseDir := filepath.Join(cfg.WorkDir, tc.name)
		if err := os.MkdirAll(caseDir, 0o755); err != nil {
			return nil, err
		}
		port, err := freePort()
		if err != nil {
			return nil, err
		}
		addr := "127.0.0.1:" + strconv.Itoa(port)
		dbPath := filepath.Join(caseDir, "fault.db")
		configPath := filepath.Join(caseDir, "config.json")
		if err := os.WriteFile(configPath, []byte(fmt.Sprintf(`{
  "target_load_per_instance": 10, "max_scale_up_factor": 2, "max_scale_up_floor": 1,
  "scale_down_stable_window_seconds": 60, "stale_skew_seconds": 30, "tolerance": 0.10,
  "min_fresh_fraction": 0.5, "min_replicas": 0, "max_replicas": 16, "bootstrap_replicas": 1,
  "http_addr": %q, "database_dsn": %q
}`, addr, "file:"+dbPath)), 0o644); err != nil {
			return nil, err
		}
		ctx, cancel := context.WithCancel(context.Background())
		proc, err := startBinary(ctx, bin, configPath)
		if err != nil {
			cancel()
			return nil, err
		}
		if err := waitReady(addr, 10*time.Second); err != nil {
			proc.kill()
			cancel()
			return nil, err
		}
		base := "http://" + addr
		client := &http.Client{Timeout: 5 * time.Second}
		at := int64(900000 + int64(i)*100)

		if tc.zeroFleet {
			// Fresh DB is already at 0; inject before the zero-policy demand read.
			if code, _, err := postJSON(client, base+"/v1/admin/faults", "",
				map[string]any{tc.field: "injected " + tc.name + " outage"}); err != nil || code != http.StatusOK {
				fc.Mismatch = fmt.Sprintf("fault set failed code=%d err=%v", code, err)
			}
		} else {
			if _, _, err := postJSON(client, base+"/v1/admin/seed", "",
				map[string]any{"replicas": tc.replicas}); err != nil {
				proc.kill()
				cancel()
				return nil, err
			}
			for n := 1; n <= tc.replicas; n++ {
				if _, _, err := postJSON(client, base+"/v1/metrics", "",
					map[string]any{
						"instance_id": fmt.Sprintf("instance-%03d", n),
						"load":        tc.load,
						"reported_at": at,
					}); err != nil {
					proc.kill()
					cancel()
					return nil, err
				}
			}
			if code, _, err := postJSON(client, base+"/v1/admin/faults", "",
				map[string]any{tc.field: "injected " + tc.name + " outage"}); err != nil || code != http.StatusOK {
				fc.Mismatch = fmt.Sprintf("fault set failed code=%d err=%v", code, err)
			}
		}

		if fc.Mismatch == "" {
			status, body, err := postReconcileAt(client, base+"/v1/reconcile", at)
			if err != nil {
				fc.Mismatch = err.Error()
			} else {
				fc.HTTPCode = status
				got, _ := body["category"].(string)
				fc.Passed = status == http.StatusConflict && got == tc.category
				if !fc.Passed {
					fc.Mismatch = fmt.Sprintf("got http=%d category=%q want http=409 category=%q (body=%v)",
						status, got, tc.category, compact(body))
				}
			}
		}
		proc.kill()
		cancel()
		out = append(out, fc)
	}
	return out, nil
}

func compact(v map[string]any) string {
	b, _ := json.Marshal(v)
	if len(b) > 200 {
		return string(b[:200]) + "..."
	}
	return string(b)
}

func postReconcileAt(client *http.Client, url string, at int64) (int, map[string]any, error) {
	b, _ := json.Marshal(map[string]any{"at": at})
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out, nil
}
