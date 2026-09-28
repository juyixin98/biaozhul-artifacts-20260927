package server

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"example.com/cgcoord/coordinator"
	"example.com/cgcoord/protocol"
)

func (s *Server) routes(mux *http.ServeMux) {
	mux.HandleFunc("GET /healthz", s.handleHealth)

	mux.HandleFunc("POST /v1/groups", s.handleCreateGroup)
	mux.HandleFunc("GET /v1/groups/{group}/state", s.handleState)
	mux.HandleFunc("GET /v1/groups/{group}/journal", s.handleJournal)
	mux.HandleFunc("GET /v1/groups/{group}/replay", s.handleReplay)

	mux.HandleFunc("POST /v1/groups/{group}/members", s.handleJoin)
	mux.HandleFunc("POST /v1/groups/{group}/members/{member}/heartbeat", s.handleHeartbeat)
	mux.HandleFunc("POST /v1/groups/{group}/members/{member}/leave", s.handleLeave)
	mux.HandleFunc("POST /v1/groups/{group}/members/{member}/sync", s.handleSync)
	mux.HandleFunc("POST /v1/groups/{group}/members/{member}/revoke-acks", s.handleAck)

	mux.HandleFunc("POST /v1/groups/{group}/commits", s.handleCommit)
	mux.HandleFunc("GET /v1/groups/{group}/offsets", s.handleFetchOffsets)

	mux.HandleFunc("POST /v1/groups/{group}/recover", s.handleRecover)
	mux.HandleFunc("POST /v1/admin/groups/{group}/sweep", s.handleSweep)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, r, http.StatusOK, map[string]any{"status": "ok"})
}

type createGroupBody struct {
	Name   string             `json:"name"`
	Topics []topicBody        `json:"topics"`
	Config groupConfigBody    `json:"config"`
}

type topicBody struct {
	Name       string `json:"name"`
	Partitions int    `json:"partitions"`
}

type groupConfigBody struct {
	RevokeTimeoutMS         int `json:"revoke_timeout_ms"`
	QuarantineTimeoutMS     int `json:"quarantine_timeout_ms"`
	DefaultSessionTimeoutMS int `json:"default_session_timeout_ms"`
}

func (b groupConfigBody) toConfig() protocol.GroupConfig {
	return protocol.GroupConfig{
		RevokeTimeout:         time.Duration(b.RevokeTimeoutMS) * time.Millisecond,
		QuarantineTimeout:     time.Duration(b.QuarantineTimeoutMS) * time.Millisecond,
		DefaultSessionTimeout: time.Duration(b.DefaultSessionTimeoutMS) * time.Millisecond,
	}
}

