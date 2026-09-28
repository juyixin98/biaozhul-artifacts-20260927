package nat

// ComputeError wraps storage/internal failures. The HTTP boundary maps it to
// 500 with category compute_failure; model-level rejections never use it.
type ComputeError struct{ Err error }

func (c *ComputeError) Error() string { return "STORE_ERROR: " + c.Err.Error() }
func (c *ComputeError) Unwrap() error { return c.Err }

func computeErr(err error) error { return &ComputeError{Err: err} }
