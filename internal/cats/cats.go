// Package cats defines the shared, stable set of failure categories used
// across the service. Every rejection, validation failure and API error
// carries one of these categories so that operators and tests can assert
// on the *kind* of failure instead of matching message text.
package cats

// Failure categories. Keep this list small and stable; it is part of the
// service's external contract (HTTP responses, replay results, logs).
const (
	// Configuration / scenario problems.
	InvalidConfig   = "invalid_config"   // config file failed validation
	InvalidScenario = "invalid_scenario" // scenario is structurally inconsistent

	// Event validation problems (per-event rejections during replay).
	BadEvent         = "bad_event"         // malformed event (bad type, negative time, ...)
	OutOfOrder       = "out_of_order"      // event time goes backwards
	UnknownInterface = "unknown_interface" // interface not declared in config
	InvalidGroup     = "invalid_group"     // group address not a valid multicast address
	InvalidMember    = "invalid_member"    // member address not a valid IP
	UnknownMember    = "unknown_member"    // leave for a member that never joined
	UnknownGroup     = "unknown_group"     // group-scoped event for a group with no state
	FutureGeneration = "future_generation" // report claims to answer a query round not yet issued

	// Storage / API problems.
	RunNotFound = "run_not_found" // requested replay run id does not exist
	BadRequest  = "bad_request"   // HTTP body could not be decoded
	Internal    = "internal"      // unexpected server-side failure
)

// Error is a categorized error. Category is machine-readable; Message is
// for humans and may contain (redacted) context.
type Error struct {
	Category string `json:"category"`
	Message  string `json:"message"`
}

func (e *Error) Error() string { return e.Category + ": " + e.Message }

// New builds a categorized error.
func New(category, message string) *Error {
	return &Error{Category: category, Message: message}
}

// CategoryOf extracts the category of an error, defaulting to Internal.
func CategoryOf(err error) string {
	if ce, ok := err.(*Error); ok {
		return ce.Category
	}
	return Internal
}
