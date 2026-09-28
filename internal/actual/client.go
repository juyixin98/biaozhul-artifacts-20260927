// Package actual is the controller-side HTTP adapter for the actual resource
// service. It classifies transport/HTTP failures into the categories the
// reconcile loop can reason about, and propagates request IDs so diagnostics
// on both planes can be correlated.
package actual

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"crcontroller/internal/model"
)

// ErrorKinds are the classified failure categories. They mirror
// model.FailureCategory but live on the adapter boundary so that callers do
// not import HTTP semantics.
type ErrorKind string

const (
	KindNotFound        ErrorKind = "NotFound"
	KindAlreadyOwned    ErrorKind = "AlreadyOwned"
	KindVersionConflict ErrorKind = "VersionConflict"
	KindResponseLost    ErrorKind = "ResponseLost"
	KindTransient       ErrorKind = "Transient"
	KindFaultInjected   ErrorKind = "FaultInjected"
	KindBadRequest      ErrorKind = "BadRequest"
)

// Error is a classified adapter failure.
type Error struct {
	Kind      ErrorKind
	Status    int
	RequestID string
	Body      string
}

func (e *Error) Error() string {
	return fmt.Sprintf("actual service: %s (status=%d requestID=%s): %s",
		e.Kind, e.Status, e.RequestID, truncate(e.Body, 300))
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// Client talks to the actual resource service.
type Client struct {
	BaseURL string
	HTTP    *http.Client
}

// New builds a client with sane timeouts.
func New(baseURL string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		HTTP: &http.Client{
			Timeout: 5 * time.Second,
		},
	}
}

func (c *Client) do(ctx context.Context, method, path, requestID string,
	body any) (*http.Response, []byte, error) {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, nil, err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, rdr)
	if err != nil {
		return nil, nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		// Network failure right after a mutating call is unknowable; the
		// caller decides whether to claim. Classified at call sites.
		return nil, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp, data, nil
}

// CreateResourceRequest is the create payload.
type CreateResourceRequest struct {
	RequestID  string         `json:"-"`
	ID         string         `json:"id"`
	OwnerUID   string         `json:"ownerUID"`
	Generation int64          `json:"generation"`
	SpecHash   string         `json:"specHash"`
	Spec       map[string]any `json:"spec"`
}

