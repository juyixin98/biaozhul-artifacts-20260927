package broker

import "localbroker/internal/kernel"

// InvalidArgument is re-exported from the kernel so callers of the service
// layer can match validation errors without depending on the kernel directly.
type InvalidArgument = kernel.InvalidArgument
