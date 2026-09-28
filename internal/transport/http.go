package transport

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/server"
	"dhcpv4lab/internal/storage"
	"dhcpv4lab/internal/version"
)

// HTTPAPI exposes the replay and diagnostics surface.
type HTTPAPI struct {
	srv      *server.Server
	log      logger
	runID    string
	testMode bool
	mux      *http.ServeMux
	srvHTTP  *http.Server
}

type logger interface {
	Info(msg string, args ...any)
	Warn(msg string, args ...any)
	Error(msg string, args ...any)
}

// NewHTTP constructs the API mux. Listen/Close are driven by the caller.
func NewHTTP(srv *server.Server, log logger, runID string, testMode bool) *HTTPAPI {
	h := &HTTPAPI{srv: srv, log: log, runID: runID, testMode: testMode, mux: http.NewServeMux()}
	h.routes()
	return h
}

func (h *HTTPAPI) routes() {
	h.mux.HandleFunc("GET /healthz", h.health)
	h.mux.HandleFunc("GET /version", h.ver)
	h.mux.HandleFunc("POST /api/replay", h.replay)
	h.mux.HandleFunc("GET /api/leases", h.leases)
	h.mux.HandleFunc("GET /api/pool", h.pool)
	h.mux.HandleFunc("GET /api/events", h.events)
	h.mux.HandleFunc("GET /api/counters", h.counters)
	if h.testMode {
		h.mux.HandleFunc("POST /test/clock/advance", h.clockAdvance)
		h.mux.HandleFunc("POST /test/sweep", h.clockSweep)
		h.mux.HandleFunc("POST /test/reset", h.reset)
	}
}

// Handler exposes the mux (mainly tests).
func (h *HTTPAPI) Handler() http.Handler { return h.mux }

// ListenAndServe binds and serves. It blocks like http.Server.ListenAndServe.
func (h *HTTPAPI) ListenAndServe(addr string) error {
	h.srvHTTP = &http.Server{
		Addr:              addr,
		Handler:           h.mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return h.srvHTTP.ListenAndServe()
}

// Shutdown gracefully stops the HTTP server.
func (h *HTTPAPI) Shutdown(ctx context.Context) error {
	if h.srvHTTP == nil {
		return nil
	}
	return h.srvHTTP.Shutdown(ctx)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func errBody(code, reason, detail string) map[string]any {
	return map[string]any{"ok": false, "error": code, "reason": reason, "detail": detail}
}

func (h *HTTPAPI) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "runId": h.runID})
}

func (h *HTTPAPI) ver(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"version": version.Version, "commit": version.Commit, "banner": version.Banner()})
}

// ReplayRequest feeds one base64-encoded DHCP datagram through the SAME state
// machine the UDP transport uses. This is the "replay interface": captures
// from a run (or independently synthesized packets) can be re-injected.
type ReplayRequest struct {
	PacketB64  string `json:"packetB64"`
	PacketHex  string `json:"packetHex"`
	RemoteAddr string `json:"remoteAddr"`
	RunID      string `json:"runId"`
}

type replayResponse struct {
	OK         bool   `json:"ok"`
	Category   string `json:"category"`
	Action     string `json:"action"`
	Result     string `json:"result"`
	Reason     string `json:"reason"`
	Detail     string `json:"detail"`
	Duplicate  bool   `json:"duplicate"`
	AssignedIP string `json:"assignedIp,omitempty"`
	OutType    string `json:"outType,omitempty"`
	ReplyB64   string `json:"replyB64,omitempty"`
	ReplyHex   string `json:"replyHex,omitempty"`
	ReplyLen   int    `json:"replyLen"`
}

func (h *HTTPAPI) replay(w http.ResponseWriter, r *http.Request) {
	var in ReplayRequest
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeJSON(w, http.StatusBadRequest, errBody("bad_json", "decode_failed", err.Error()))
		return
	}
	var raw []byte
	switch {
	case in.PacketB64 != "":
		b, err := base64.StdEncoding.DecodeString(in.PacketB64)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, errBody("bad_base64", "packetB64_invalid", err.Error()))
			return
		}
		raw = b
	case in.PacketHex != "":
		s := in.PacketHex
		if len(s) > 1 && (s[:2] == "0x" || s[:2] == "0X") {
			s = s[2:]
		}
		b, err := hexDecode(s)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, errBody("bad_hex", "packetHex_invalid", err.Error()))
			return
		}
		raw = b
	default:
		writeJSON(w, http.StatusBadRequest, errBody("missing_packet", "no_payload",
			"provide packetB64 or packetHex"))
		return
	}
	p, derr := dhcppacket.Decode(raw)
	if derr != nil {
		writeJSON(w, http.StatusUnprocessableEntity, replayResponse{
			OK: false, Category: string(storage.CatMalformed), Action: "decode_failed",
			Result: string(storage.CatMalformed), Reason: "malformed_packet", Detail: derr.Error(),
		})
		return
	}
	runID := in.RunID
	if runID == "" {
		runID = h.runID
	}
	remote := in.RemoteAddr
	if remote == "" {
		remote = r.RemoteAddr
	}
	o := h.srv.Handle(r.Context(), p, server.Source{RemoteAddr: remote, RunID: runID})
	resp := replayResponse{
		OK:        o.Category == storage.CatOK || o.Category == storage.CatNAK || o.Category == storage.CatNoReply,
		Category:  string(o.Category),
		Action:    o.Action,
		Result:    o.Result,
		Reason:    o.Reason,
		Detail:    o.Detail,
		Duplicate: o.Duplicate,
	}
	if o.AssignedIP.IsValid() {
		resp.AssignedIP = o.AssignedIP.String()
	}
	if o.OutType != 0 {
		resp.OutType = dhcpMsgName(o.OutType)
	}
	if len(o.Reply) > 0 {
		resp.ReplyB64 = base64.StdEncoding.EncodeToString(o.Reply)
		resp.ReplyHex = hexEncode(o.Reply)
		resp.ReplyLen = len(o.Reply)
	}
	// NAKs are a valid protocol answer but HTTP stays 200 so the test client
	// reads the structured category; transport-level parse failures got 422.
	writeJSON(w, http.StatusOK, resp)
}