// Create creates (or, on conflict, returns) the physical resource for an
// owner. A 500 with header X-Fault: create-response-loss is mapped to
// KindResponseLost: the row was committed but the response was lost, so the
// caller must query/claim rather than retry blindly.
func (c *Client) Create(ctx context.Context, in CreateResourceRequest) (*model.ActualResource, error) {
	resp, data, err := c.do(ctx, http.MethodPost, "/v1/resources", in.RequestID, in)
	if err != nil {
		return nil, &Error{Kind: KindTransient, Status: 0,
			RequestID: in.RequestID, Body: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	switch {
	case resp.StatusCode == http.StatusCreated:
		var out model.ActualResource
		if err := json.Unmarshal(data, &out); err != nil {
			return nil, &Error{Kind: KindTransient, Status: resp.StatusCode,
				RequestID: rid, Body: err.Error()}
		}
		return &out, nil
	case resp.StatusCode == http.StatusConflict:
		// Idempotent create: owner already exists. Body carries the row.
		var out model.ActualResource
		if jerr := json.Unmarshal(data, &out); jerr == nil && out.ID != "" {
			return &out, &Error{Kind: KindAlreadyOwned, Status: resp.StatusCode,
				RequestID: rid}
		}
		return nil, &Error{Kind: KindAlreadyOwned, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode == http.StatusInternalServerError &&
		resp.Header.Get("X-Fault") == "create-response-loss":
		return nil, &Error{Kind: KindResponseLost, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode >= 500:
		return nil, &Error{Kind: KindTransient, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	default:
		return nil, &Error{Kind: KindBadRequest, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	}
}

// GetByOwner claims/looks up the resource owned by uid.
func (c *Client) GetByOwner(ctx context.Context, ownerUID, requestID string) (*model.ActualResource, error) {
	resp, data, err := c.do(ctx, http.MethodGet,
		"/v1/resources/by-owner/"+ownerUID, requestID, nil)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: requestID,
			Body: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode == http.StatusOK {
		var out model.ActualResource
		if err := json.Unmarshal(data, &out); err != nil {
			return nil, &Error{Kind: KindTransient, Status: resp.StatusCode,
				RequestID: rid}
		}
		return &out, nil
	}
	return nil, classifyRead(resp.StatusCode, rid, data)
}

// Get fetches by physical id. If serveStaleVersion is positive and the
// response carries X-Served-Snapshot: true, the body is a deliberately old
// snapshot that the controller must reject.
func (c *Client) Get(ctx context.Context, id, requestID string) (*Observation, error) {
	resp, data, err := c.do(ctx, http.MethodGet, "/v1/resources/"+id, requestID, nil)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: requestID,
			Body: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode == http.StatusOK {
		var out model.ActualResource
		if err := json.Unmarshal(data, &out); err != nil {
			return nil, &Error{Kind: KindTransient, Status: resp.StatusCode,
				RequestID: rid}
		}
		return &Observation{
			Resource:       &out,
			ServedStale:    resp.Header.Get("X-Served-Snapshot") == "true",
			CurrentVersion: parseVersion(resp.Header.Get("X-Current-Version")),
		}, nil
	}
	return nil, classifyRead(resp.StatusCode, rid, data)
}

// Observation wraps a read with stale-snapshot metadata.
type Observation struct {
	Resource       *model.ActualResource
	ServedStale    bool
	CurrentVersion int64
}

// UpdateRequest is a conditional update.
type UpdateRequest struct {
	RequestID   string         `json:"-"`
	ID          string         `json:"-"`
	ExpectedVer int64          `json:"-"`
	Generation  int64          `json:"generation"`
	SpecHash    string         `json:"specHash"`
	Spec        map[string]any `json:"spec"`
}

// Update performs a conditional PUT.
func (c *Client) Update(ctx context.Context, in UpdateRequest) (*model.ActualResource, error) {
	path := fmt.Sprintf("/v1/resources/%s?expectedVersion=%d", in.ID, in.ExpectedVer)
	resp, data, err := c.do(ctx, http.MethodPut, path, in.RequestID, in)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: in.RequestID,
			Body: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	switch {
	case resp.StatusCode == http.StatusOK:
		var out model.ActualResource
		if err := json.Unmarshal(data, &out); err != nil {
			return nil, &Error{Kind: KindTransient, Status: resp.StatusCode,
				RequestID: rid}
		}
		return &out, nil
	case resp.StatusCode == http.StatusPreconditionFailed,
		resp.StatusCode == http.StatusConflict:
		return nil, &Error{Kind: KindVersionConflict, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode == http.StatusInternalServerError &&
		resp.Header.Get("X-Fault") == "update-response-loss":
		return nil, &Error{Kind: KindResponseLost, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode >= 500:
		return nil, &Error{Kind: KindTransient, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	default:
		return nil, &Error{Kind: KindBadRequest, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	}
}

// Delete removes a physical resource. expectedVer <= 0 means unconditional.
func (c *Client) Delete(ctx context.Context, id string, expectedVer int64, requestID string) error {
	path := fmt.Sprintf("/v1/resources/%s", id)
	if expectedVer > 0 {
		path += fmt.Sprintf("?expectedVersion=%d", expectedVer)
	}
	resp, data, err := c.do(ctx, http.MethodDelete, path, requestID, nil)
	if err != nil {
		return &Error{Kind: KindTransient, RequestID: requestID,
			Body: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	switch {
	case resp.StatusCode == http.StatusNoContent:
		return nil
	case resp.StatusCode == http.StatusNotFound:
		return &Error{Kind: KindNotFound, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode == http.StatusPreconditionFailed,
		resp.StatusCode == http.StatusConflict:
		return &Error{Kind: KindVersionConflict, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode == http.StatusInternalServerError &&
		resp.Header.Get("X-Fault") == "delete-failed":
		return &Error{Kind: KindFaultInjected, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode == http.StatusInternalServerError &&
		resp.Header.Get("X-Fault") == "delete-response-loss":
		return &Error{Kind: KindResponseLost, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	case resp.StatusCode >= 500:
		return &Error{Kind: KindTransient, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	default:
		return &Error{Kind: KindBadRequest, Status: resp.StatusCode,
			RequestID: rid, Body: string(data)}
	}
}

func classifyRead(status int, rid string, data []byte) error {
	switch {
	case status == http.StatusNotFound:
		return &Error{Kind: KindNotFound, Status: status, RequestID: rid,
			Body: string(data)}
	case status == http.StatusConflict, status == http.StatusPreconditionFailed:
		return &Error{Kind: KindVersionConflict, Status: status,
			RequestID: rid, Body: string(data)}
	case status >= 500:
		return &Error{Kind: KindTransient, Status: status, RequestID: rid,
			Body: string(data)}
	default:
		return &Error{Kind: KindBadRequest, Status: status, RequestID: rid,
			Body: string(data)}
	}
}

// AsError extracts an *Error from a classified failure.
func AsError(err error) (*Error, bool) {
	var ae *Error
	if errors.As(err, &ae) {
		return ae, true
	}
	return nil, false
}

func parseVersion(v string) int64 {
	var n int64
	for _, ch := range v {
		if ch < '0' || ch > '9' {
			return 0
		}
		n = n*10 + int64(ch-'0')
	}
	return n
}
