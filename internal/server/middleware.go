package server

import (
	"context"
	"net/http"

	"igmpv2timer/internal/model"
)

type reqIDKey struct{}

func (s *Server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Honor a client correlation id if supplied, else mint one.
		id := r.Header.Get("X-Request-ID")
		if id == "" {
			id = s.nextRequestID("http")
		} else {
			id = s.nextRequestID(id)
		}
		w.Header().Set("X-Request-ID", id)
		ctx := context.WithValue(r.Context(), reqIDKey{}, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// recoverer turns panics into 500s carrying the request id but never the
// raw payload (which may contain fixture addresses).
func (s *Server) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqID, _ := r.Context().Value(reqIDKey{}).(string)
		defer func() {
			if rec := recover(); rec != nil {
				s.log.Printf("req=%s PANIC recovered; payload not logged; src would be %s",
					reqID, model.MaskAddr(r.Header.Get("X-Synthetic-Src")))
				writeJSON(w, http.StatusInternalServerError, errorBody{
					RequestID: reqID,
					Error:     "internal error while processing event",
					Category:  "internal_panic",
				})
			}
		}()
		next.ServeHTTP(w, r)
	})
}