func (s *Server) handleCreateGroup(w http.ResponseWriter, r *http.Request) {
	var body createGroupBody
	if !decode(w, r, &body) {
		return
	}
	topics := make([]protocol.TopicSpec, 0, len(body.Topics))
	for _, t := range body.Topics {
		topics = append(topics, protocol.TopicSpec{Name: protocol.Topic(t.Name), Partitions: t.Partitions})
	}
	err := s.coord.CreateGroup(r.Context(), coordinator.CreateGroupRequest{
		Name:      body.Name,
		Topics:    topics,
		Config:    body.Config.toConfig(),
		RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusCreated, map[string]any{
		"request_id": requestID(r),
		"group":      body.Name,
		"status":     "created",
	})
}

type joinBody struct {
	MemberID          string   `json:"member_id"`
	Topics            []string `json:"topics"`
	SessionTimeoutMS  int      `json:"session_timeout_ms"`
	Metadata          string   `json:"metadata"`
}

func (s *Server) handleJoin(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	var body joinBody
	if !decode(w, r, &body) {
		return
	}
	topics := make([]protocol.Topic, 0, len(body.Topics))
	for _, t := range body.Topics {
		topics = append(topics, protocol.Topic(t))
	}
	res, err := s.coord.Join(r.Context(), coordinator.JoinRequest{
		Group: group,
		Member: protocol.MemberSpec{
			ID:             protocol.MemberID(body.MemberID),
			Subscription:   protocol.Subscription{Topics: topics},
			SessionTimeout: time.Duration(body.SessionTimeoutMS) * time.Millisecond,
			Metadata:       body.Metadata,
		},
		RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id":   requestID(r),
		"group":        group,
		"member":       string(res.Member),
		"phase":        string(res.Phase),
		"generation":   int64(res.Generation),
		"revocations":  res.Revocations,
	})
}

type genBody struct {
	Generation int64 `json:"generation"`
}

func (s *Server) handleHeartbeat(w http.ResponseWriter, r *http.Request) {
	group, member := r.PathValue("group"), r.PathValue("member")
	var body genBody
	if r.ContentLength > 0 {
		if !decode(w, r, &body) {
			return
		}
	}
	res, err := s.coord.Heartbeat(r.Context(), coordinator.HeartbeatRequest{
		Group: group, Member: protocol.MemberID(member),
		Generation: protocol.Generation(body.Generation), RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, heartbeatJSON(requestID(r), group, member, res))
}

func (s *Server) handleLeave(w http.ResponseWriter, r *http.Request) {
	group, member := r.PathValue("group"), r.PathValue("member")
	res, err := s.coord.Leave(r.Context(), coordinator.LeaveRequest{
		Group: group, Member: protocol.MemberID(member), RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id": requestID(r), "group": group, "member": member,
		"phase": string(res.Phase), "generation": int64(res.Generation),
	})
}

func (s *Server) handleSync(w http.ResponseWriter, r *http.Request) {
	group, member := r.PathValue("group"), r.PathValue("member")
	var body genBody
	if r.ContentLength > 0 {
		if !decode(w, r, &body) {
			return
		}
	}
	res, err := s.coord.Sync(r.Context(), coordinator.SyncRequest{
		Group: group, Member: protocol.MemberID(member),
		Generation: protocol.Generation(body.Generation), RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id": requestID(r), "group": group, "member": member,
		"phase": string(res.Phase), "generation": int64(res.Generation),
		"stable": res.Stable, "assignment": res.Assignment,
	})
}

type ackBody struct {
	Generation int64         `json:"generation"`
	Partitions []protocol.TP `json:"partitions"`
}

func (s *Server) handleAck(w http.ResponseWriter, r *http.Request) {
	group, member := r.PathValue("group"), r.PathValue("member")
	var body ackBody
	if !decode(w, r, &body) {
		return
	}
	res, err := s.coord.AckRevocations(r.Context(), coordinator.AckRequest{
		Group: group, Member: protocol.MemberID(member),
		Generation: protocol.Generation(body.Generation),
		Partitions: body.Partitions, RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, ackJSON(requestID(r), group, member, res))
}

type recoverBody struct {
	Generation int64         `json:"generation"`
	Partitions []protocol.TP `json:"partitions"`
	Reason     string        `json:"reason"`
}

func (s *Server) handleRecover(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	var body recoverBody
	if !decode(w, r, &body) {
		return
	}
	if body.Reason == "" {
		writeJSON(w, r, http.StatusBadRequest, errorBodyLocal("reason is required: recovery must state why it is safe to assume the old owner stopped"))
		return
	}
	res, err := s.coord.RecoverAck(r.Context(), coordinator.RecoverAckRequest{
		Group: group, Generation: protocol.Generation(body.Generation),
		Partitions: body.Partitions, Reason: body.Reason, RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, ackJSON(requestID(r), group, "", res))
}

type commitBody struct {
	Member     string          `json:"member"`
	Generation int64           `json:"generation"`
	Items      []commitItemDTO `json:"items"`
}

type commitItemDTO struct {
	Topic       string `json:"topic"`
	Partition   int32  `json:"partition"`
	Offset      int64  `json:"offset"`
	LeaderEpoch int64  `json:"leader_epoch"`
}

func (s *Server) handleCommit(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	var body commitBody
	if !decode(w, r, &body) {
		return
	}
	items := make([]coordinator.CommitItem, 0, len(body.Items))
	for _, it := range body.Items {
		items = append(items, coordinator.CommitItem{
			TP:          protocol.TP{Topic: protocol.Topic(it.Topic), Partition: protocol.Partition(it.Partition)},
			Offset:      it.Offset,
			LeaderEpoch: it.LeaderEpoch,
		})
	}
	res, err := s.coord.Commit(r.Context(), coordinator.CommitRequest{
		Group: group, Member: protocol.MemberID(body.Member),
		Generation: protocol.Generation(body.Generation),
		Items: items, RequestID: requestID(r),
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	out := map[string]any{
		"request_id": requestID(r),
		"group":      group,
		"generation": int64(res.Generation),
		"items":      res.Items,
	}
	// A batch with any rejected item is HTTP 207 Multi-Status: the accepted
	// items are durable; the rejected ones carry their failure code.
	if res.HasRejections() {
		writeJSON(w, r, http.StatusMultiStatus, out)
		return
	}
	writeJSON(w, r, http.StatusOK, out)
}

func (s *Server) handleFetchOffsets(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	var tps []protocol.TP
	if raw := r.URL.Query().Get("partitions"); raw != "" {
		for _, pair := range strings.Split(raw, ",") {
			tp, err := parseTP(pair)
			if err != nil {
				writeJSON(w, r, http.StatusBadRequest, errorBodyLocal(err.Error()))
				return
			}
			tps = append(tps, tp)
		}
	}
	offsets, err := s.coord.FetchOffsets(r.Context(), coordinator.FetchOffsetsRequest{
		Group: group, Partitions: tps,
	})
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id": requestID(r), "group": group, "offsets": offsets,
	})
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	st, err := s.coord.GetState(r.Context(), group)
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	view := coordinator.BuildView(st)
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id": requestID(r),
		"state":      view,
	})
}

func (s *Server) handleJournal(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	from, _ := strconv.ParseInt(r.URL.Query().Get("from"), 10, 64)
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	events, err := s.coord.JournalPage(r.Context(), group, protocol.Seq(from), limit)
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id": requestID(r),
		"group":      group,
		"from":       from,
		"events":     events,
	})
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	built, err := s.coord.RebuildState(r.Context(), group)
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	view := coordinator.BuildView(built)
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id": requestID(r),
		"source":     "journal-rebuild",
		"state":      view,
	})
}

