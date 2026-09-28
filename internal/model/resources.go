// Package model defines the resource model shared by the scheduler,
// the reconciliation loop and the HTTP adapter.
//
// The model is intentionally small and explicit so that placement results
// can be verified by hand on the fixture clusters (see test/testdata).
package model

import (
	"errors"
	"fmt"
)

// Resources is a fixed-dimension resource vector. The three dimensions are
// millicpu (1000 == 1 vCPU), memory bytes and storage bytes.
// All dimensions are inclusive non-negative quantities.
type Resources struct {
	MilliCPU int64 `json:"milli_cpu"`
	Memory   int64 `json:"memory_bytes"`
	Storage  int64 `json:"storage_bytes"`
}

var (
	errNegativeResource = errors.New("resource quantities must be non-negative")
)

// Add returns r+x without modifying r.
func (r Resources) Add(x Resources) Resources {
	return Resources{
		MilliCPU: r.MilliCPU + x.MilliCPU,
		Memory:   r.Memory + x.Memory,
		Storage:  r.Storage + x.Storage,
	}
}

// Sub returns r-x without modifying r.
func (r Resources) Sub(x Resources) Resources {
	return Resources{
		MilliCPU: r.MilliCPU - x.MilliCPU,
		Memory:   r.Memory - x.Memory,
		Storage:  r.Storage - x.Storage,
	}
}

// Fits reports whether every dimension of r is >= need.
func (r Resources) Fits(need Resources) bool {
	return r.MilliCPU >= need.MilliCPU && r.Memory >= need.Memory && r.Storage >= need.Storage
}

// Validate rejects negative quantities.
func (r Resources) Validate() error {
	if r.MilliCPU < 0 || r.Memory < 0 || r.Storage < 0 {
		return errNegativeResource
	}
	return nil
}

// Missing returns the dimensions of need that do not fit in r as a
// human-readable list; it returns an empty slice when everything fits.
func (r Resources) Missing(need Resources) []string {
	var missing []string
	if r.MilliCPU < need.MilliCPU {
		missing = append(missing, fmt.Sprintf("milli_cpu(need=%d,free=%d)", need.MilliCPU, r.MilliCPU))
	}
	if r.Memory < need.Memory {
		missing = append(missing, fmt.Sprintf("memory(need=%d,free=%d)", need.Memory, r.Memory))
	}
	if r.Storage < need.Storage {
		missing = append(missing, fmt.Sprintf("storage(need=%d,free=%d)", need.Storage, r.Storage))
	}
	return missing
}
