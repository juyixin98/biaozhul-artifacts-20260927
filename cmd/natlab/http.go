package main

import (
	"net/http"
	"time"
)

// newHTTPServer builds the stdlib server. Timeouts are defensive only; the
// listener is a local replay surface.
func newHTTPServer(addr string, h http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           h,
		ReadHeaderTimeout: 5 * time.Second,
	}
}
