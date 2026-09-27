package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/fixture"
	"tcpreasm/internal/httpapi"
	"tcpreasm/internal/reassembly"
	"tcpreasm/internal/store"
	"tcpreasm/internal/tcpmodel"
)

func newTestServer(t *testing.T, policy config.OverlapPolicy) (*httptest.Server, *store.Store) {
	t.Helper()
	cfg := config.Default()
	cfg.Storage.DSN = ":memory:"
	if policy != "" {
		cfg.Reassembly.OverlapPolicy = policy
	}
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	sink := diag.NewSink(io.Discard, st, true)
	eng := reassembly.NewEngine(reassembly.NewOptions(cfg, st, sink))
	srv := &httpapi.Server{Engine: eng, Store: st, Cfg: cfg}
	return httptest.NewServer(srv.NewRouter()), st
}

func ingestJSONL(t *testing.T, base, requestID, body string) map[string]any {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, base+"/v1/ingest/jsonl", strings.NewReader(body))
	req.Header.Set("X-Request-Id", requestID)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("ingest status %d: %s", resp.StatusCode, raw)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func captureBody(t *testing.T, bl fixture.Built) string {
	t.Helper()
	var sb strings.Builder
	for _, p := range fixture.ToModelAll(bl.Flow, bl.Packets) {
		line, _ := json.Marshal(p)
		sb.Write(line)
		sb.WriteByte('\n')
	}
	return sb.String()
}

func TestHealthz(t *testing.T) {
	srv, _ := newTestServer(t, "")
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz %d", resp.StatusCode)
	}
}

func TestIngestAndReplayStream(t *testing.T) {
	srv, _ := newTestServer(t, "")
	defer srv.Close()
	bl := fixture.InOrder()
	ingestJSONL(t, srv.URL, "req-http", captureBody(t, bl))

	resp, err := http.Get(srv.URL + "/v1/connections")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var conns struct {
		Connections []map[string]any `json:"connections"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&conns); err != nil {
		t.Fatal(err)
	}
	if len(conns.Connections) != 1 {
		t.Fatalf("want 1 connection, got %d", len(conns.Connections))
	}
	gens := conns.Connections[0]["generations"].([]any)
	if len(gens) != 1 {
		t.Fatalf("want 1 generation, got %d", len(gens))
	}

	// Query the c2s stream and compare with the oracle answer.
	flowKey := conns.Connections[0]["flow_key"].(string)
	u := srv.URL + "/v1/stream?flow_key=" + flowKey + "&gen=1&direction=c2s&start=0&end=40"
	resp2, err := http.Get(u)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var st struct {
		DataHex    string `json:"data_hex"`
		Contiguous bool   `json:"contiguous"`
	}
	if err := json.NewDecoder(resp2.Body).Decode(&st); err != nil {
		t.Fatal(err)
	}
	if !st.Contiguous {
		t.Fatal("clean stream must be contiguous")
	}
	if want := bl.Spec.C2SStreamHex; st.DataHex != want {
		t.Fatalf("stream mismatch\n got %s\nwant %s", st.DataHex, want)
	}
}

func TestIngestBadJSONRejected(t *testing.T) {
	srv, _ := newTestServer(t, "")
	defer srv.Close()
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/ingest", bytes.NewReader([]byte("{not json")))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
}

func TestGapAndConflictEndpoints(t *testing.T) {
	srv, _ := newTestServer(t, config.PolicyFirstWins)
	defer srv.Close()
	bl := fixture.MissingSegments()
	ingestJSONL(t, srv.URL, "req-gaps", captureBody(t, bl))

	conns := listConnections(t, srv.URL)
	flowKey := conns[0]["flow_key"].(string)
	resp, err := http.Get(srv.URL + "/v1/gaps?flow_key=" + flowKey + "&gen=1&status=open")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var gaps struct {
		Gaps []struct {
			Direction string `json:"direction"`
			StartOff  uint64 `json:"start_off"`
			EndOff    uint64 `json:"end_off"`
			Status    string `json:"status"`
		} `json:"gaps"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&gaps); err != nil {
		t.Fatal(err)
	}
	if len(gaps.Gaps) != 2 {
		t.Fatalf("want 2 open gaps, got %d: %+v", len(gaps.Gaps), gaps.Gaps)
	}
	want := map[string][2]uint64{"c2s": {30, 45}, "s2c": {15, 30}}
	for _, g := range gaps.Gaps {
		w := want[g.Direction]
		if g.StartOff != w[0] || g.EndOff != w[1] {
			t.Fatalf("%s gap [%d,%d) want [%d,%d)", g.Direction, g.StartOff, g.EndOff, w[0], w[1])
		}
	}

	// Raw format must expose the bytes and the contiguity header.
	u := srv.URL + "/v1/stream?flow_key=" + flowKey + "&gen=1&direction=c2s&start=0&end=60&format=raw"
	r, err := http.Get(u)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(r.Body)
	r.Body.Close()
	if len(body) != 30 {
		t.Fatalf("only the 30-byte contiguous prefix must be returned, got %d", len(body))
	}
}

