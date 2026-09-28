// Command netsemd is the runnable HTTP service for offline first-match
// network-rule analysis.
//
// Endpoints (all local, JSON only):
//
//	POST /configs            submit a ruleset JSON; stores version + report
//	GET  /reports/latest     latest analysis report (diagnostics, witnesses)
//	GET  /reports/{version}  report for a version
//	POST /evaluate           evaluate one packet against the latest config
//	GET  /requests/{id}      correlated, explainable log of a request
//	GET  /healthz
package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"netsem/internal/analyzer"
	"netsem/internal/config"
	"netsem/internal/replay"
	"netsem/internal/store"
)

type server struct {
	st             *store.Store
	instance       string
	sourceLocation string

	mu        sync.RWMutex
	version   int64
	evaluator *replay.Evaluator
}

// New constructs the application: opens the store, loads the newest config
// version (if any), and returns a Handler plus a cleanup function.
func New(dbPath, instance string) (*App, func() error, error) {
	ctx := context.Background()
	st, err := store.Open(ctx, dbPath, instance)
	if err != nil {
		return nil, nil, err
	}
	a := &App{
		st:             st,
		instance:       instance,
		sourceLocation: "netsemd@" + instance,
	}
	if err := a.loadLatest(ctx); err != nil {
		st.Close()
		return nil, nil, err
	}
	return a, st.Close, nil
}

// App holds service state.
type App = server

// Handler returns the wired HTTP handler.
func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /configs", a.postConfig)
	mux.HandleFunc("GET /reports/latest", a.getLatestReport)
	mux.HandleFunc("GET /reports/{version}", a.getReport)
	mux.HandleFunc("POST /evaluate", a.postEvaluate)
	mux.HandleFunc("GET /requests/{id}", a.getRequest)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "instance": a.instance})
	})
	return logRequests(mux)
}

// Instance returns the instance label recorded in logs.
func (a *App) Instance() string { return a.instance }

func (s *server) loadLatest(ctx context.Context) error {
	v, err := s.st.LatestVersion(ctx)
	if err != nil {
		return err
	}
	if v == 0 {
		return nil
	}
	payload, err := s.st.Report(ctx, v)
	if err != nil {
		return err
	}
	var rep analyzer.Report
	if err := json.Unmarshal(payload, &rep); err != nil {
		return err
	}
	// Rebuild evaluator from stored regions is not possible from report JSON;
	// recompile requires the raw config. Persisted raw config is re-parsed.
	raw, err := s.rawConfig(ctx, v)
	if err != nil {
		return err
	}
	ev, err := buildEvaluator(v, raw)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.version = v
	s.evaluator = ev
	s.mu.Unlock()
	return nil
}

func (s *server) rawConfig(ctx context.Context, v int64) (string, error) {
	return s.st.RawConfig(ctx, v)
}

type postConfigResponse struct {
	Version        int64                `json:"version"`
	ParseOK        bool                 `json:"parse_ok"`
	ParseErrors    []*config.ParseError `json:"parse_errors"`
	Notes          []ruleNote           `json:"notes,omitempty"`
	Report         *analyzer.Report     `json:"report,omitempty"`
	Instance       string               `json:"instance"`
	SourceLocation string               `json:"source_location"`
}

type ruleNote struct {
	RuleID string `json:"rule_id"`
	Note   string `json:"note"`
}

func (s *server) postConfig(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	body, err := readLimited(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	rs, perrs := config.Parse(strings.NewReader(string(body)))
	parseOK := len(perrs) == 0
	errJSON, _ := json.Marshal(perrs)
	v, err := s.st.SaveConfig(ctx, string(body), parseOK, errJSON)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "store_error", err.Error())
		return
	}
	resp := postConfigResponse{
		Version: v, ParseOK: parseOK, ParseErrors: perrs,
		Instance: s.instance, SourceLocation: s.sourceLocation,
	}
	if rs != nil {
		for _, ru := range rs.Rules {
			for _, n := range ru.Notes {
				resp.Notes = append(resp.Notes, ruleNote{RuleID: ru.ID, Note: n})
			}
		}
	}
	if parseOK {
		rep, err := analyzer.Analyze(rs)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "analysis_error", err.Error())
			return
		}
		payload, _ := json.Marshal(rep)
		if err := s.st.SaveReport(ctx, v, payload); err != nil {
			writeError(w, http.StatusInternalServerError, "store_error", err.Error())
			return
		}
		resp.Report = rep
		ev, err := buildEvaluator(v, string(body))
		if err != nil {
			writeError(w, http.StatusInternalServerError, "analysis_error", err.Error())
			return
		}
		s.mu.Lock()
		s.version, s.evaluator = v, ev
		s.mu.Unlock()
	}
	writeJSON(w, http.StatusOK, resp)
}

