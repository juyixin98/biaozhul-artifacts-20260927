package node

import (
	"encoding/json"
	"errors"
	"net/http"

	"clsnap/internal/errs"
	"clsnap/internal/protocol"
)

type apiError struct {
	Class   string `json:"class"`
	Code    string `json:"code"`
	Message string `json:"message"`
	RunID   string `json:"run_id"`
}

type transferReq struct {
	To     string `json:"to"`
	TxID   string `json:"tx_id"`
	Amount int64  `json:"amount"`
	Memo   string `json:"memo,omitempty"`
}

type snapshotReq struct {
	SessionID string `json:"session_id"`
}

type abortReq struct {
	SessionID string `json:"session_id"`
	Reason    string `json:"reason"`
}

type pinReq struct {
	Peer   string `json:"peer"`
	Pinned bool   `json:"pinned"`
}

// Handler builds the HTTP mux for one node.
func (n *Node) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", n.handleHealth)
	mux.HandleFunc("GET /state", n.handleState)
	mux.HandleFunc("POST /transfer", n.handleTransfer)
	mux.HandleFunc("POST /snapshot", n.handleSnapshot)
	mux.HandleFunc("GET /snapshots", n.handleListSnapshots)
	mux.HandleFunc("GET /snapshots/{id}", n.handleGetSnapshot)
	mux.HandleFunc("GET /journal", n.handleJournal)
	mux.HandleFunc("POST /message", n.handleMessage)
	mux.HandleFunc("POST /admin/pin", n.handlePin)
	mux.HandleFunc("POST /admin/abort", n.handleAbort)
	mux.HandleFunc("GET /admin/outbox", n.handleOutbox)
	return mux
}

func (n *Node) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"node_id":          n.cfg.NodeID,
		"run_id":           n.RunID(),
		"lamport":          n.clk.Get(),
		"aborted_at_boot":  n.abortedAtBoot,
		"peers":            peerIDs(n.cfg.Peers),
	})
}

func (n *Node) handleState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"node_id":  n.cfg.NodeID,
		"run_id":   n.RunID(),
		"lamport":  n.clk.Get(),
		"balances": n.st.Balances(),
	})
}

func (n *Node) handleTransfer(w http.ResponseWriter, r *http.Request) {
	var req transferReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	env, err := n.SubmitTransfer(r.Context(), req.To, protocol.Transfer{
		TxID: req.TxID, Amount: req.Amount, Memo: req.Memo,
	})
	if err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]interface{}{
		"accepted": true, "msg_id": env.MsgID, "seq": env.Seq, "lamport": env.Lamport,
	})
}

func (n *Node) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	var req snapshotReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	rec, err := n.InitiateSnapshot(r.Context(), req.SessionID)
	if err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusCreated, rec)
}

func (n *Node) handleListSnapshots(w http.ResponseWriter, r *http.Request) {
	recs, err := n.st.ListSessions()
	if err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"snapshots": recs})
}

func (n *Node) handleGetSnapshot(w http.ResponseWriter, r *http.Request) {
	rec, err := n.st.GetSession(r.PathValue("id"))
	if err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusOK, rec)
}

func (n *Node) handleJournal(w http.ResponseWriter, r *http.Request) {
	limit := 0
	events, err := n.st.Journal(r.Context(), r.URL.Query().Get("session_id"), limit)
	if err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"node_id": n.cfg.NodeID, "events": events})
}

func (n *Node) handleMessage(w http.ResponseWriter, r *http.Request) {
	var env protocol.Envelope
	if err := decode(r, &env); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	if err := n.HandleIncoming(r.Context(), env); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (n *Node) handlePin(w http.ResponseWriter, r *http.Request) {
	var req pinReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	if err := n.SetPinned(req.Peer, req.Pinned); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"peer": req.Peer, "pinned": req.Pinned})
}

func (n *Node) handleOutbox(w http.ResponseWriter, r *http.Request) {
	peer := r.URL.Query().Get("peer")
	l, err := n.st.OutboxLen(peer)
	if err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	pinned := n.isPinned(peer)
	writeJSON(w, http.StatusOK, map[string]interface{}{"peer": peer, "total": l, "pinned": pinned})
}

func (n *Node) handleAbort(w http.ResponseWriter, r *http.Request) {
	var req abortReq
	if err := decode(r, &req); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	if req.Reason == "" {
		req.Reason = "manual abort"
	}
	if err := n.AbortSnapshot(r.Context(), req.SessionID, req.Reason); err != nil {
		writeErr(w, err, n.RunID())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"aborted": req.SessionID})
}

func decode(r *http.Request, v interface{}) error {
	dec := json.NewDecoder(ioLimitReader(r.Body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		if errors.Is(err, errBodyTooLarge) {
			return errs.New(errs.ClassResourceExhausted, errs.CodeBodyTooLarge,
				"request body exceeds 1 MiB limit", err)
		}
		return errs.New(errs.ClassInputInvalid, errs.CodeMalformed,
			"cannot decode JSON request: "+err.Error(), err)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, err error, runID string) {
	if ce, ok := errs.As(err); ok {
		writeJSON(w, ce.HTTPStatus(), apiError{
			Class: string(ce.Class), Code: ce.Code, Message: ce.Message, RunID: runID,
		})
		return
	}
	writeJSON(w, http.StatusInternalServerError, apiError{
		Class: "internal", Code: "internal", Message: err.Error(), RunID: runID,
	})
}

func peerIDs(ps []Peer) []string {
	out := make([]string, 0, len(ps))
	for _, p := range ps {
		out = append(out, p.ID)
	}
	return out
}
