// Package server exposes the coordinator over a net/http JSON API. It owns
// no coordination logic: every handler translates one HTTP request into one
// coordinator call. All responses carry the request id so an API result and
// its log lines can be correlated.
package server

import (
	"encoding/json"
	"net/http"

	"example.com/cgcoord/protocol"
)

// errorBody is the uniform error envelope.
type errorBody struct {
	Error errorPayload `json:"error"`
}

type errorPayload struct {
	Code      protocol.ErrorCode `json:"code"`
	Message   string             `json:"message"`
	RequestID string             `json:"request_id"`
}

func statusFor(code protocol.ErrorCode) int {
	switch code {
	case protocol.ErrUnknownGroup, protocol.ErrUnknownMember, protocol.ErrUnknownPartition:
		return http.StatusNotFound
	case protocol.ErrMemberExists:
		return http.StatusConflict
	case protocol.ErrStaleGeneration, protocol.ErrFutureGeneration,
		protocol.ErrNotOwner, protocol.ErrRevokedPartition, protocol.ErrOffsetRegression,
		protocol.ErrLeavingMember:
		return http.StatusConflict
	case protocol.ErrRebalanceInProgress, protocol.ErrPartitionQuarantined:
		return http.StatusConflict
	case protocol.ErrBadRequest:
		return http.StatusBadRequest
	case protocol.ErrStorage:
		return http.StatusInternalServerError
	default:
		return http.StatusInternalServerError
	}
}

func writeError(w http.ResponseWriter, r *http.Request, requestID string, err error) {
	code := protocol.ErrStorage
	msg := err.Error()
	var pe *protocol.Error
	if asError(err, &pe) {
		code = pe.Code
		msg = pe.Message
	}
	writeJSON(w, r, statusFor(code), errorBody{Error: errorPayload{
		Code: code, Message: msg, RequestID: requestID,
	}})
}

func asError(err error, target **protocol.Error) bool {
	for err != nil {
		if e, ok := err.(*protocol.Error); ok {
			*target = e
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

func writeJSON(w http.ResponseWriter, r *http.Request, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeJSON(w, r, http.StatusBadRequest, errorBody{Error: errorPayload{
			Code: protocol.ErrBadRequest, Message: "invalid JSON body: " + err.Error(),
		}})
		return false
	}
	return true
}
