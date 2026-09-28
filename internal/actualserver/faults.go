// Package actualserver serves the physical resource plane over HTTP and owns
// its own SQLite database. A test-only admin API installs fault injections
// (lost responses, injected failures, stale snapshots, version conflicts);
// no cloud account or external system is involved.
package actualserver

import (
	"encoding/json"
	"net/http"
	"sort"
	"sync"
)

// Fault names understood by the service. Faults are scoped to an owner UID so
// that one resource can be failing while others converge.
const (
	// FaultCreateResponseLoss: create commits the row, then responds 500 with
	// X-Fault: create-response-loss.
	FaultCreateResponseLoss = "create-response-loss"
	// FaultDeleteFailed: delete responds 500 without removing the row.
	FaultDeleteFailed = "delete-failed"
	// FaultDeleteResponseLoss: delete removes the row, then responds 500.
	FaultDeleteResponseLoss = "delete-response-loss"
	// FaultUpdateConflict: PUT always responds 412 for the owner's resource.
	FaultUpdateConflict = "update-conflict"
	// FaultUpdateResponseLoss: update commits, then responds 500.
	FaultUpdateResponseLoss = "update-response-loss"
	// FaultStaleGet: GET serves the last pre-update snapshot with
	// X-Served-Snapshot: true and the true version in X-Current-Version.
	FaultStaleGet = "stale-get"
)

// ValidFaults is the accepted fault vocabulary.
var ValidFaults = map[string]bool{
	FaultCreateResponseLoss: true,
	FaultDeleteFailed:       true,
	FaultDeleteResponseLoss: true,
	FaultUpdateConflict:     true,
	FaultUpdateResponseLoss: true,
	FaultStaleGet:           true,
}

// faultManager is an in-memory map ownerUID -> fault name.
type faultManager struct {
	mu     sync.RWMutex
	faults map[string]string
}

func newFaultManager() *faultManager {
	return &faultManager{faults: map[string]string{}}
}

func (f *faultManager) set(ownerUID, fault string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if fault == "" {
		delete(f.faults, ownerUID)
		return
	}
	f.faults[ownerUID] = fault
}

func (f *faultManager) clear() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.faults = map[string]string{}
}

func (f *faultManager) get(ownerUID string) (string, bool) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	v, ok := f.faults[ownerUID]
	return v, ok
}

func (f *faultManager) snapshot() map[string]string {
	f.mu.RLock()
	defer f.mu.RUnlock()
	out := make(map[string]string, len(f.faults))
	for k, v := range f.faults {
		out[k] = v
	}
	return out
}

// sortedOwners helper for deterministic listings.
func sortedOwners(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func errorBody(msg string) map[string]string { return map[string]string{"error": msg} }
