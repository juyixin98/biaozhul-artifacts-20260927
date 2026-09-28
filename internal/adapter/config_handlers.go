package adapter

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"

	"replicactl/internal/config"
)

// configView is returned to clients with provenance fields.
type configView struct {
	Version  int            `json:"schema_version"`
	Revision int64          `json:"revision"`
	Config   map[string]any `json:"config"`
}

func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	cfg, rev, err := s.store.LoadConfig(r.Context())
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "CONFIG_UNREADABLE", err.Error())
		return
	}
	var raw map[string]any
	b, _ := json.Marshal(cfg)
	_ = json.Unmarshal(b, &raw)
	writeJSON(w, http.StatusOK, configView{Version: config.SchemaVersion, Revision: rev, Config: raw})
}

// handlePutConfig accepts a partial patch: it merges over the current
// configuration so omitted fields keep their values, validates the result,
// and bumps the revision.
func (s *Server) handlePutConfig(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r.Context())
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<16))
	if err != nil {
		writeError(w, r, http.StatusBadRequest, "BAD_BODY", "cannot read body")
		return
	}
	cur, rev, err := s.store.LoadConfig(r.Context())
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "CONFIG_UNREADABLE", err.Error())
		return
	}
	curB, _ := json.Marshal(cur)
	var merged map[string]any
	if err := json.Unmarshal(curB, &merged); err != nil {
		writeError(w, r, http.StatusInternalServerError, "CONFIG_CORRUPT", err.Error())
		return
	}
	var patch map[string]any
	if err := json.Unmarshal(body, &patch); err != nil {
		writeError(w, r, http.StatusBadRequest, "BAD_JSON", "invalid JSON: "+err.Error())
		return
	}
	for k, v := range patch {
		merged[k] = v
	}
	mergedB, _ := json.Marshal(merged)
	var updated config.Config
	if err := json.NewDecoder(bytes.NewReader(mergedB)).Decode(&updated); err != nil {
		writeError(w, r, http.StatusBadRequest, "BAD_CONFIG", "cannot decode merged config: "+err.Error())
		return
	}
	if err := updated.Validate(); err != nil {
		writeError(w, r, http.StatusBadRequest, "INVALID_CONFIG", err.Error())
		return
	}
	newRev, err := s.store.SaveConfig(r.Context(), updated, s.now().Format("2006-01-02T15:04:05.999999999Z07:00"))
	if err != nil {
		writeError(w, r, http.StatusInternalServerError, "CONFIG_SAVE_FAILED", err.Error())
		return
	}
	s.log.Info("config_replaced", "request_id", rid, "old_revision", rev, "new_revision", newRev)
	out, _ := json.Marshal(updated)
	var raw map[string]any
	_ = json.Unmarshal(out, &raw)
	writeJSON(w, http.StatusOK, configView{Version: config.SchemaVersion, Revision: newRev, Config: raw})
}
