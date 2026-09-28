// Package replay exposes the state machine over HTTP for deterministic
// replay and diagnostics. The /inject endpoint accepts an exact on-wire
// datagram (hex), runs it through the same adapter the UDP transport
// uses, and returns the exact action and reply bytes. Malformed input is
// reported with the parse-error category and HTTP 422 — it is never
// folded into a success response.
package replay

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"time"

	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/server"
	"dhcp4lab/internal/storage"
	"dhcp4lab/internal/version"
)

// API is the HTTP handler bundle.
type API struct {
	srv   *server.Server
	log   *slog.Logger
	runID string
	mux   *http.ServeMux
}

// New builds the API rooted at /api/v1/.
func New(srv *server.Server, runID string, log *slog.Logger) *API {
	if log == nil {
		log = slog.Default()
	}
	a := &API{srv: srv, log: log, runID: runID}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/healthz", a.handleHealth)
	mux.HandleFunc("/api/v1/version", a.handleVersion)
	mux.HandleFunc("/api/v1/inject", a.handleInject)
	mux.HandleFunc("/api/v1/leases", a.handleLeases)
	mux.HandleFunc("/api/v1/journal", a.handleJournal)
	mux.HandleFunc("/api/v1/events", a.handleEvents)
	a.mux = mux
	return a
}

// Handler is the rooted http.Handler.
func (a *API) Handler() http.Handler {
	return a.mux
}

func (a *API) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", r.Method)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":   "ok",
		"run_id":   a.runID,
		"server":   version.Server,
		"time_utc": time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (a *API) handleVersion(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"server_version": version.Server,
		"protocol":       version.Protocol,
		"run_id":         a.runID,
		"subset":         []string{"DISCOVER", "OFFER", "REQUEST", "ACK", "NAK", "RELEASE"},
	})
}

// InjectRequest is a raw datagram submission.
type InjectRequest struct {
	// DatagramHex is the full BOOTP message in hexadecimal (spaces
	// allowed). Exactly one encoding must be supplied.
	DatagramHex string `json:"datagram_hex"`
}

// InjectResponse is the exact outcome of running one datagram.
type InjectResponse struct {
	RunID        string `json:"run_id"`
	RecvType     string `json:"recv_type"`
	XID          string `json:"xid"`
	Action       string `json:"action"`
	ReplyType    string `json:"reply_type,omitempty"`
	ReplyHex     string `json:"reply_hex,omitempty"`
	ReplyBytes   int    `json:"reply_bytes"`
	LeaseIP      string `json:"lease_ip,omitempty"`
	LeaseState   string `json:"lease_state,omitempty"`
	LeaseExpires string `json:"lease_expires,omitempty"`
	Duplicate    bool   `json:"duplicate"`
	Reason       string `json:"reason,omitempty"`
}

