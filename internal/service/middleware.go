package service

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// statusWriter captures the response status for access logging.
type statusWriter struct {
	http.ResponseWriter
	status int
	wrote  bool
}

func (s *statusWriter) WriteHeader(code int) {
	if !s.wrote {
		s.status = code
		s.wrote = true
	}
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusWriter) Write(b []byte) (int, error) {
	s.wrote = true
	return s.ResponseWriter.Write(b)
}

// decodeJSONLimited parses the body into v, enforcing a maximum size and
// rejecting trailing content or multiple JSON values.
func decodeJSONLimited(w http.ResponseWriter, r *http.Request, v any, maxBytes int64) error {
	if r.Body == nil {
		return errors.New("empty request body")
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(v); err != nil {
		if errors.Is(err, io.EOF) {
			return errors.New("empty request body")
		}
		return err
	}
	// Reject extra data after the first JSON value.
	var extra json.RawMessage
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("unexpected trailing content after JSON object")
		}
		return err
	}
	return nil
}

// recoverer turns a panic in a handler into a 500 with the correlation id,
// instead of dropping the connection without explanation.
func (s *Service) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				rc := identity(r)
				s.log.Log(LogEntry{
					Level: "error", RequestID: rc.ID, Message: "panic_recovered",
					Error: errors.New(toString(rec)).Error(),
				})
				writeJSON(w, http.StatusInternalServerError, errorDTO{
					RequestID: rc.ID, Status: "error", ErrorCode: "internal_panic",
					Error:    toString(rec),
					Location: callerLocation(3),
					Version:  Version,
				})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func toString(v any) string {
	switch t := v.(type) {
	case error:
		return t.Error()
	case string:
		return t
	default:
		b, _ := json.Marshal(t)
		return string(b)
	}
}
