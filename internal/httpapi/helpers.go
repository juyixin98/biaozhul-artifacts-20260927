package httpapi

import (
	"errors"
	"io"
)

// readAllLimit reads at most max+1 bytes and errors when the body exceeds the
// limit, preventing an oversized request from exhausting memory.
func readAllLimit(r io.Reader, max int64) ([]byte, error) {
	lr := io.LimitReader(r, max+1)
	b, err := io.ReadAll(lr)
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > max {
		return nil, errors.New("request body too large (limit 1 MiB)")
	}
	return b, nil
}
