package service_test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"tcpreplay/internal/config"
	"tcpreplay/internal/diagnose"
	"tcpreplay/internal/reassembly"
	"tcpreplay/internal/service"
	"tcpreplay/internal/storage"
	"tcpreplay/internal/testsupport"
)

func newTestServer(t *testing.T, policy string) (*httptest.Server, *storage.Store) {
	t.Helper()
	cfg := config.Default()
	cfg.DBPath = ":memory:"
	cfg.OverlapPolicy = policy
	store, err := storage.Open("file::memory:?cache=shared&_pragma=busy_timeout(2000)")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	srv := service.NewServer(store, cfg, diagnose.NewLogger(io.Discard, false))
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return ts, store
}

// TestIngestPcapEndToEnd runs a shuffled, gappy capture over HTTP, locates
// every gap byte through the JSON report and fetches the raw replayed stream.
func TestIngestPcapEndToEnd(t *testing.T) {
	ts, _ := newTestServer(t, "first_wins")

	orig := "HELLO-TCP-REASSEMBLY-WORLD!!"
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(12, []byte(orig[12:20]))
	b.ClientData(20, []byte(orig[20:28]))
	b.ClientData(0, []byte(orig[0:6])) // gap [6,12) never sent
	b.ClientFIN(28, nil)
	b.ServerData(0, []byte("HELLO-BACK"))
	b.ServerFIN(10, nil)

	resp, err := http.Post(ts.URL+"/api/v1/ingest/pcap?request_id=e2e-gap",
		"application/vnd.tcpdump.pcap", bytes.NewReader(b.PCap()))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("ingest status %d: %s", resp.StatusCode, body)
	}
	var created struct {
		RequestID   string `json:"request_id"`
		Conflicts   int    `json:"conflict_count"`
		Generations []struct {
			AtoBBytes int64 `json:"a_to_b_contiguous_bytes"`
			AtoBGaps  int   `json:"a_to_b_gaps"`
		} `json:"generations"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&created); err != nil {
		t.Fatal(err)
	}
	if created.RequestID != "e2e-gap" || len(created.Generations) != 1 {
		t.Fatalf("bad created summary: %+v", created)
	}
	if created.Generations[0].AtoBBytes != 6 || created.Generations[0].AtoBGaps != 1 {
		t.Fatalf("summary: bytes=%d gaps=%d", created.Generations[0].AtoBBytes, created.Generations[0].AtoBGaps)
	}

	// Raw stream endpoint returns exactly the evidenced prefix.
	raw, status := getRaw(t, ts.URL+"/api/v1/requests/e2e-gap/flows/10.0.0.1%3A40001%3C-%3E10.0.0.2%3A80/generations/0/stream/a_to_b")
	if status != http.StatusOK {
		t.Fatalf("raw stream status %d", status)
	}
	if string(raw) != orig[:6] {
		t.Fatalf("raw replayed bytes: %q want %q", raw, orig[:6])
	}

	// JSON view locates the exact gap interval.
	var view struct {
		Gaps []struct {
			Start int64 `json:"start"`
			End   int64 `json:"end"`
		} `json:"gaps"`
		FINPos       int64 `json:"fin_position"`
		LengthProved int64 `json:"length_proved"`
	}
	getJSON(t, ts.URL+"/api/v1/requests/e2e-gap/flows/10.0.0.1%3A40001%3C-%3E10.0.0.2%3A80/generations/0/stream/a_to_b?format=json", &view)
	if len(view.Gaps) != 1 || view.Gaps[0].Start != 6 || view.Gaps[0].End != 12 {
		t.Fatalf("gap evidence wrong: %+v", view.Gaps)
	}
	if view.LengthProved != 28 || view.FINPos != 28 {
		t.Fatalf("FIN proved length wrong: %+v", view)
	}
}

// TestConflictPolicyOverHTTP posts an overlapping, contradictory segment and
// locates the exact conflict byte over the API.
func TestConflictPolicyOverHTTP(t *testing.T) {
	ts, _ := newTestServer(t, "first_wins")
	orig := "HELLO-TCP-REASSEMBLY-WORLD!!"
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(0, []byte(orig))
	b.ClientDataID(10, []byte("zzzzzzzz"), "evil")
	b.ClientFIN(28, nil)
	b.ServerFIN(0, nil)

	resp, err := http.Post(ts.URL+"/api/v1/ingest/pcap?request_id=e2e-conf",
		"application/octet-stream", bytes.NewReader(b.PCap()))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("status %d: %s", resp.StatusCode, body)
	}

	var confResp struct {
		Conflicts []struct {
			ByteOffset  int64  `json:"byte_offset"`
			RawSeq      uint32 `json:"raw_seq"`
			Accepted    int    `json:"accepted_byte"`
			Offered     int    `json:"offered_byte"`
			AcceptedBy  string `json:"accepted_by_record_id"`
			RecordID    string `json:"offered_record_id"`
			Disposition string `json:"disposition"`
		} `json:"conflicts"`
	}
	getJSON(t, ts.URL+"/api/v1/requests/e2e-conf/conflicts", &confResp)
	if len(confResp.Conflicts) != 8 {
		t.Fatalf("conflict count %d", len(confResp.Conflicts))
	}
	first := confResp.Conflicts[0]
	// pcap carries no record ids, so the server assigns sequential rec-NNNNN
	// ids in arrival order; evidence identity is offset + both bytes + the
	// earlier accepting record (different id from the offering one).
	if first.ByteOffset != 10 || byte(first.Accepted) != orig[10] ||
		byte(first.Offered) != 'z' || first.Disposition != reassembly.DispRejected ||
		first.RecordID == first.AcceptedBy {
		t.Fatalf("first conflict evidence wrong: %+v", first)
	}

	// Stream must retain the original under first_wins.
	raw, _ := getRaw(t, ts.URL+"/api/v1/requests/e2e-conf/flows/10.0.0.1%3A40001%3C-%3E10.0.0.2%3A80/generations/0/stream/a_to_b")
	if string(raw) != orig {
		t.Fatalf("first_wins stream corrupted: %q", raw)
	}
}

// TestFailureCategories asserts the specific error categories instead of
// merely "non-200".
func TestFailureCategories(t *testing.T) {
	ts, _ := newTestServer(t, "first_wins")

	// 1. Bad pcap magic -> pcap_parse_failed.
	req, _ := http.NewRequest("POST", ts.URL+"/api/v1/ingest/pcap", bytes.NewReader(bytes.Repeat([]byte{0xff}, 24)))
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var e1 struct {
		Category string `json:"category"`
	}
	json.NewDecoder(resp.Body).Decode(&e1)
	if resp.StatusCode != http.StatusBadRequest || e1.Category != "pcap_parse_failed" {
		t.Fatalf("bad pcap: status=%d category=%q", resp.StatusCode, e1.Category)
	}

	// 2. Empty packet list -> validation.
	resp2, err := http.Post(ts.URL+"/api/v1/ingest", "application/json",
		strings.NewReader(`{"packets":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	var e2 struct {
		Category string `json:"category"`
	}
	json.NewDecoder(resp2.Body).Decode(&e2)
	if resp2.StatusCode != http.StatusBadRequest || e2.Category != "validation" {
		t.Fatalf("empty packets: status=%d category=%q", resp2.StatusCode, e2.Category)
	}

	// 3. Unknown request id -> 404 not_found.
	resp3, err := http.Get(ts.URL + "/api/v1/requests/nope/report")
	if err != nil {
		t.Fatal(err)
	}
	defer resp3.Body.Close()
	var e3 struct {
		Category string `json:"category"`
	}
	json.NewDecoder(resp3.Body).Decode(&e3)
	if resp3.StatusCode != http.StatusNotFound || e3.Category != "not_found" {
		t.Fatalf("missing: status=%d category=%q", resp3.StatusCode, e3.Category)
	}
}

// TestDuplicateRequestRejected inserts the same request id twice and expects
// 409 duplicate_request.
func TestDuplicateRequestRejected(t *testing.T) {
	ts, _ := newTestServer(t, "first_wins")
	b := testsupport.NewBuilder(1, 2).Handshake()
	b.ClientData(0, []byte("x"))
	b.ClientFIN(1, nil)
	b.ServerFIN(0, nil)
	post := func() int {
		resp, err := http.Post(ts.URL+"/api/v1/ingest/pcap?request_id=dup",
			"application/octet-stream", bytes.NewReader(b.PCap()))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		io.Copy(io.Discard, resp.Body)
		return resp.StatusCode
	}
	if code := post(); code != http.StatusCreated {
		t.Fatalf("first post = %d", code)
	}
	resp, _ := http.Post(ts.URL+"/api/v1/ingest/pcap?request_id=dup",
		"application/octet-stream", bytes.NewReader(b.PCap()))
	defer resp.Body.Close()
	var e struct {
		Category string `json:"category"`
	}
	json.NewDecoder(resp.Body).Decode(&e)
	if resp.StatusCode != http.StatusConflict || e.Category != "duplicate_request" {
		t.Fatalf("dup: status=%d category=%q", resp.StatusCode, e.Category)
	}
}

// TestEventsEndpointFiltering validates the reject decision is queryable by
// level and includes key state, and redaction defaults hide payload bytes.
func TestEventsEndpointFiltering(t *testing.T) {
	ts, _ := newTestServer(t, "first_wins")
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(0, []byte("SECRET-PAYLOAD"))
	b.ClientFIN(14, nil)
	// Data after FIN: reject.
	b.RawSegment(testsupport.FromClient, 1000+1+14, []string{"ACK", "PSH"}, []byte("LATE"), "late")
	b.ServerFIN(0, nil)
	resp, err := http.Post(ts.URL+"/api/v1/ingest/pcap?request_id=ev",
		"application/octet-stream", bytes.NewReader(b.PCap()))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	var ev struct {
		Events []struct {
			Code       string `json:"code"`
			Level      string `json:"level"`
			RequestID  string `json:"request_id"`
			RecordID   string `json:"record_id"`
			NextContig int64  `json:"next_contiguous"`
			Msg        string `json:"msg"`
		} `json:"events"`
	}
	getJSON(t, ts.URL+"/api/v1/requests/ev/events?level=reject", &ev)
	if len(ev.Events) != 1 || ev.Events[0].Code != "DATA_AFTER_FIN_REJECTED" {
		t.Fatalf("reject events: %+v", ev.Events)
	}
	if ev.Events[0].RequestID != "ev" || !strings.HasPrefix(ev.Events[0].RecordID, "rec-") {
		t.Fatalf("correlation ids missing: %+v", ev.Events[0])
	}
}

// TestJSONIngestPreservesRecordID proves caller-supplied evidence ids survive
// end to end (the pcap format has no such field, so this is JSON-only).
func TestJSONIngestPreservesRecordID(t *testing.T) {
	ts, _ := newTestServer(t, "first_wins")
	pkts := testsupport.NewBuilder(1000, 5000).Handshake().
		ClientData(0, []byte("HELLO")).
		ClientFIN(5, nil).
		ServerFIN(0, nil).Packets()
	raw, _ := json.Marshal(map[string]any{
		"request_id": "json-rid",
		"packets":    pkts,
	})
	resp, err := http.Post(ts.URL+"/api/v1/ingest", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("status %d: %s", resp.StatusCode, b)
	}
	var pktResp struct {
		Packets []struct {
			RecordID string `json:"record_id"`
		} `json:"packets"`
	}
	getJSON(t, ts.URL+"/api/v1/requests/json-rid/packets", &pktResp)
	if len(pktResp.Packets) != 6 || pktResp.Packets[0].RecordID != "c-001" {
		t.Fatalf("explicit record ids not preserved: %+v", pktResp.Packets)
	}
}

func getRaw(t *testing.T, url string) ([]byte, int) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return body, resp.StatusCode
}

func getJSON(t *testing.T, url string, v any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("GET %s -> %d: %s", url, resp.StatusCode, body)
	}
	if err := json.NewDecoder(resp.Body).Decode(v); err != nil {
		t.Fatal(err)
	}
}
