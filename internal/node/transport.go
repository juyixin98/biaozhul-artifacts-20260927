package node

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"clsnap/internal/errs"
	"clsnap/internal/protocol"
)

// Transport is the narrow HTTP contract used by the node, so tests can replace
// it with an in-process dispatcher.
type Transport interface {
	Deliver(ctx context.Context, baseURL string, env protocol.Envelope) error
}

// HTTPTransport posts envelopes to peer /message.
//
// Reliability: the caller only acks its persisted outbox item when Deliver
// returns nil, so crashes or connection failures cause a redelivery (the
// receiver is idempotent by MsgID). FIFO ordering is the caller's job (it
// never passes the head of a channel until the head is acked).
type HTTPTransport struct {
	Client *http.Client
}

func NewHTTPTransport() *HTTPTransport {
	return &HTTPTransport{Client: &http.Client{Timeout: 3 * time.Second}}
}

type wireError struct {
	Class   string `json:"class"`
	Code    string `json:"code"`
	Message string `json:"message"`
	RunID   string `json:"run_id,omitempty"`
}

func (t *HTTPTransport) Deliver(ctx context.Context, baseURL string, env protocol.Envelope) error {
	body, err := json.Marshal(env)
	if err != nil {
		return errs.New(errs.ClassInputInvalid, errs.CodeMalformed, "cannot encode envelope", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/message", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := t.Client.Do(req)
	if err != nil {
		return errs.New(errs.ClassUnavailable, errs.CodePeerUnavailable,
			fmt.Sprintf("peer %s unreachable: %v", baseURL, err), err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 == 2 {
		return nil
	}
	var we wireError
	_ = json.NewDecoder(ioLimitReader(resp.Body)).Decode(&we)
	if we.Class != "" {
		return errs.New(errs.Class(we.Class), we.Code, we.Message, nil)
	}
	return errs.New(errs.ClassUnavailable, errs.CodePeerUnavailable,
		fmt.Sprintf("peer %s returned HTTP %d", baseURL, resp.StatusCode), nil)
}
