// Package httpapi is the net/http transport adapter. It exposes the
// coordinator over JSON using only the standard library; every response
// carries a request id and the budget snapshot that drove the decision.
package httpapi

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
)

// Server bundles dependencies for routing.
type Server struct {
	Handler http.Handler
	log     *slog.Logger
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func errorBody(w http.ResponseWriter, status int, reqID, category, msg string) {
	writeJSON(w, status, map[string]any{
		"request_id": reqID,
		"accepted":   false,
		"category":   category,
		"error":      msg,
	})
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

// splitPath returns the trimmed non-empty path segments.
func splitPath(p string) []string {
	parts := strings.Split(strings.Trim(p, "/"), "/")
	out := parts[:0]
	for _, x := range parts {
		if x != "" {
			out = append(out, x)
		}
	}
	return out
}