func TestConflictEndpointAndRequestID(t *testing.T) {
	srv, _ := newTestServer(t, config.PolicyQuarantine)
	defer srv.Close()
	bl := fixture.ConflictingRetransmission()
	ingestJSONL(t, srv.URL, "req-conf", captureBody(t, bl))

	conns := listConnections(t, srv.URL)
	flowKey := conns[0]["flow_key"].(string)
	resp, err := http.Get(srv.URL + "/v1/conflicts?flow_key=" + flowKey + "&gen=1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var cs struct {
		Conflicts []map[string]any `json:"conflicts"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&cs); err != nil {
		t.Fatal(err)
	}
	if len(cs.Conflicts) != 1 {
		t.Fatalf("want 1 conflict, got %d", len(cs.Conflicts))
	}
	if cs.Conflicts[0]["start_off"].(float64) != 10 || cs.Conflicts[0]["end_off"].(float64) != 11 {
		t.Fatalf("conflict offsets wrong: %v", cs.Conflicts[0])
	}

	// Diagnostics must be scoped by request id.
	dr, err := http.Get(srv.URL + "/v1/diagnostics?request_id=req-conf&category=UNDECIDABLE_CONFLICT_AGAINST_DELIVERED")
	if err != nil {
		t.Fatal(err)
	}
	defer dr.Body.Close()
	var diagResp struct {
		Diagnostics []map[string]any `json:"diagnostics"`
	}
	if err := json.NewDecoder(dr.Body).Decode(&diagResp); err != nil {
		t.Fatal(err)
	}
	if len(diagResp.Diagnostics) == 0 {
		t.Fatal("expected ledger diagnostics scoped to request id")
	}
}

func TestRepeatedIngestIsIdempotent(t *testing.T) {
	srv, _ := newTestServer(t, "")
	defer srv.Close()
	bl := fixture.InOrder()
	body := captureBody(t, bl)
	first := ingestJSONL(t, srv.URL, "req-a", body)
	second := ingestJSONL(t, srv.URL, "req-a", body)
	// The second ingestion marks every record as a duplicate packet.
	verdicts := second["verdicts"].([]any)
	dups := 0
	for _, v := range verdicts {
		if b, _ := v.(map[string]any)["duplicate_packet"].(bool); b {
			dups++
		}
	}
	if int(first["accepted"].(float64)) == 0 {
		t.Fatal("first ingestion should accept packets")
	}
	if dups != len(verdicts) {
		t.Fatalf("all %d replayed packets must be duplicates, got %d", len(verdicts), dups)
	}
}

func TestJSONBatchIngest(t *testing.T) {
	srv, _ := newTestServer(t, "")
	defer srv.Close()
	bl := fixture.WrapBoundary()
	models := fixture.ToModelAll(bl.Flow, bl.Packets)
	payload := map[string]any{"request_id": "batch-1", "packets": models}
	raw, _ := json.Marshal(payload)
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/ingest", bytes.NewReader(raw))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("batch ingest %d: %s", resp.StatusCode, b)
	}
	var out struct {
		Accepted int `json:"accepted"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out.Accepted != len(models) {
		t.Fatalf("accepted %d want %d", out.Accepted, len(models))
	}
}

func listConnections(t *testing.T, base string) []map[string]any {
	t.Helper()
	resp, err := http.Get(base + "/v1/connections")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Connections []map[string]any `json:"connections"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out.Connections
}

var _ = tcpmodel.DirC2S
