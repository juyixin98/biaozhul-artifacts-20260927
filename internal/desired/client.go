// Package desired is the controller-side HTTP adapter for the desired-state
// API. It models the 409/422 distinctions the reconcile loop reacts to and
// attaches the controller credential for privileged status/finalizer writes.
package desired

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"crcontroller/internal/model"
)

// ErrorKind classifies a desired-plane failure.
type ErrorKind string

const (
	KindNotFound     ErrorKind = "NotFound"
	KindConflict     ErrorKind = "Conflict"
	KindRefused      ErrorKind = "Refused"
	KindTerminating  ErrorKind = "Terminating"
	KindUnauthorized ErrorKind = "Unauthorized"
	KindTransient    ErrorKind = "Transient"
)

// Error is a classified adapter failure.
type Error struct {
	Kind      ErrorKind
	Status    int
	RequestID string
	Detail    string
}

func (e *Error) Error() string {
	return fmt.Sprintf("desired plane: %s (status=%d requestID=%s): %s",
		e.Kind, e.Status, e.RequestID, e.Detail)
}

// Client talks to the desired-state API as the controller.
type Client struct {
	BaseURL string
	Auth    string
	HTTP    *http.Client
}

// New builds a client.
func New(baseURL, auth string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Auth:    auth,
		HTTP:    &http.Client{Timeout: 5 * time.Second},
	}
}

// Object is the API wire shape (mirrors apiserver.resourceView).
type Object = model.Object

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
	if c.Auth != "" {
		req.Header.Set("X-Controller-Auth", c.Auth)
	}
	if requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp, data, nil
}

func decodeObject(data []byte) (*Object, error) {
	var o Object
	if err := json.Unmarshal(data, &o); err != nil {
		return nil, err
	}
	return &o, nil
}

func classify(status int, rid string, data []byte) error {
	var env struct {
		Detail string `json:"detail"`
		Reason string `json:"reason"`
		Error  string `json:"error"`
	}
	_ = json.Unmarshal(data, &env)
	detail := env.Detail
	if detail == "" {
		detail = env.Reason
	}
	if detail == "" {
		detail = env.Error
	}
	switch status {
	case http.StatusNotFound:
		return &Error{Kind: KindNotFound, Status: status, RequestID: rid, Detail: detail}
	case http.StatusConflict:
		if strings.Contains(detail, "terminating") {
			return &Error{Kind: KindTerminating, Status: status, RequestID: rid, Detail: detail}
		}
		return &Error{Kind: KindConflict, Status: status, RequestID: rid, Detail: detail}
	case http.StatusUnprocessableEntity:
		return &Error{Kind: KindRefused, Status: status, RequestID: rid, Detail: detail}
	case http.StatusForbidden, http.StatusUnauthorized:
		return &Error{Kind: KindUnauthorized, Status: status, RequestID: rid, Detail: detail}
	default:
		if status >= 500 {
			return &Error{Kind: KindTransient, Status: status, RequestID: rid, Detail: detail}
		}
		return &Error{Kind: KindTransient, Status: status, RequestID: rid,
			Detail: "unexpected status " + http.StatusText(status) + ": " + detail}
	}
}

// Get fetches one object by UID.
func (c *Client) Get(ctx context.Context, uid, requestID string) (*Object, error) {
	resp, data, err := c.do(ctx, http.MethodGet,
		"/api/v1/resources/"+url.PathEscape(uid), requestID, nil)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: requestID,
			Detail: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode == http.StatusOK {
		return decodeObject(data)
	}
	return nil, classify(resp.StatusCode, rid, data)
}

// List returns current objects (including terminating, so deletions complete).
func (c *Client) List(ctx context.Context, requestID string) ([]*Object, error) {
	resp, data, err := c.do(ctx, http.MethodGet,
		"/api/v1/resources?includeTerminating=true&limit=1000",
		requestID, nil)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: requestID,
			Detail: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode != http.StatusOK {
		return nil, classify(resp.StatusCode, rid, data)
	}
	var env struct {
		Items []*Object `json:"items"`
	}
	if err := json.Unmarshal(data, &env); err != nil {
		return nil, err
	}
	return env.Items, nil
}

// FinalizerAction patches finalizers with an explicit resourceVersion.
type FinalizerAction struct {
	RequestID       string
	Namespace       string
	Name            string
	Action          string // "add" | "remove"
	ResourceVersion int64
}

// PatchFinalizers performs the guarded finalizer patch.
func (c *Client) PatchFinalizers(ctx context.Context, a FinalizerAction) (*Object, error) {
	body := map[string]any{
		"action":          a.Action,
		"finalizer":       model.FinalizerController,
		"resourceVersion": a.ResourceVersion,
	}
	path := fmt.Sprintf("/api/v1/namespaces/%s/resources/%s/finalizers",
		url.PathEscape(a.Namespace), url.PathEscape(a.Name))
	resp, data, err := c.do(ctx, http.MethodPatch, path, a.RequestID, body)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: a.RequestID,
			Detail: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode == http.StatusOK {
		return decodeObject(data)
	}
	return nil, classify(resp.StatusCode, rid, data)
}

// StatusWrite is a guarded status subresource write.
type StatusWrite struct {
	RequestID          string
	Namespace          string
	Name               string
	ResourceVersion    int64
	ObservedGeneration int64
	ExternalID         string
	State              string
	Conditions         []model.Condition
}

// PutStatus writes the status subresource.
func (c *Client) PutStatus(ctx context.Context, w StatusWrite) (*Object, error) {
	body := map[string]any{
		"resourceVersion":    w.ResourceVersion,
		"observedGeneration": w.ObservedGeneration,
		"externalID":         w.ExternalID,
		"state":              w.State,
		"conditions":         w.Conditions,
	}
	path := fmt.Sprintf("/api/v1/namespaces/%s/resources/%s/status",
		url.PathEscape(w.Namespace), url.PathEscape(w.Name))
	resp, data, err := c.do(ctx, http.MethodPut, path, w.RequestID, body)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: w.RequestID,
			Detail: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode == http.StatusOK {
		return decodeObject(data)
	}
	return nil, classify(resp.StatusCode, rid, data)
}

// Delete requests deletion of an object by UID (marks terminating, or 404
// once purged).
func (c *Client) Delete(ctx context.Context, uid, requestID string) (*Object, error) {
	resp, data, err := c.do(ctx, http.MethodDelete,
		"/api/v1/resources/"+url.PathEscape(uid), requestID, nil)
	if err != nil {
		return nil, &Error{Kind: KindTransient, RequestID: requestID,
			Detail: err.Error()}
	}
	rid := resp.Header.Get("X-Request-ID")
	if resp.StatusCode == http.StatusOK {
		return decodeObject(data)
	}
	return nil, classify(resp.StatusCode, rid, data)
}

// Event nudge payload.
type Event struct {
	UID       string `json:"uid"`
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Type      string `json:"type"`
}

// PostEvent sends a best-effort reconcile trigger.
func (c *Client) PostEvent(ctx context.Context, requestID string, ev Event) error {
	resp, data, err := c.do(ctx, http.MethodPost, "/api/v1/events", requestID, ev)
	if err != nil {
		return &Error{Kind: KindTransient, RequestID: requestID,
			Detail: err.Error()}
	}
	if resp.StatusCode == http.StatusAccepted {
		return nil
	}
	return classify(resp.StatusCode, resp.Header.Get("X-Request-ID"), data)
}

// AsError extracts a classified desired-plane error.
func AsError(err error) (*Error, bool) {
	var de *Error
	if errors.As(err, &de) {
		return de, true
	}
	return nil, false
}
