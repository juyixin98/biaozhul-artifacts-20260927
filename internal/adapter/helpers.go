package adapter

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"

	"netpolicy/internal/diag"
	"netpolicy/internal/engine"
)

func decode(w http.ResponseWriter, r *http.Request, log *slog.Logger, dst any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		log.Debug("malformed request body",
			"requestId", diag.RequestIDFromContext(r.Context()), "error", err.Error())
		writeError(w, r, http.StatusBadRequest, "malformed_json", "request body must be valid JSON matching the schema: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(body)
}

func writeError(w http.ResponseWriter, r *http.Request, status int, code, msg string) {
	writeJSON(w, status, errorBody{
		Error:     apiError{Code: code, Message: msg},
		RequestID: diag.RequestIDFromContext(r.Context()),
	})
}

func parsePathInt(s string, dst *int64) (int64, error) {
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, err
	}
	*dst = v
	return v, nil
}

// logDecision records the verdict with its request id and the key state that
// produced it. Only safe, non-payload fields are logged: endpoint ids, port
// descriptors, policy names and per-side isolation flags. Endpoint label
// values never enter this line, so sensitive labels cannot leak via logs.
func logDecision(r *http.Request, log *slog.Logger, src, dst string, port int, d *engine.Decision) {
	attrs := []any{
		"requestId", diag.RequestIDFromContext(r.Context()),
		"revision", d.Revision,
		"srcUid", src,
		"dstUid", dst,
		"port", port,
		"protocol", d.Protocol,
		"verdict", d.Verdict,
		"reason", d.Reason,
		"ingressIsolated", d.Ingress.Isolated,
		"egressIsolated", d.Egress.Isolated,
		"ingressSelected", d.Ingress.SelectedPolicies,
		"egressSelected", d.Egress.SelectedPolicies,
		"ingressMatches", matchRefs(d.Ingress.Matches),
		"egressMatches", matchRefs(d.Egress.Matches),
	}
	if len(d.Ingress.Hints) > 0 || len(d.Egress.Hints) > 0 {
		attrs = append(attrs,
			"ingressHints", d.Ingress.Hints,
			"egressHints", d.Egress.Hints)
	}
	if len(d.Ingress.Ambiguities) > 0 || len(d.Egress.Ambiguities) > 0 {
		attrs = append(attrs,
			"ingressAmbiguities", d.Ingress.Ambiguities,
			"egressAmbiguities", d.Egress.Ambiguities)
	}
	switch d.Verdict {
	case engine.VerdictUndecidable:
		log.WarnContext(r.Context(), "reachability decision undecidable", attrs...)
	case engine.VerdictDeny:
		log.InfoContext(r.Context(), "reachability decision deny", attrs...)
	default:
		log.InfoContext(r.Context(), "reachability decision allow", attrs...)
	}
}

func matchRefs(ms []engine.Match) []map[string]any {
	if len(ms) == 0 {
		return nil
	}
	out := make([]map[string]any, 0, len(ms))
	for _, m := range ms {
		out = append(out, map[string]any{
			"policy":    m.PolicyNamespace + "/" + m.PolicyName,
			"direction": string(m.Direction),
			"ruleIndex": m.RuleIndex,
		})
	}
	return out
}
