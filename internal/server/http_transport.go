package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

// HTTPTransport sends envelopes to peer processes over the standard library
// HTTP client.
//
// FIFO guarantee: plain HTTP requests over independent TCP connections are
// NOT generally ordered, so relying on "requests usually arrive in order"
// would violate the documented reliable-FIFO assumption. Instead this
// transport maintains ONE mutex per directed channel (sender side) and stamps
// each envelope with its channel-local Seq; the receiver rejects an envelope
// whose Seq is not exactly (lastSeen+1) with state_conflict/fifo_violation.
// The sender mutex makes that the normal order even before the check; the
// check makes any out-of-order delivery a loud, classified failure instead of
// silent corruption. Redelivery of the last acked seq is accepted
// idempotently (at-least-once send + receiver dedup).
type HTTPTransport struct {
	self    protocol.NodeID
	urls    map[protocol.NodeID]string
	client  *http.Client
	mu      map[protocol.NodeID]*sync.Mutex
}

// NewHTTPTransport builds a client-side transport.
func NewHTTPTransport(self protocol.NodeID, peerURLs map[protocol.NodeID]string) *HTTPTransport {
	t := &HTTPTransport{
		self:   self,
		urls:   peerURLs,
		client: &http.Client{Timeout: 10 * time.Second},
		mu:     make(map[protocol.NodeID]*sync.Mutex),
	}
	for n := range peerURLs {
		t.mu[n] = &sync.Mutex{}
	}
	return t
}

// Send POSTs one envelope to /msg on the destination peer.
func (t *HTTPTransport) Send(ctx context.Context, env protocol.Envelope) error {
	url, ok := t.urls[env.Dst]
	if !ok {
		return apperr.Inputf(apperr.CodeUnknownPeer, "no url configured for peer %s", env.Dst)
	}
	lock := t.mu[env.Dst]
	lock.Lock()
	defer lock.Unlock()

	body, err := json.Marshal(env)
	if err != nil {
		return apperr.Inputf(apperr.CodeMalformed, "encode envelope: %v", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url+"/msg", bytes.NewReader(body))
	if err != nil {
		return apperr.Failure(apperr.CodeTransport, "HTTPTransport.Send", "build request", err)
	}
	req.Header.Set("content-type", "application/json")
	resp, err := t.client.Do(req)
	if err != nil {
		return apperr.Failure(apperr.CodeTransport, "HTTPTransport.Send",
			fmt.Sprintf("http call to %s failed", env.Dst), err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusConflict || resp.StatusCode == http.StatusBadRequest ||
		resp.StatusCode == http.StatusTooManyRequests {
		var ae EnvelopeError
		if dec := json.NewDecoder(resp.Body).Decode(&ae); dec == nil && ae.Error.Code != "" {
			return &apperr.Error{
				Kind: apperr.Kind(ae.Error.Kind), Code: ae.Error.Code,
				Msg: ae.Error.Message, Op: "remote:" + ae.Error.Op,
			}
		}
	}
	if resp.StatusCode >= 300 {
		return apperr.Failure(apperr.CodeTransport, "HTTPTransport.Send",
			fmt.Sprintf("peer %s returned HTTP %d", env.Dst, resp.StatusCode), nil)
	}
	return nil
}

// EnvelopeError is the wire form of an apperr on /msg responses.
type EnvelopeError struct {
	Error struct {
		Kind    string `json:"kind"`
		Code    string `json:"code"`
		Op      string `json:"op"`
		Message string `json:"message"`
	} `json:"error"`
}