func (a *API) handleInject(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", r.Method)
		return
	}
	var req InjectRequest
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	raw, err := decodeHexLoose(req.DatagramHex)
	if err != nil || len(raw) == 0 {
		writeError(w, http.StatusBadRequest, "bad_hex", "datagram_hex must be even-length hex of a BOOTP message")
		return
	}

	// Parse for the response metadata; the adapter parses defensively
	// too. A parse failure is an explicit 422 with the category.
	pkt, perr := dhcp4.Unmarshal(raw)
	if perr != nil {
		var pe *dhcp4.ParseError
		cat, detail := "parse_error", perr.Error()
		if errors.As(perr, &pe) {
			cat, detail = pe.Code, pe.Reason
		}
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
			"run_id":        a.runID,
			"fail_category": cat,
			"detail":        detail,
		})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	decision, err := a.srv.Handle(ctx, pkt)
	if err != nil {
		a.log.LogAttrs(r.Context(), slog.LevelError, "inject handling failed",
			slog.String("component", "replay"), slog.Any("err", err))
		writeError(w, http.StatusInternalServerError, "state_machine_error", err.Error())
		return
	}

	mt, _ := pkt.Type()
	resp := InjectResponse{
		RunID:     a.runID,
		RecvType:  mt.String(),
		XID:       hex.EncodeToString(pkt.XID[:]),
		Action:    string(decision.Outcome.Action),
		Reason:    decision.Outcome.Reason,
		Duplicate: decision.Outcome.Duplicate,
	}
	if decision.Outcome.Reply != nil {
		switch decision.Outcome.Reply.Type {
		case dhcp4.MsgOffer:
			resp.ReplyType = "OFFER"
		case dhcp4.MsgACK:
			resp.ReplyType = "ACK"
		case dhcp4.MsgNAK:
			resp.ReplyType = "NAK"
		}
	}
	if len(decision.ReplyBytes) > 0 {
		resp.ReplyHex = hex.EncodeToString(decision.ReplyBytes)
		resp.ReplyBytes = len(decision.ReplyBytes)
	}
	if decision.Outcome.LeaseIP.IsValid() {
		resp.LeaseIP = decision.Outcome.LeaseIP.String()
	}
	if decision.Outcome.LeaseState != "" {
		resp.LeaseState = string(decision.Outcome.LeaseState)
	}
	if !decision.Outcome.LeaseExpires.IsZero() {
		resp.LeaseExpires = decision.Outcome.LeaseExpires.UTC().Format(time.RFC3339Nano)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (a *API) handleLeases(w http.ResponseWriter, r *http.Request) {
	rows, err := a.srv.Store().ListLeases(r.Context(), 200)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "query_error", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, l := range rows {
		out = append(out, leaseMap(l))
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": a.runID, "leases": out})
}

func (a *API) handleJournal(w http.ResponseWriter, r *http.Request) {
	rows, err := a.srv.Store().RecentJournal(r.Context(), 200)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "query_error", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, j := range rows {
		m := map[string]any{
			"id": j.ID, "ts": j.CreatedAt.UTC().Format(time.RFC3339Nano),
			"client_key": j.ClientKey, "xid": hex.EncodeToString(j.XID),
			"recv_type": j.RecvType, "action": j.Action,
			"reply_type": j.ReplyType, "reason": j.Reason,
		}
		if j.OfferedIP != "" {
			m["offered_ip"] = j.OfferedIP
		}
		if !j.LeaseExpires.IsZero() {
			m["lease_expires"] = j.LeaseExpires.UTC().Format(time.RFC3339Nano)
		}
		out = append(out, m)
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": a.runID, "journal": out})
}

func (a *API) handleEvents(w http.ResponseWriter, r *http.Request) {
	rows, err := a.srv.Store().RecentEvents(r.Context(), 200)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "query_error", err.Error())
		return
	}
	out := make([]map[string]any, 0, len(rows))
	for _, e := range rows {
		out = append(out, map[string]any{
			"id": e.ID, "ts": e.TS.UTC().Format(time.RFC3339Nano),
			"client_key": e.ClientKey, "xid": hex.EncodeToString(e.XID),
			"kind": e.Kind, "detail": e.Detail,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": a.runID, "events": out})
}

func leaseMap(l storage.LeaseView) map[string]any {
	m := map[string]any{
		"ip": l.IP.String(), "state": string(l.State), "client_key": l.ClientKey,
		"created_at": l.CreatedAt.UTC().Format(time.RFC3339Nano),
		"updated_at": l.UpdatedAt.UTC().Format(time.RFC3339Nano),
	}
	if !l.ExpiresAt.IsZero() {
		m["expires_at"] = l.ExpiresAt.UTC().Format(time.RFC3339Nano)
	}
	return m
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, code int, category, detail string) {
	writeJSON(w, code, map[string]any{"fail_category": category, "detail": detail})
}

// decodeHexLoose accepts hex with whitespace and colons.
func decodeHexLoose(s string) ([]byte, error) {
	out := make([]byte, 0, len(s)/2)
	var hi byte = 255
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == ':':
			continue
		case c >= '0' && c <= '9':
			c -= '0'
		case c >= 'a' && c <= 'f':
			c -= 'a' - 10
		case c >= 'A' && c <= 'F':
			c -= 'A' - 10
		default:
			return nil, errors.New("invalid hex digit")
		}
		if hi == 255 {
			hi = c
		} else {
			out = append(out, hi<<4|c)
			hi = 255
		}
	}
	if hi != 255 {
		return nil, errors.New("odd hex length")
	}
	return out, nil
}

// ListenAndServe starts the admin HTTP server on a loopback address.
func ListenAndServe(ctx context.Context, addr string, h http.Handler, log *slog.Logger) (*http.Server, net.Listener, error) {
	ln, err := net.Listen("tcp4", addr)
	if err != nil {
		return nil, nil, err
	}
	if !ln.Addr().(*net.TCPAddr).IP.IsLoopback() {
		_ = ln.Close()
		return nil, nil, errors.New("replay: refusing non-loopback admin bind")
	}
	srv := &http.Server{Addr: ln.Addr().String(), Handler: h}
	go func() {
		_ = srv.Serve(ln)
	}()
	return srv, ln, nil
}
