// Package service wires the reassembly engine, evidence store, diagnostics
// and HTTP API together. Each ingest request is analyzed by a fresh, isolated
// engine instance; results are persisted to SQLite and all subsequent reads
// are served from storage, so replay never mutates analysis state.
package service

import (
	"fmt"
	"time"

	"tcpreplay/internal/netmodel"
	"tcpreplay/internal/reassembly"
	"tcpreplay/internal/storage"
)

// IngestRequest is the JSON body for POST /api/v1/ingest.
type IngestRequest struct {
	// RequestID is caller-supplied correlation evidence. When empty the
	// server generates a random id and returns it.
	RequestID string `json:"request_id,omitempty"`
	// Packets are observations in arrival order. Reordering is part of the
	// test surface: the server never sorts by sequence number.
	Packets []netmodel.Packet `json:"packets"`
}

// GenerationSummary is one generation in the ingest response.
type GenerationSummary struct {
	Flow       string `json:"flow"`
	Generation int    `json:"generation"`
	Closed     bool   `json:"closed"`
	Reset      bool   `json:"reset"`
	// Contiguous stream lengths only — gaps/conflicts are fetched via report.
	AtoBBytes int64 `json:"a_to_b_contiguous_bytes"`
	BtoABytes int64 `json:"b_to_a_contiguous_bytes"`
	AtoBGaps  int   `json:"a_to_b_gaps"`
	BtoAGaps  int   `json:"b_to_a_gaps"`
}

// IngestResponse summarizes a completed analysis.
type IngestResponse struct {
	RequestID     string              `json:"request_id"`
	Source        string              `json:"source"`
	Policy        string              `json:"policy"`
	PacketCount   int                 `json:"packet_count"`
	CreatedAt     string              `json:"created_at"`
	Conflicts     int                 `json:"conflict_count"`
	EventsByLevel map[string]int      `json:"events_by_level"`
	Generations   []GenerationSummary `json:"generations"`
}

// Report is the full auditable result of one request.
type Report struct {
	Request   storage.RequestHeader       `json:"request"`
	Views     []reassembly.GenerationView `json:"generation_views"`
	Events    []reassembly.Event          `json:"events"`
	Conflicts []reassembly.Conflict       `json:"conflicts"`
	Packets   []storage.PacketMeta        `json:"packets"`
}

// analysisRun is the in-process working set of one ingest.
type analysisRun struct {
	policy  reassembly.OverlapPolicy
	preview bool
	manager *reassembly.Manager
	events  []reassembly.Event
	metas   []storage.PacketMeta
}

func newAnalysisRun(policy string, preview bool) *analysisRun {
	return &analysisRun{
		policy:  reassembly.OverlapPolicy(policy),
		preview: preview,
		manager: reassembly.NewManager(reassembly.OverlapPolicy(policy), preview),
	}
}

// feed pushes one packet in arrival order and archives the decision.
func (r *analysisRun) feed(idx int, pk netmodel.Packet, reqID string) {
	recordID := pk.RecordID
	if recordID == "" {
		recordID = autoRecordID(idx)
	}
	k, aToB := pk.FlowKeyAndDir()
	meta := storage.PacketMeta{
		Index: idx, RecordID: recordID, Flow: k.String(),
		Direction: dirName(aToB), RawSeq: pk.Seq, PayloadLen: len(pk.Payload),
		Flags: joinFlags(pk.Flags), Timestamp: pk.Timestamp,
	}
	res := r.manager.Process(pk, reqID, recordID)
	r.events = append(r.events, res.Events...)
	r.metas = append(r.metas, meta)
}

// finish returns the materialized relative-offset conflict list and clears the
// run accumulator. The manager holds authoritative conflicts; per-process
// results carry internal coordinates and are not stored directly.
func (r *analysisRun) conflicts() []reassembly.Conflict {
	return r.manager.Conflicts()
}

func (r *analysisRun) createdAt() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func autoRecordID(idx int) string { return formatRecordID(idx) }

func dirName(aToB bool) string {
	if aToB {
		return "a_to_b"
	}
	return "b_to_a"
}

func joinFlags(flags []string) string {
	out := ""
	for i, f := range flags {
		if i > 0 {
			out += ","
		}
		out += f
	}
	return out
}

func formatRecordID(idx int) string { return fmt.Sprintf("rec-%05d", idx+1) }
