package apiserver

import (
	"net/http"
	"sync"
)

// Test-only, one-shot fault hooks for the desired plane. They live in memory
// and are installed via /admin/faults. Production deployments never need this
// surface; it exists so tests can deterministically force a status write to
// lose optimistic concurrency and assert that the controller requeues instead
// of overwriting a newer spec.
const (
	// FaultStatusConflictOnce: the next status write for the object fails
	// with 409, exactly once, regardless of resourceVersion.
	FaultStatusConflictOnce = "status-conflict-once"
)

// ValidAPIFaults is the accepted desired-plane fault vocabulary.
var ValidAPIFaults = map[string]bool{
	FaultStatusConflictOnce: true,
}

type apiFaultManager struct {
	mu     sync.Mutex
	faults map[string]string
}

func newAPIFaultManager() *apiFaultManager {
	return &apiFaultManager{faults: map[string]string{}}
}

func (f *apiFaultManager) set(uid, fault string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if fault == "" {
		delete(f.faults, uid)
		return
	}
	f.faults[uid] = fault
}

func (f *apiFaultManager) take(uid, fault string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.faults[uid] != fault {
		return false
	}
	delete(f.faults, uid)
	return true
}

func (f *apiFaultManager) snapshot() map[string]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]string, len(f.faults))
	for k, v := range f.faults {
		out[k] = v
	}
	return out
}

func (f *apiFaultManager) clear() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.faults = map[string]string{}
}

func (s *Server) adminFaults(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.apiFaults.snapshot())
	case http.MethodPost, http.MethodPut:
		var req struct {
			UID   string `json:"uid"`
			Fault string `json:"fault"`
		}
		if err := decodeBody(r, &req); err != nil {
			fail(w, http.StatusBadRequest, requestID(r), "invalid JSON")
			return
		}
		if req.UID == "" {
			fail(w, http.StatusBadRequest, requestID(r), "uid required")
			return
		}
		if req.Fault != "" && !ValidAPIFaults[req.Fault] {
			fail(w, http.StatusBadRequest, requestID(r), "unknown fault: "+req.Fault)
			return
		}
		s.apiFaults.set(req.UID, req.Fault)
		writeJSON(w, http.StatusOK, map[string]string{"uid": req.UID, "fault": req.Fault})
	case http.MethodDelete:
		s.apiFaults.clear()
		w.WriteHeader(http.StatusNoContent)
	default:
		fail(w, http.StatusMethodNotAllowed, requestID(r), "method not allowed")
	}
}
