package protocol

import "fmt"

// ErrorCode is the machine-readable failure category shared by the
// coordinator, HTTP layer and tests. The README documents the full semantics.
type ErrorCode string

const (
	// ErrUnknownGroup: the group in the request path does not exist.
	ErrUnknownGroup ErrorCode = "UNKNOWN_GROUP"
	// ErrUnknownMember: the member in the request path is not a group member.
	ErrUnknownMember ErrorCode = "UNKNOWN_MEMBER"
	// ErrMemberExists: a join reused a member ID already in the group.
	ErrMemberExists ErrorCode = "MEMBER_EXISTS"
	// ErrStaleGeneration: request generation is behind the current
	// generation (fenced). The request had no effect.
	ErrStaleGeneration ErrorCode = "STALE_GENERATION"
	// ErrFutureGeneration: request generation is ahead of the coordinator.
	ErrFutureGeneration ErrorCode = "FUTURE_GENERATION"
	// ErrNotOwner: the member does not own the partition in the generation
	// named by the request.
	ErrNotOwner ErrorCode = "NOT_OWNER"
	// ErrRevokedPartition: the member used to own the partition in that
	// generation, but revocation was already issued: commits are refused so
	// a slow member cannot write after hand-off.
	ErrRevokedPartition ErrorCode = "REVOKED_PARTITION"
	// ErrOffsetRegression: the committed offset is below the stored one.
	ErrOffsetRegression ErrorCode = "OFFSET_REGRESSION"
	// ErrUnknownPartition: the partition is not part of the group's topic
	// universe.
	ErrUnknownPartition ErrorCode = "UNKNOWN_PARTITION"
	// ErrBadRequest: malformed input (empty member id, negative offsets…).
	ErrBadRequest ErrorCode = "BAD_REQUEST"
	// ErrRebalanceInProgress: the group is mid-rebalance and the requested
	// operation only succeeds in a stable generation.
	ErrRebalanceInProgress ErrorCode = "REBALANCE_IN_PROGRESS"
	// ErrPartitionQuarantined: the partition has no effective owner because
	// its revocation was never confirmed; outcome is uncertain.
	ErrPartitionQuarantined ErrorCode = "PARTITION_QUARANTINED"
	// ErrLeavingMember: the member is leaving; the operation is refused.
	ErrLeavingMember ErrorCode = "LEAVING_MEMBER"
	// ErrStorage: storage backend failure; the operation may not have been
	// applied.
	ErrStorage ErrorCode = "STORAGE"
)

// Error is a typed coordinator error carrying a code and a human message.
type Error struct {
	Code    ErrorCode
	Message string
}

func (e *Error) Error() string { return string(e.Code) + ": " + e.Message }

// NewError builds a typed error.
func NewError(code ErrorCode, format string, args ...any) *Error {
	return &Error{Code: code, Message: fmt.Sprintf(format, args...)}
}
