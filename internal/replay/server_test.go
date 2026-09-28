package replay_test

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"ipreasm/internal/config"
	"ipreasm/internal/replay"
	"ipreasm/internal/store"
)

func testServer(t *testing.T) (*httptest.Server, *store.SQLiteStore) {
	t.Helper()
	cfg := config.Default()
	cfg.DBPath = filepath.Join(t.TempDir(), "server.db")
	st, err := store.Open(cfg.DBPath)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(replay.NewServer(cfg, st).Routes())
	t.Cleanup(func() { srv.Close(); st.Close() })
	return srv, st
}

func TestHealthz(t *testing.T) {
	srv, _ := testServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	var body map[string]any
	json.NewDecoder(resp.Body).Decode(&body)
	if body["status"] != "ok" {
		t.Fatalf("body=%v", body)
	}
	t.Logf("input=http GET /healthz run=server-health; verdict status=ok")
}

func TestPCAPReplayEndpoint(t *testing.T) {
	const runID = "http-pcap-001"
	srv, st := testServer(t)
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", "basic.pcap"))
	if err != nil {
		t.Fatal(err)
	}
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/replay/pcap?run_id="+runID, bytes.NewReader(raw))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("status=%d body=%s", resp.StatusCode, b)
	}
	var rep replay.Report
	if err := json.NewDecoder(resp.Body).Decode(&rep); err != nil {
		t.Fatal(err)
	}
	if rep.RunID != runID || len(rep.Completed) != 3 {
		t.Fatalf("run=%s completed=%d", rep.RunID, len(rep.Completed))
	}
	if rep.Stats.Duplicates != 1 || rep.Unfragmented != 1 {
		t.Fatalf("duplicates=%d unfragmented=%d", rep.Stats.Duplicates, rep.Unfragmented)
	}
	// persisted rows queryable through the run-scoped endpoint
	gresp, err := http.Get(srv.URL + "/v1/runs/" + runID + "/datagrams")
	if err != nil {
		t.Fatal(err)
	}
	defer gresp.Body.Close()
	var dgs struct {
		Datagrams []map[string]any `json:"datagrams"`
	}
	json.NewDecoder(gresp.Body).Decode(&dgs)
	if len(dgs.Datagrams) != 3 {
		t.Fatalf("stored datagrams=%d", len(dgs.Datagrams))
	}
	rows, _ := st.FragCount(runID)
	t.Logf("input=%s POST basic.pcap; verdict completed=%d dup=%d sqlite_rows_left=%d (want 0)",
		runID, len(rep.Completed), rep.Stats.Duplicates, rows)
	if rows != 0 {
		t.Fatalf("rows left=%d", rows)
	}
}

func TestFragmentsEndpoint(t *testing.T) {
	const runID = "http-frag-001"
	srv, _ := testServer(t)
	// two fragments covering 24 bytes, built independently here
	p0 := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
	p1 := []byte{16, 17, 18, 19, 20, 21, 22, 23}
	body := map[string]any{
		"fragments": []map[string]any{
			{"src": "10.0.0.1", "dst": "10.0.0.2", "proto": 17, "id": 77,
				"offset_units": 0, "more": true, "payload_b64": base64.StdEncoding.EncodeToString(p0)},
			{"src": "10.0.0.1", "dst": "10.0.0.2", "proto": 17, "id": 77,
				"offset_units": 2, "more": false, "payload_b64": base64.StdEncoding.EncodeToString(p1)},
		},
	}
	buf, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/replay/fragments?run_id="+runID, bytes.NewReader(buf))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("status=%d body=%s", resp.StatusCode, b)
	}
	var rep replay.Report
	json.NewDecoder(resp.Body).Decode(&rep)
	if len(rep.Completed) != 1 || rep.Completed[0].Length != 24 {
		t.Fatalf("completed=%+v", rep.Completed)
	}
	t.Logf("input=%s JSON 2 frags; verdict completed len=24 key=%s", runID, rep.Completed[0].Key)
}

func TestBadInputsAreExplicitFailures(t *testing.T) {
	srv, _ := testServer(t)

	// garbage pcap -> 400 with ok:false, not a success envelope
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/replay/pcap?run_id=http-bad-001", bytes.NewReader([]byte("not a pcap")))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("garbage pcap status=%d", resp.StatusCode)
	}
	var errBody map[string]any
	if err := json.Unmarshal(b, &errBody); err != nil || errBody["ok"] != false {
		t.Fatalf("error not explicit: %s", b)
	}
	t.Logf("input=http-bad-001 garbage pcap; verdict 400 ok=false error_code=%v", errBody["error_code"])

	// invalid run id -> 400
	req, _ = http.NewRequest(http.MethodPost, srv.URL+"/v1/replay/pcap?run_id=bad%20id", bytes.NewReader([]byte("x")))
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != 400 {
		t.Fatalf("bad run id status=%d", resp2.StatusCode)
	}

	// malformed fragment (bad address) -> 400
	body := []byte(`{"fragments":[{"src":"not-an-ip","dst":"10.0.0.2","proto":17,"id":1,"offset_units":0,"more":false,"payload_b64":""}]}`)
	req, _ = http.NewRequest(http.MethodPost, srv.URL+"/v1/replay/fragments?run_id=http-bad-002", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp3, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp3.Body.Close()
	if resp3.StatusCode != 400 {
		t.Fatalf("bad fragment status=%d", resp3.StatusCode)
	}
	t.Logf("input=http-bad-002 malformed JSON fragments; verdict 400 (unknown/bad input never reported success)")
}

func TestEventsEndpoint(t *testing.T) {
	srv, _ := testServer(t)
	raw, _ := os.ReadFile(filepath.Join("..", "..", "testdata", "overlap.pcap"))
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/replay/pcap?run_id=http-ev-001", bytes.NewReader(raw))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	ev, err := http.Get(srv.URL + "/v1/runs/http-ev-001/events")
	if err != nil {
		t.Fatal(err)
	}
	defer ev.Body.Close()
	var out struct {
		Events []map[string]any `json:"events"`
	}
	json.NewDecoder(ev.Body).Decode(&out)
	var rejected int
	for _, e := range out.Events {
		if e["category"] == "rejected" {
			rejected++
		}
	}
	if rejected != 2 { // overlap + poisoned late fragment
		t.Fatalf("rejected events=%d want 2", rejected)
	}
	t.Logf("input=http-ev-001 overlap.pcap; verdict audit rejected events=%d (overlap, then poisoned)", rejected)
}
