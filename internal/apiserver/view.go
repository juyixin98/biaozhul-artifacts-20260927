package apiserver

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"crcontroller/internal/logx"
	"crcontroller/internal/model"
)

func decodeBody(r *http.Request, v any) error {
	return json.NewDecoder(r.Body).Decode(v)
}

const requestIDHeader = "X-Request-ID"

// resourceView is the wire representation.
type resourceView struct {
	UID               string            `json:"uid"`
	Name              string            `json:"name"`
	Namespace         string            `json:"namespace"`
	Generation        int64             `json:"generation"`
	ResourceVersion   int64             `json:"resourceVersion"`
	Spec              map[string]any    `json:"spec"`
	SpecHash          string            `json:"specHash"`
	Finalizers        []string          `json:"finalizers"`
	DeletionTimestamp *time.Time        `json:"deletionTimestamp,omitempty"`
	Status            model.Status      `json:"status"`
	Annotations       map[string]string `json:"annotations,omitempty"`
	CreatedAt         time.Time         `json:"createdAt"`
	UpdatedAt         time.Time         `json:"updatedAt"`
}

func (s *Server) view(o *model.Object, priv bool) resourceView {
	spec := o.Spec
	if !priv {
		spec = logx.RedactSpec(o.Spec)
	}
	fin := o.Finalizers
	if fin == nil {
		fin = []string{}
	}
	conds := o.Status.Conditions
	if conds == nil {
		conds = []model.Condition{}
	}
	st := o.Status
	st.Conditions = conds
	return resourceView{
		UID: o.UID, Name: o.Name, Namespace: o.Namespace,
		Generation: o.Generation, ResourceVersion: o.ResourceVer,
		Spec: spec, SpecHash: o.SpecHash, Finalizers: fin,
		DeletionTimestamp: o.DeletionTS, Status: st,
		Annotations: o.Annotations, CreatedAt: o.CreatedAt, UpdatedAt: o.UpdatedAt,
	}
}

func privileged(r *http.Request, secret string) bool {
	if secret == "" {
		// No secret configured: the deployment is local-only; allow (tests
		// still exercise the redaction path by configuring a secret).
		return true
	}
	const hdr = "X-Controller-Auth"
	return r.Header.Get(hdr) == secret
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, status int, rid, msg string) {
	writeJSON(w, status, map[string]string{
		"error": http.StatusText(status), "requestID": rid, "detail": msg,
	})
}

func expectedVersion(r *http.Request, fallback int64) int64 {
	// Prefer the explicit header; a query parameter is also accepted for
	// curl-friendly demos.
	if v := r.Header.Get("If-Match"); v != "" {
		v = strings.Trim(v, `"`)
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	if v := r.URL.Query().Get("resourceVersion"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return fallback
}

func queryInt(r *http.Request, key string, def int) int {
	v := r.URL.Query().Get(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def
	}
	return n
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func without(xs []string, v string) []string {
	out := xs[:0]
	for _, x := range xs {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}

func newUID(ns, name string) string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "u-" + ns + "-" + name + "-" + hex.EncodeToString(b[:])
}

// requestIDMiddleware guarantees every request has a correlation ID echoed
// back via the response header.
func requestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rid := r.Header.Get(requestIDHeader)
		if rid == "" {
			rid = newReqID()
		}
		w.Header().Set(requestIDHeader, rid)
		next.ServeHTTP(w, r)
	})
}

func requestID(r *http.Request) string {
	if v := r.Header.Get(requestIDHeader); v != "" {
		return v
	}
	return "req-unknown"
}

func errorBody(msg string) map[string]string { return map[string]string{"error": msg} }

func newReqID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "req-" + hex.EncodeToString(b[:])
}
