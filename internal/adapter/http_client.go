// Package adapter implements reconcile.ExternalClient over HTTP against the
// actual resource service. It owns transport concerns only: retries are NOT
// done here for state-changing calls (the reconcile loop decides ambiguity
// policy), response status codes are normalized into reconcile.CallError
// categories, and request IDs are propagated.
package adapter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	"resourcecontroller/internal/diag"
	"resourcecontroller/internal/reconcile"
)

// HTTPClient talks to the fakecloud-style resource service.
type HTTPClient struct {
	BaseURL string
	HTTP    *http.Client
	Log     *diag.Logger
}

// New returns an HTTPClient with sensible local timeouts.
func New(baseURL string, log *diag.Logger) *HTTPClient {
	return &HTTPClient{
		BaseURL: baseURL,
		HTTP: &http.Client{
			Timeout: 5 * time.Second,
			// Do not silently follow redirects for mutating calls.
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		Log: log,
	}
}

type wireSpec struct {
	Name        string `json:"name"`
	Replicas    int    `json:"replicas"`
	Color       string `json:"color"`
	SecretToken string `json:"secretToken,omitempty"`
}

type wireResource struct {
	ID     string   `json:"id"`
	Name   string   `json:"name"`
	Spec   wireSpec `json:"spec"`
	SpecFP string   `json:"specFingerprint"`
	// The service emits version as a number; tolerate quoted strings too.
	Version json.Number `json:"version"`
}

func (w wireResource) toExternal() (*reconcile.ExternalResource, error) {
	v, err := w.Version.Int64()
	if err != nil {
		return nil, fmt.Errorf("external resource has non-integer version %q", w.Version.String())
	}
	return &reconcile.ExternalResource{
		ID:      w.ID,
		Name:    w.Name,
		Version: v,
		SpecFP:  w.SpecFP,
		Spec: reconcile.ExternalSpec{
			Name:        w.Spec.Name,
			Replicas:    w.Spec.Replicas,
			Color:       w.Spec.Color,
			SecretToken: w.Spec.SecretToken,
		},
	}, nil
}

// Observe performs GET /v1/widgets/{id}.
func (c *HTTPClient) Observe(ctx context.Context, id string) (*reconcile.ExternalResource, error) {
	var out wireResource
	status, body, err := c.do(ctx, http.MethodGet, "/v1/widgets/"+id, nil, nil)
	if err != nil {
		return nil, err
	}
	switch status {
	case http.StatusOK:
		if err := decodeBody(body, &out); err != nil {
			return nil, callErr("get", reconcile.CategoryUnknown, "MalformedResponse", err.Error())
		}
		return out.toExternal()
	case http.StatusNotFound:
		return nil, callErr("get", reconcile.CategoryNotFound, "NotFound", "external resource absent")
	case http.StatusConflict:
		return nil, callErr("get", reconcile.CategoryConflict, "Conflict", errorMessage(body))
	case http.StatusRequestTimeout, http.StatusTooManyRequests,
		http.StatusInternalServerError, http.StatusBadGateway,
		http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return nil, callErr("get", reconcile.CategoryTransient, "HTTP"+strconv.Itoa(status), errorMessage(body))
	default:
		return nil, callErr("get", reconcile.CategoryRejected, "HTTP"+strconv.Itoa(status), errorMessage(body))
	}
}

// Create performs PUT /v1/widgets/{id} with an idempotency key.
// upsertBody matches the actual service's PUT envelope.
type upsertBody struct {
	Name string   `json:"name"`
	Spec wireSpec `json:"spec"`
}

func (c *HTTPClient) Create(ctx context.Context, id, idempotencyKey string, spec reconcile.ExternalSpec) (*reconcile.ExternalResource, error) {
	body := upsertBody{Name: spec.Name, Spec: wireSpec{
		Replicas: spec.Replicas, Color: spec.Color, SecretToken: spec.SecretToken,
	}}
	headers := http.Header{"Idempotency-Key": []string{idempotencyKey}}
	status, respBody, err := c.do(ctx, http.MethodPut, "/v1/widgets/"+id, headers, body)
	if err != nil {
		return nil, err
	}
	switch {
	case status == http.StatusCreated || status == http.StatusOK:
		var out wireResource
		if err := decodeBody(respBody, &out); err != nil {
			return nil, callErr("create", reconcile.CategoryUnknown, "MalformedResponse", err.Error())
		}
		return out.toExternal()
	case status == http.StatusConflict:
		return nil, callErr("create", reconcile.CategoryConflict, "Conflict", errorMessage(respBody))
	case status == http.StatusRequestTimeout || status >= http.StatusInternalServerError:
		// 5xx after a mutating call: the commit state is unknown. The caller
		// must observe/claim, never blindly retry.
		return nil, callErr("create", reconcile.CategoryAmbiguous, "HTTP"+strconv.Itoa(status),
			"create outcome unknown: "+errorMessage(respBody))
	case status >= 400:
		return nil, callErr("create", reconcile.CategoryRejected, "HTTP"+strconv.Itoa(status), errorMessage(respBody))
	default:
		return nil, callErr("create", reconcile.CategoryUnknown, "HTTP"+strconv.Itoa(status), errorMessage(respBody))
	}
}

// Update performs PUT /v1/widgets/{id} with an If-Match version guard.
func (c *HTTPClient) Update(ctx context.Context, id string, expectedVersion int64, spec reconcile.ExternalSpec) (*reconcile.ExternalResource, error) {
	body := upsertBody{Name: spec.Name, Spec: wireSpec{
		Replicas: spec.Replicas, Color: spec.Color, SecretToken: spec.SecretToken,
	}}
	headers := http.Header{"If-Match": []string{strconv.FormatInt(expectedVersion, 10)}}
	status, respBody, err := c.do(ctx, http.MethodPut, "/v1/widgets/"+id, headers, body)
	if err != nil {
		return nil, err
	}
	switch {
	case status == http.StatusOK:
		var out wireResource
		if err := decodeBody(respBody, &out); err != nil {
			return nil, callErr("update", reconcile.CategoryUnknown, "MalformedResponse", err.Error())
		}
		return out.toExternal()
	case status == http.StatusConflict:
		return nil, callErr("update", reconcile.CategoryConflict, "VersionConflict", errorMessage(respBody))
	case status == http.StatusRequestTimeout || status >= http.StatusInternalServerError:
		return nil, callErr("update", reconcile.CategoryAmbiguous, "HTTP"+strconv.Itoa(status),
			"update outcome unknown: "+errorMessage(respBody))
	case status >= 400:
		return nil, callErr("update", reconcile.CategoryRejected, "HTTP"+strconv.Itoa(status), errorMessage(respBody))
	default:
		return nil, callErr("update", reconcile.CategoryUnknown, "HTTP"+strconv.Itoa(status), errorMessage(respBody))
	}
}

// Delete performs DELETE /v1/widgets/{id}. The service treats deleting a
// missing object as success.
func (c *HTTPClient) Delete(ctx context.Context, id string) error {
	status, body, err := c.do(ctx, http.MethodDelete, "/v1/widgets/"+id, nil, nil)
	if err != nil {
		return err
	}
	switch {
	case status == http.StatusNoContent || status == http.StatusOK:
		return nil
	case status == http.StatusNotFound:
		return nil
	case status == http.StatusConflict:
		return callErr("delete", reconcile.CategoryConflict, "Conflict", errorMessage(body))
	case status == http.StatusRequestTimeout || status >= http.StatusInternalServerError:
		return callErr("delete", reconcile.CategoryAmbiguous, "HTTP"+strconv.Itoa(status),
			"delete outcome unknown: "+errorMessage(body))
	case status >= 400:
		return callErr("delete", reconcile.CategoryRejected, "HTTP"+strconv.Itoa(status), errorMessage(body))
	default:
		return callErr("delete", reconcile.CategoryUnknown, "HTTP"+strconv.Itoa(status), errorMessage(body))
	}
}

func (c *HTTPClient) do(ctx context.Context, method, path string, headers http.Header, payload any) (int, []byte, error) {
	var bodyReader io.Reader
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			return 0, nil, callErr(method, reconcile.CategoryUnknown, "EncodeFailed", err.Error())
		}
		bodyReader = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, bodyReader)
	if err != nil {
		return 0, nil, callErr(method, reconcile.CategoryUnknown, "BuildRequestFailed", err.Error())
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rid := diag.RequestIDFromContext(ctx)
	if rid != "-" {
		req.Header.Set(diag.RequestIDHeader, rid)
	}
	for k, vs := range headers {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		// Transport error (connection refused, reset, client timeout): the
		// server-side outcome is unknown.
		cat := reconcile.CategoryTransient
		if method != http.MethodGet && method != http.MethodHead {
			cat = reconcile.CategoryAmbiguous
		}
		return 0, nil, callErr(method, cat, "TransportError", err.Error())
	}
	defer resp.Body.Close()
	// Limit error/response bodies so a misbehaving peer cannot exhaust memory.
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return resp.StatusCode, nil, callErr(method, reconcile.CategoryTransient, "ReadBodyFailed", err.Error())
	}
	return resp.StatusCode, b, nil
}

func callErr(attempt, category, code, msg string) *reconcile.CallError {
	return &reconcile.CallError{Attempt: attempt, Category: category, Code: code, Message: msg}
}

func decodeBody(b []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(b))
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

// errorMessage extracts the synthetic service's error message without
// leaking it verbatim into logs at this layer; callers decide what to record.
func errorMessage(b []byte) string {
	var env struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(b, &env); err == nil && env.Error.Message != "" {
		return env.Error.Message
	}
	return string(bytes.TrimSpace(b))
}
