package transport

import (
	"encoding/base64"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"dhcpv4lab/internal/config"
	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/server"
	"dhcpv4lab/internal/storage"
)

func startHTTPEnv(t *testing.T) (*httptest.Server, *server.Server, *server.FakeClock) {
	t.Helper()
	cfg := config.Default()
	cfg.Store.DSN = "file:" + filepath.Join(t.TempDir(), "http.db")
	cfg.Store.Reset = true
	cfg.Pool.RangeStart = "127.30.0.2"
	cfg.Pool.RangeEnd = "127.30.0.10"
	cfg.Lease.LeaseTime = config.Duration{Duration: 15 * time.Second}
	cfg.Lease.T1 = config.Duration{Duration: 6 * time.Second}
	cfg.Lease.T2 = config.Duration{Duration: 12 * time.Second}
	cfg.Lease.OfferTTL = config.Duration{Duration: 5 * time.Second}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	st, err := storage.Open(cfg.Store.DSN, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	clk := server.NewFakeClock(time.Unix(1_700_000_500, 0))
	srv, err := server.New(cfg, st, clk)
	if err != nil {
		t.Fatal(err)
	}
	api := NewHTTP(srv, testLogger{t}, "http-it", true)
	ts := httptest.NewServer(api.Handler())
	t.Cleanup(ts.Close)
	return ts, srv, clk
}

type testLogger struct{ t *testing.T }

func (l testLogger) Info(msg string, args ...any)  { l.t.Logf("INFO %s %v", msg, args) }
func (l testLogger) Warn(msg string, args ...any)  { l.t.Logf("WARN %s %v", msg, args) }
func (l testLogger) Error(msg string, args ...any) { l.t.Logf("ERROR %s %v", msg, args) }

func postReplay(t *testing.T, base string, pkt []byte) map[string]any {
	t.Helper()
	body, _ := json.Marshal(map[string]string{
		"packetB64":  base64.StdEncoding.EncodeToString(pkt),
		"remoteAddr": "127.0.0.1:7777",
	})
	resp, err := http.Post(base+"/api/replay", "application/json", strings.NewReader(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestHTTP_ReplayDORAAndState(t *testing.T) {
	ts, _, _ := startHTTPEnv(t)
	mac, _ := net.ParseMAC("02:00:00:00:cc:01")
	cid := append([]byte{1}, mac...)

	disc := dhcppacket.NewRequest(9001, mac).Type(dhcppacket.MsgDiscover).
		ClientID(cid).Bytes()
	o1 := postReplay(t, ts.URL, disc)
	if o1["category"] != "ok" || o1["outType"] != "OFFER" {
		t.Fatalf("discover replay: %v", o1)
	}
	if o1["assignedIp"] != "127.30.0.2" {
		t.Fatalf("assigned: %v", o1["assignedIp"])
	}
	replyB64, _ := o1["replyB64"].(string)
	if replyB64 == "" {
		t.Fatal("missing replyB64")
	}
	reply, _ := base64.StdEncoding.DecodeString(replyB64)
	off, err := dhcppacket.Decode(reply)
	if err != nil || off.YIAddr.String() != "127.30.0.2" {
		t.Fatalf("offer decode: %v %v", off, err)
	}

	req := dhcppacket.NewRequest(9002, mac).Type(dhcppacket.MsgRequest).ClientID(cid).
		RequestedIP(netip.MustParseAddr("127.30.0.2")).ServerID(netip.MustParseAddr("127.0.0.1")).Bytes()
	o2 := postReplay(t, ts.URL, req)
	if o2["category"] != "ok" || o2["outType"] != "ACK" {
		t.Fatalf("request replay: %v", o2)
	}

	// Duplicate replay path over HTTP returns the duplicate marker.
	o3 := postReplay(t, ts.URL, req)
	if o3["duplicate"] != true || o3["result"] != "duplicate_replay" {
		t.Fatalf("duplicate replay: %v", o3)
	}

	// Lease API shows the committed row.
	resp, err := http.Get(ts.URL + "/api/leases?state=BOUND")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var leases struct {
		Leases []map[string]any `json:"leases"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&leases); err != nil {
		t.Fatal(err)
	}
	if len(leases.Leases) != 1 || leases.Leases[0]["ip"] != "127.30.0.2" {
		t.Fatalf("persisted leases: %+v", leases.Leases)
	}
	if leases.Leases[0]["state"] != "BOUND" {
		t.Fatalf("state=%v", leases.Leases[0]["state"])
	}
}

func TestHTTP_MalformedReturnsCategory(t *testing.T) {
	ts, _, _ := startHTTPEnv(t)
	body, _ := json.Marshal(map[string]string{"packetB64": base64.StdEncoding.EncodeToString([]byte("garbage"))})
	resp, err := http.Post(ts.URL+"/api/replay", "application/json", strings.NewReader(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d want 422", resp.StatusCode)
	}
	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	if out["category"] != "malformed_packet" {
		t.Fatalf("category=%v", out["category"])
	}
}

func TestHTTP_ClockAndSweepEndpoints(t *testing.T) {
	ts, srv, clk := startHTTPEnv(t)
	mac, _ := net.ParseMAC("02:00:00:00:cc:02")
	postReplay(t, ts.URL, dhcppacket.NewRequest(7001, mac).Type(dhcppacket.MsgDiscover).
		ClientID(append([]byte{1}, mac...)).Bytes())

	body, _ := json.Marshal(map[string]string{"duration": "6s"})
	resp, err := http.Post(ts.URL+"/test/clock/advance", "application/json",
		strings.NewReader(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if clk.Now().Unix() != time.Unix(1_700_000_500, 0).Add(6*time.Second).Unix() {
		t.Fatalf("clock did not advance: %s", clk.Now())
	}

	resp, err = http.Post(ts.URL+"/test/sweep", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Changes []map[string]any `json:"changes"`
	}
	json.NewDecoder(resp.Body).Decode(&out)
	if len(out.Changes) != 1 || out.Changes[0]["to"] != "EXPIRED" {
		t.Fatalf("sweep changes=%+v", out.Changes)
	}
	// Expired reservation frees the address: next offer starts at pool bottom.
	mac2, _ := net.ParseMAC("02:00:00:00:cc:03")
	o := postReplay(t, ts.URL, dhcppacket.NewRequest(7002, mac2).
		Type(dhcppacket.MsgDiscover).ClientID(append([]byte{1}, mac2...)).Bytes())
	if o["assignedIp"] != "127.30.0.2" {
		t.Fatalf("freed address not reused: %v", o["assignedIp"])
	}
	_ = srv
}