func buildEvaluator(version int64, raw string) (*replay.Evaluator, error) {
	rs, perrs := config.Parse(strings.NewReader(raw))
	if len(perrs) > 0 {
		return nil, fmt.Errorf("config version %d has parse errors", version)
	}
	regions, err := analyzer.Compile(rs)
	if err != nil {
		return nil, err
	}
	notes := map[string][]string{}
	for _, ru := range rs.Rules {
		if len(ru.Notes) > 0 {
			notes[ru.ID] = ru.Notes
		}
	}
	return replay.NewEvaluator(version, rs.DefaultAction, regions, notes), nil
}

func (s *server) getLatestReport(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	v := s.version
	s.mu.RUnlock()
	if v == 0 {
		writeError(w, http.StatusNotFound, "no_config", "no configuration has been submitted yet")
		return
	}
	http.Redirect(w, r, fmt.Sprintf("/reports/%d", v), http.StatusSeeOther)
}

func (s *server) getReport(w http.ResponseWriter, r *http.Request) {
	v, err := parseVersion(r.PathValue("version"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_version", err.Error())
		return
	}
	payload, err := s.st.Report(r.Context(), v)
	if err != nil {
		writeError(w, http.StatusNotFound, "not_found", fmt.Sprintf("no report for version %d (config had parse errors or does not exist)", v))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(payload)
}

func (s *server) postEvaluate(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	ev := s.evaluator
	version := s.version
	s.mu.RUnlock()
	if ev == nil {
		writeError(w, http.StatusConflict, "no_config", "no valid configuration available; POST /configs first")
		return
	}
	body, err := readLimited(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	var req replay.PacketRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "body must be JSON: "+err.Error())
		return
	}
	d := ev.Evaluate(req)
	d.Instance = s.instance
	d.SourceLocation = s.sourceLocation
	d.Version = version

	packetJSON, _ := json.Marshal(req)
	steps := make([]store.Step, len(d.Steps))
	for i, st := range d.Steps {
		steps[i] = store.Step{Order: st.Order, RuleID: st.RuleID, Matched: st.Matched, Action: st.Action, Explanation: st.Explanation}
	}
	logErr := s.st.LogRequest(r.Context(), store.LogEntry{
		RequestID: d.RequestID, Version: version, Family: d.Family,
		PacketJSON: packetJSON, Decision: d.Decision, MatchedRuleID: d.MatchedRuleID,
		Steps: steps, Certain: d.Certain, Uncertainties: d.Uncertainties, Errors: d.Errors,
		SourceLocation: s.sourceLocation,
	})
	if logErr != nil {
		// Logging failure must not silently produce an untraceable answer.
		d.Uncertainties = append(d.Uncertainties, "request could not be persisted: "+logErr.Error())
		d.Certain = false
	}
	status := http.StatusOK
	if len(d.Errors) > 0 {
		status = http.StatusUnprocessableEntity
	}
	writeJSON(w, status, d)
}

func (s *server) getRequest(w http.ResponseWriter, r *http.Request) {
	rl, err := s.st.GetRequest(r.Context(), r.PathValue("id"))
	if err != nil {
		writeError(w, http.StatusNotFound, "not_found", "no request with that id")
		return
	}
	writeJSON(w, http.StatusOK, rl)
}

// --- small HTTP helpers ---

func readLimited(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	return readAllLimit(r.Body, 1<<20)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

type errBody struct {
	ErrorCategory string `json:"error_category"`
	Message       string `json:"message"`
	Instance      string `json:"instance,omitempty"`
}

func writeError(w http.ResponseWriter, code int, category, msg string) {
	writeJSON(w, code, errBody{ErrorCategory: category, Message: msg})
}

func parseVersion(tok string) (int64, error) {
	var v int64
	if _, err := fmt.Sscanf(tok, "%d", &v); err != nil || v <= 0 {
		return 0, fmt.Errorf("version must be a positive integer, got %q", tok)
	}
	return v, nil
}

func hostnameOr(fallback string) string {
	if h, err := os.Hostname(); err == nil && h != "" {
		return h
	}
	return fallback
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start))
	})
}
