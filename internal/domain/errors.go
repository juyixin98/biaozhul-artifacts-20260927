package domain

import "fmt"

// ErrorKind is a stable machine-readable failure category. The HTTP adapter
// maps these kinds to status codes, and the independent tests assert on them.
type ErrorKind string

// Structural validation failure categories.
const (
	ErrNamespaceNameless        ErrorKind = "namespace_nameless"
	ErrNamespaceDuplicate       ErrorKind = "namespace_duplicate"
	ErrLabelInvalid             ErrorKind = "label_invalid"
	ErrEndpointNoUID            ErrorKind = "endpoint_no_uid"
	ErrEndpointDuplicateUID     ErrorKind = "endpoint_duplicate_uid"
	ErrEndpointNoNamespace      ErrorKind = "endpoint_no_namespace"
	ErrEndpointUnknownNamespace ErrorKind = "endpoint_unknown_namespace"
	ErrPortNumberInvalid        ErrorKind = "port_number_invalid"
	ErrPortProtocolInvalid      ErrorKind = "port_protocol_invalid"
	ErrPortNameDuplicate        ErrorKind = "port_name_duplicate"
	ErrPolicyNameless           ErrorKind = "policy_nameless"
	ErrPolicyDuplicate          ErrorKind = "policy_duplicate"
	ErrPolicyNoNamespace        ErrorKind = "policy_no_namespace"
	ErrPolicyUnknownNamespace   ErrorKind = "policy_unknown_namespace"
	ErrPolicyTypeInvalid        ErrorKind = "policy_type_invalid"
	ErrSelectorInvalid          ErrorKind = "selector_invalid"
	ErrRulePortInvalid          ErrorKind = "rule_port_invalid"
)

// Fetch/adaptation failure categories produced outside domain validation.
const (
	ErrSourceNotFound ErrorKind = "source_not_found"
	ErrSourceSyntax   ErrorKind = "source_syntax"
)

// ValidationError carries a stable category plus references for diagnostics.
type ValidationError struct {
	Kind   ErrorKind
	Name   string
	Detail string
}

func (e ValidationError) Error() string {
	msg := string(e.Kind)
	if e.Name != "" {
		msg += " at " + e.Name
	}
	if e.Detail != "" {
		msg += ": " + e.Detail
	}
	return msg
}

// AsValidationError extracts the category from any validation error.
func AsValidationError(err error) (ValidationError, bool) {
	if err == nil {
		return ValidationError{}, false
	}
	var ve ValidationError
	if e, ok := err.(ValidationError); ok {
		return e, true
	}
	return ve, false
}

var _ error = (*ValidationError)(nil)
var _ = fmt.Sprintf