func (s *Server) handleSweep(w http.ResponseWriter, r *http.Request) {
	group := r.PathValue("group")
	at := time.Now()
	if v := r.URL.Query().Get("at_unix_ms"); v != "" {
		ms, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			writeJSON(w, r, http.StatusBadRequest, errorBodyLocal("at_unix_ms must be unix milliseconds"))
			return
		}
		at = time.UnixMilli(ms)
	}
	res, err := s.coord.Sweep(r.Context(), group, at)
	if err != nil {
		writeError(w, r, requestID(r), err)
		return
	}
	writeJSON(w, r, http.StatusOK, map[string]any{
		"request_id":   requestID(r),
		"group":        group,
		"expired":      res.Expired,
		"quarantined":  res.Quarantined,
		"force_freed":  res.ForceFreed,
		"activated":    res.Activated,
		"phase":        string(res.Phase),
		"generation":   int64(res.Generation),
	})
}

func heartbeatJSON(reqID, group, member string, res *coordinator.HeartbeatResult) map[string]any {
	return map[string]any{
		"request_id":   reqID,
		"group":        group,
		"member":       member,
		"phase":        string(res.Phase),
		"generation":   int64(res.Generation),
		"revoke":       res.Revoke,
		"owned":        res.Owned,
		"quarantined":  res.Quarantined,
	}
}

func ackJSON(reqID, group, member string, res *coordinator.AckResult) map[string]any {
	return map[string]any{
		"request_id":  reqID,
		"group":       group,
		"member":      member,
		"phase":       string(res.Phase),
		"generation":  int64(res.Generation),
		"activated":   res.Activated,
		"acked":       res.Acked,
		"skipped":     res.Skipped,
	}
}

func parseTP(s string) (protocol.TP, error) {
	i := strings.LastIndex(s, "#")
	if i <= 0 || i == len(s)-1 {
		return protocol.TP{}, &protocol.Error{Code: protocol.ErrBadRequest, Message: "partition must be topic#index"}
	}
	idx, err := strconv.Atoi(s[i+1:])
	if err != nil || idx < 0 {
		return protocol.TP{}, &protocol.Error{Code: protocol.ErrBadRequest, Message: "partition index must be >= 0"}
	}
	return protocol.TP{Topic: protocol.Topic(s[:i]), Partition: protocol.Partition(idx)}, nil
}

func errorBodyLocal(msg string) errorBody {
	return errorBody{Error: errorPayload{Code: protocol.ErrBadRequest, Message: msg}}
}

func newRequestID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "req-" + hex.EncodeToString(b[:])
}

var _ = json.Marshal // keep encoding/json used as API evolves