func (h *HTTPAPI) leases(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	rows, err := h.srv.Store().ListLeases(r.Context(), limit)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errBody("db_error", "list_failed", err.Error()))
		return
	}
	// Optional filters: identity (full key), state, ip.
	identity := r.URL.Query().Get("identity")
	state := r.URL.Query().Get("state")
	ip := r.URL.Query().Get("ip")
	labelContains := r.URL.Query().Get("labelContains")
	filtered := make([]storage.Lease, 0, len(rows))
	for _, l := range rows {
		if identity != "" && l.IdentityID != identity {
			continue
		}
		if state != "" && string(l.State) != state {
			continue
		}
		if ip != "" && l.IP != ip {
			continue
		}
		if labelContains != "" && !containsStr(l.IdentityLabel, labelContains) {
			continue
		}
		filtered = append(filtered, l)
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "runId": h.runID, "leases": filtered})
}

func containsStr(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func (h *HTTPAPI) pool(w http.ResponseWriter, r *http.Request) {
	offered, bound, err := h.srv.Store().ActiveAddressCount(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errBody("db_error", "count_failed", err.Error()))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "runId": h.runID,
		"offered": offered, "bound": bound,
		"now": h.srv.Clock().Now().Format(time.RFC3339Nano),
	})
}

func (h *HTTPAPI) events(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	runID := r.URL.Query().Get("runId")
	if runID == "" {
		runID = h.runID
	}
	if r.URL.Query().Get("all") == "1" {
		runID = ""
	}
	asc := r.URL.Query().Get("order") != "desc"
	evs, err := h.srv.Store().RecentEvents(r.Context(), runID, limit, asc)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errBody("db_error", "events_failed", err.Error()))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "runId": h.runID, "events": evs})
}

func (h *HTTPAPI) counters(w http.ResponseWriter, _ *http.Request) {
	c := h.srv.Counters()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "counters": c, "now": h.srv.Clock().Now().Format(time.RFC3339Nano)})
}

func (h *HTTPAPI) clockAdvance(w http.ResponseWriter, r *http.Request) {
	if !h.testMode {
		writeJSON(w, http.StatusForbidden, errBody("disabled", "test_mode_off", "enable testMode in config"))
		return
	}
	fc, ok := h.srv.Clock().(*server.FakeClock)
	if !ok {
		writeJSON(w, http.StatusConflict, errBody("not_fake_clock", "wall_clock",
			"server is running with the real clock"))
		return
	}
	var in struct {
		Duration string `json:"duration"`
	}
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeJSON(w, http.StatusBadRequest, errBody("bad_json", "decode_failed", err.Error()))
		return
	}
	d, err := time.ParseDuration(in.Duration)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errBody("bad_duration", "duration_invalid", err.Error()))
		return
	}
	now := fc.Advance(d)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "now": now.Format(time.RFC3339Nano)})
}

func (h *HTTPAPI) clockSweep(w http.ResponseWriter, r *http.Request) {
	if !h.testMode {
		writeJSON(w, http.StatusForbidden, errBody("disabled", "test_mode_off", "enable testMode in config"))
		return
	}
	changes, err := h.srv.Sweep(r.Context(), h.runID)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errBody("sweep_failed", "sweep_error", err.Error()))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "changes": changes,
		"now": h.srv.Clock().Now().Format(time.RFC3339Nano)})
}

func (h *HTTPAPI) reset(w http.ResponseWriter, r *http.Request) {
	if !h.testMode {
		writeJSON(w, http.StatusForbidden, errBody("disabled", "test_mode_off", "enable testMode in config"))
		return
	}
	if err := h.srv.Store().ResetState(r.Context()); err != nil {
		writeJSON(w, http.StatusInternalServerError, errBody("reset_failed", "reset_error", err.Error()))
		return
	}
	if fc, ok := h.srv.Clock().(*server.FakeClock); ok {
		fc.Set(testClockSeed)
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true,
		"now": h.srv.Clock().Now().Format(time.RFC3339Nano)})
}

// testClockSeed matches the daemon's fake-clock seed (cmd/dhcpd), so a reset
// returns the virtual clock to a known origin.
var testClockSeed = time.Unix(server.FakeClockSeedUnix, 0)

func dhcpMsgName(t byte) string {
	switch t {
	case dhcppacket.MsgDiscover:
		return "DISCOVER"
	case dhcppacket.MsgOffer:
		return "OFFER"
	case dhcppacket.MsgRequest:
		return "REQUEST"
	case dhcppacket.MsgAck:
		return "ACK"
	case dhcppacket.MsgNak:
		return "NAK"
	case dhcppacket.MsgRelease:
		return "RELEASE"
	default:
		return "TYPE(" + strconv.Itoa(int(t)) + ")"
	}
}
