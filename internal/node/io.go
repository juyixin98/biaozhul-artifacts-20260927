package node

import (
	"errors"
	"io"
)

// maxBodyBytes bounds every request/response body so a huge payload is a
// resource_exhausted failure instead of silent memory growth.
const maxBodyBytes = 1 << 20 // 1 MiB

var errBodyTooLarge = errors.New("http: request body too large")

type limitedReader struct {
	r io.Reader
	n int64
}

func (lr *limitedReader) Read(p []byte) (int, error) {
	if lr.n <= 0 {
		return 0, errBodyTooLarge
	}
	if int64(len(p)) > lr.n {
		p = p[:lr.n]
	}
	n, err := lr.r.Read(p)
	lr.n -= int64(n)
	if err == io.EOF && lr.n == 0 {
		err = nil
	}
	return n, err
}

func ioLimitReader(r io.Reader) io.Reader { return &limitedReader{r: r, n: maxBodyBytes} }
