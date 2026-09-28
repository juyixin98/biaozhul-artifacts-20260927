package replay_test

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"dhcp4lab/internal/replay"
	"dhcp4lab/testfixture/testhelp"
	"dhcp4lab/testfixture/wirekit"
)

func postInject(t *testing.T, h http.Handler, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inject", strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var m map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &m)
	}
	return rec.Code, m
}

func TestInjectDiscoverThenRequest(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	api := replay.New(s.Server, "run-http-1", nil)
	h := api.Handler()

	xid := [4]byte{0x77, 0x88, 0x99, 0xaa}
	mac := wirekit.MAC("02:00:00:33:33:33")
	disc := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTDiscover).Build()

	code, m := postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(disc)+`"}`)
	if code != http.StatusOK {
		t.Fatalf("discover http %d: %v", code, m)
	}
	if m["action"] != "offer" || m["reply_type"] != "OFFER" {
		t.Fatalf("discover resp = %v", m)
	}
	if m["run_id"] != "run-http-1" {
		t.Fatalf("run id not correlated: %v", m["run_id"])
	}
	offerHex, _ := m["reply_hex"].(string)
	if offerHex == "" {
		t.Fatal("missing reply hex")
	}
	// Validate the returned bytes with the INDEPENDENT parser.
	rb, _ := hex.DecodeString(offerHex)
	ref, err := wirekit.Parse(rb)
	if err != nil {
		t.Fatalf("reference parser rejects offer hex: %v", err)
	}
	if mt, _ := ref.MsgType(); mt != wirekit.MTOffer {
		t.Fatalf("offer hex type = %d", mt)
	}

	// SELECTING REQUEST for the offered IP.
	reqB := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
		RequestedIP(ref.YIAddr).ServerID(wirekit.IP("192.0.2.1")).Build()
	code, m = postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(reqB)+`"}`)
	if code != http.StatusOK {
		t.Fatalf("request http %d", code)
	}
	if m["action"] != "ack" || m["reply_type"] != "ACK" {
		t.Fatalf("request resp = %v", m)
	}
	if m["lease_state"] != "leased" {
		t.Fatalf("lease_state = %v", m["lease_state"])
	}
	if m["lease_ip"] != wirekit.IPStr(ref.YIAddr) {
		t.Fatalf("lease_ip = %v", m["lease_ip"])
	}
	if exp, _ := m["lease_expires"].(string); exp == "" {
		t.Fatal("missing lease_expires")
	}
}

func TestInjectReplayDoesNotExtend(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	api := replay.New(s.Server, "run-http-2", nil)
	h := api.Handler()

	xid := [4]byte{0x01, 0x02, 0x03, 0x04}
	mac := wirekit.MAC("02:00:00:44:44:44")
	disc := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTDiscover).Build()
	_, m1 := postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(disc)+`"}`)
	offerHex, _ := m1["reply_hex"].(string)
	rb, _ := hex.DecodeString(offerHex)
	off, _ := wirekit.Parse(rb)

	reqB := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
		RequestedIP(off.YIAddr).ServerID(wirekit.IP("192.0.2.1")).Build()
	_, m2 := postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(reqB)+`"}`)
	expiry1, _ := m2["lease_expires"].(string)

	s.Clock.Advance(50 * time.Second)
	// Replay identical REQUEST datagram.
	_, m3 := postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(reqB)+`"}`)
	expiry2, _ := m3["lease_expires"].(string)
	if m3["duplicate"] != true {
		t.Fatalf("replay duplicate flag = %v", m3["duplicate"])
	}
	if expiry1 != expiry2 {
		t.Fatalf("replay moved expiry %s -> %s", expiry1, expiry2)
	}
}

func TestInjectMalformedIs422WithCategory(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	api := replay.New(s.Server, "run-http-3", nil)
	h := api.Handler()

	code, m := postInject(t, h, `{"datagram_hex":"000102"}`)
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("http = %d, want 422", code)
	}
	if m["fail_category"] != "message_too_short" {
		t.Fatalf("category = %v", m["fail_category"])
	}

	// Bad JSON / bad hex are 400 with their own categories.
	code, m = postInject(t, h, `{"datagram_hex":"zzzz"}`)
	if code != http.StatusBadRequest || m["fail_category"] != "bad_hex" {
		t.Fatalf("bad hex: %d %v", code, m)
	}
	code, _ = postInject(t, h, `{not json`)
	if code != http.StatusBadRequest {
		t.Fatalf("bad json http = %d", code)
	}
}

func TestForeignServerSilenceAndNAK(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	api := replay.New(s.Server, "run-http-4", nil)
	h := api.Handler()

	xid := [4]byte{0x55, 0x66, 0x77, 0x88}
	mac := wirekit.MAC("02:00:00:55:55:55")
	// REQUEST selecting a foreign server without an offer: drop.
	req := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTRequest).
		RequestedIP(wirekit.IP("192.0.2.10")).
		ServerID(wirekit.IP("198.51.100.9")).Build()
	code, m := postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(req)+`"}`)
	if code != http.StatusOK {
		t.Fatalf("http %d", code)
	}
	if m["action"] != "drop" || m["reason"] != "server_id_is_not_this_server" {
		t.Fatalf("foreign server = %v", m)
	}
	if _, has := m["reply_hex"]; has {
		t.Fatal("silenced request must not include reply_hex")
	}
	if rb, _ := m["reply_bytes"].(float64); rb != 0 {
		t.Fatalf("reply_bytes = %v, want 0", rb)
	}
}

func TestLeasesAndJournalEndpoints(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	api := replay.New(s.Server, "run-http-5", nil)
	h := api.Handler()

	xid := [4]byte{0x90, 0x90, 0x90, 0x90}
	mac := wirekit.MAC("02:00:00:66:66:66")
	disc := wirekit.NewRequest(xid, mac).MsgType(wirekit.MTDiscover).Build()
	postInject(t, h, `{"datagram_hex":"`+hex.EncodeToString(disc)+`"}`)

	get := func(path string) (int, map[string]any) {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		var m map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &m)
		return rec.Code, m
	}
	code, m := get("/api/v1/leases")
	if code != 200 {
		t.Fatalf("leases http %d", code)
	}
	// DISCOVER alone creates no lease row.
	if n := len(m["leases"].([]any)); n != 0 {
		t.Fatalf("leases after discover only = %d, want 0", n)
	}

	code, m = get("/api/v1/journal")
	if code != 200 {
		t.Fatalf("journal http %d", code)
	}
	j := m["journal"].([]any)
	if len(j) == 0 {
		t.Fatal("empty journal")
	}
	first := j[0].(map[string]any)
	if first["action"] != "offer" || first["recv_type"] != "DISCOVER" {
		t.Fatalf("journal row = %v", first)
	}
	if first["xid"] != hex.EncodeToString(xid[:]) {
		t.Fatalf("journal xid = %v", first["xid"])
	}

	code, m = get("/api/v1/version")
	if code != 200 || m["server_version"] == "" {
		t.Fatalf("version = %v", m)
	}
}

// Guard against accidental method allowance.
func TestMethodNotAllowed(t *testing.T) {
	s := testhelp.NewStore(t, testhelp.Options{})
	h := replay.New(s.Server, "run-http-6", nil).Handler()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/inject", bytes.NewReader(nil))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET inject = %d, want 405", rec.Code)
	}
}
