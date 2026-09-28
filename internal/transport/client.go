// Package transport exposes the causal core over HTTP and implements the
// fixed-membership peer client.
//
// Endpoints:
//
//	POST /events          local event ingress -> fan-out to peers
//	POST /messages        peer message ingress (network -> core.Ingest)
//	GET  /status          delivered clock, pending gaps, buffer levels
//	GET  /replay?since=N  delivered log since delivery sequence N
//	GET  /healthz         liveness
//
// Verdicts map to distinct HTTP status codes so failure categories are
// observable on the wire; internal errors are never 2xx.
package transport

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"cbcast/internal/core"
	"cbcast/internal/protocol"
)

// PeerClient sends envelopes to one peer.
type PeerClient interface {
	SendMessage(ctx context.Context, env *protocol.Envelope) (*protocol.IngestResult, int, error)
}

// FanOut broadcasts a locally produced envelope to every other member.
type FanOut func(ctx context.Context, env *protocol.Envelope) PeerResults

// PeerResults is nodeID -> outcome.
type PeerResults map[string]PeerResult

// PeerResult is one peer delivery outcome.
type PeerResult struct {
	Delivered bool                       `json:"delivered"`
	Verdict   protocol.VerdictKind       `json:"verdict,omitempty"`
	HTTP      int                        `json:"http_status,omitempty"`
	Result    *protocol.IngestResult     `json:"result,omitempty"`
	Error     string                     `json:"error,omitempty"`
}

// httpClient is the concrete PeerClient.
type httpClient struct {
	base string
	hc   *http.Client
}

// NewHTTPPeerClient builds a PeerClient for "http://host:port".
func NewHTTPPeerClient(base string) PeerClient {
	return &httpClient{
		base: base,
		hc:   &http.Client{Timeout: 5 * time.Second},
	}
}

func (c *httpClient) SendMessage(ctx context.Context, env *protocol.Envelope) (*protocol.IngestResult, int, error) {
	body, err := json.Marshal(env)
	if err != nil {
		return nil, 0, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/messages", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var res protocol.IngestResult
	if err := json.Unmarshal(raw, &res); err != nil {
		return nil, resp.StatusCode, fmt.Errorf("peer returned non-JSON (status %d): %s", resp.StatusCode, truncate(raw))
	}
	return &res, resp.StatusCode, nil
}

func truncate(b []byte) string {
	const n = 200
	if len(b) <= n {
		return string(b)
	}
	return string(b[:n]) + "..."
}
