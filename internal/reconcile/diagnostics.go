package reconcile

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"

	"crcontroller/internal/controllerstore"
)

// DiagnosticsServer exposes the controller ledger and metrics read-only.
type DiagnosticsServer struct {
	store *controllerstore.Store
	ctl   *Controller
	mux   *http.ServeMux
}

// NewDiagnosticsServer wires the read-only diagnostics surface.
func NewDiagnosticsServer(st *controllerstore.Store, ctl *Controller) *DiagnosticsServer {
	d := &DiagnosticsServer{store: st, ctl: ctl, mux: http.NewServeMux()}
	d.mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	d.mux.HandleFunc("/diagnostics/ledger", func(w http.ResponseWriter, r *http.Request) {
		n := atoiOr(r.URL.Query().Get("limit"), 100)
		entries, err := st.LatestLedger(r.Context(), n)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, entries)
	})
	d.mux.HandleFunc("/diagnostics/ledger/", func(w http.ResponseWriter, r *http.Request) {
		uid := r.URL.Path[len("/diagnostics/ledger/"):]
		n := atoiOr(r.URL.Query().Get("limit"), 1000)
		entries, err := st.LedgerByUID(r.Context(), uid, n)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, entries)
	})
	d.mux.HandleFunc("/diagnostics/state/", func(w http.ResponseWriter, r *http.Request) {
		uid := r.URL.Path[len("/diagnostics/state/"):]
		st, err := d.store.GetState(r.Context(), uid)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, st)
	})
	d.mux.HandleFunc("/diagnostics/queue", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, ctl.Queue().Stats())
	})
	return d
}

// Handler exposes the router.
func (d *DiagnosticsServer) Handler() http.Handler { return d.mux }

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func atoiOr(v string, def int) int {
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def
	}
	return n
}

// ListenAndServe starts the diagnostics HTTP server on addr. It is a
// convenience used by cmd/controller.
func (d *DiagnosticsServer) ListenAndServe(ctx context.Context, addr string) *http.Server {
	srv := &http.Server{Addr: addr, Handler: d.mux}
	go func() {
		_ = srv.ListenAndServe()
	}()
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	return srv
}
