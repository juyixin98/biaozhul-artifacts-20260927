package diag

import (
	"io"
	"os"
	"sync"
)

// Recorder persists records (the SQLite store implements it). It is allowed
// to be nil: the sink then only writes the text log.
type Recorder interface {
	InsertDiagnostic(r Record) error
}

// Sink fans every Record out to a text writer and, optionally, a Recorder.
type Sink struct {
	mu      sync.Mutex
	w       io.Writer
	rec     Recorder
	maskIPs bool
}

// NewSink builds a sink. w nil defaults to stderr; path "" keeps stderr,
// otherwise the file is opened (append) and used.
func NewSink(w io.Writer, rec Recorder, maskIPs bool) *Sink {
	if w == nil {
		w = os.Stderr
	}
	return &Sink{w: w, rec: rec, maskIPs: maskIPs}
}

// Emit renders and persists one record. Persistence errors are ignored on
// purpose: diagnostics must never break ingestion; the caller already gets
// the record in its result slice.
func (s *Sink) Emit(r Record) {
	if s == nil {
		return
	}
	s.mu.Lock()
	_, _ = s.w.Write([]byte(r.Text(s.maskIPs) + "\n"))
	s.mu.Unlock()
	if s.rec != nil {
		_ = s.rec.InsertDiagnostic(r)
	}
}
