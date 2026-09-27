// Package reassembly turns an ordered stream of observed TCP segments into
// the two byte streams of every connection generation, preserving evidence
// of every missing and contradictory byte.
//
// Semantics enforced here (see tests):
//
//   - Sequence numbers use 32-bit modular (wrapping) window arithmetic;
//     SYN and FIN each consume exactly one sequence number.
//   - Retransmissions carrying identical bytes are accepted as evidence but
//     never emitted twice. Contradictory retransmissions are isolated per
//     the configured overlap policy; bytes already delivered are immutable.
//   - Each fresh handshake on a reused 4-tuple opens a new generation; data
//     segments are bound to generations by window checks, never by guessing.
//   - Only contiguous bytes with evidence are delivered. Holes are recorded
//     as gaps; nothing is fabricated. Captures missing the handshake may be
//     ingested into an explicitly *inferred* generation.
package reassembly

import (
	"context"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/store"
)

// Gap describes one interval of absent evidence in stream-offset space.
type Gap struct {
	Direction string `json:"direction"`
	StartOff  uint64 `json:"start_off"`
	EndOff    uint64 `json:"end_off"`
	// Status is "open" or "filled" (filled gaps are returned too, naming
	// the packet that later supplied the bytes).
	Status         string `json:"status"`
	FilledRecordID string `json:"filled_record_id,omitempty"`
}

// Conflict describes contradictory bytes at one absolute sequence range.
type Conflict struct {
	Direction         string `json:"direction"`
	StartAbs          uint64 `json:"start_abs"`
	EndAbs            uint64 `json:"end_abs"`
	StartOff          uint64 `json:"start_off"`
	EndOff            uint64 `json:"end_off"`
	IncumbentRecordID string `json:"incumbent_record_id"`
	NewcomerRecordID  string `json:"newcomer_record_id"`
	IncumbentSHA      string `json:"incumbent_sha256"`
	NewcomerSHA       string `json:"newcomer_sha256"`
	Winner            string `json:"winner"` // "incumbent" | "newcomer" | "held"
	Status            string `json:"status"`
}

// DeliveredChunk is one contiguous emission of stream bytes.
type DeliveredChunk struct {
	Direction string
	StreamOff uint64
	Data      []byte
	RecordID  string // packet that supplied the head bytes (provenance)
}

// DirectionResult summarizes what one packet contributed per direction.
type DirectionResult struct {
	Direction string
	// Accepted says the packet was evidence-consistent (in-order data,
	// buffered out-of-order data, identical retransmit, SYN/FIN/ACK).
	Accepted bool
	// Category is the diag category of the verdict.
	Category diag.Category
	Chunks   []DeliveredChunk
	// BytesDedup is the number of bytes seen but not emitted because an
	// identical copy was already present/delivered.
	BytesDedup int
	// BytesRejected is the number of bytes refused on conflict.
	BytesRejected int
}

// ProcessResult is the verdict for one packet.
type ProcessResult struct {
	RequestID string
	FlowKey   string
	GenIndex  int
	Inferred  bool
	Direction string
	Decision  diag.Decision
	Category  diag.Category
	Reason    string

	// SegSeqAbs/SegEndAbs are the data sequence extent in absolute space
	// (including wrap extension), zero when not applicable.
	SegSeqAbs uint64
	SegEndAbs uint64

	DirResults []DirectionResult
	Records    []diag.Record

	// DuplicatePacket marks an exact (source,record_id) replay.
	DuplicatePacket bool
}

// Chunks flattens delivered chunks from all per-direction results.
func (r ProcessResult) Chunks() []DeliveredChunk {
	var out []DeliveredChunk
	for _, d := range r.DirResults {
		out = append(out, d.Chunks...)
	}
	return out
}

// Persister is the storage contract of the engine. *store.Store satisfies it.
type Persister interface {
	UpsertConnection(ctx context.Context, c store.ConnectionRow) error
	UpsertGeneration(ctx context.Context, g store.GenerationRow) error
	InsertGeneration(ctx context.Context, g store.GenerationRow) error
	SaveGeneration(ctx context.Context, g store.GenerationRow) error
	InsertPacket(ctx context.Context, p store.PacketRow) error
	PacketExists(ctx context.Context, source, recordID string) (bool, error)
	InsertChunk(ctx context.Context, c store.ChunkRow) error
	OpenGap(ctx context.Context, g store.GapRow, ingestSeq int64) error
	FillGap(ctx context.Context, flowKey string, gen int, dir string, startOff, endOff uint64, recordID string, ingestSeq int64) error
	OpenGaps(ctx context.Context, flowKey string, gen int, dir string) ([][2]uint64, error)
	ReconcileOpenGap(ctx context.Context, flowKey string, gen int, dir string, startOff, endOff uint64, ingestSeq int64) error
	FillGapsUpTo(ctx context.Context, flowKey string, gen int, dir string, frontier uint64, recordID string, ingestSeq int64) error
	InsertConflict(ctx context.Context, c store.ConflictRow) error
	InsertDiagnosticRow(ctx context.Context, r diag.Record, ingestSeq int64) error
	NextIngestSeq(ctx context.Context) (int64, error)
}

// Options wires the engine.
type Options struct {
	Cfg   config.ReassemblyConfig
	Diag  diagcfg
	Store Persister
	Sink  *diag.Sink
}

// diagcfg is the subset of diagnostic config the engine needs.
type diagcfg struct {
	PayloadPreviewBytes int
}

// NewOptions builds Options from full config pieces.
func NewOptions(cfg config.Config, p Persister, sink *diag.Sink) Options {
	return Options{
		Cfg:   cfg.Reassembly,
		Diag:  diagcfg{PayloadPreviewBytes: cfg.Diagnostics.PayloadPreviewBytes},
		Store: p,
		Sink:  sink,
	}
}
