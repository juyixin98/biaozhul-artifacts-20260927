package router

// RejectError is a router-level rejection with a stable, machine-readable
// category (same taxonomy shape as topic.ProtocolError).
type RejectError struct {
	Class  string
	Detail string
}

func (e *RejectError) Error() string {
	if e.Detail == "" {
		return e.Class
	}
	return e.Class + ": " + e.Detail
}
