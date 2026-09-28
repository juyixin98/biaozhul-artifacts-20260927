// Package server is the public entry point to the netsem analysis service.
// External consumers (including the independent test module) drive the
// service exclusively through its HTTP surface, never through internals.
package server

import (
	"net/http"

	"netsem/internal/httpapi"
)

// Server is a running service instance.
type Server struct {
	app *httpapi.App
}

// New creates a service backed by the SQLite database at dbPath.
// instance is recorded in every correlated log row.
func New(dbPath, instance string) (*Server, func() error, error) {
	app, cleanup, err := httpapi.New(dbPath, instance)
	if err != nil {
		return nil, nil, err
	}
	return &Server{app: app}, cleanup, nil
}

// Handler returns the wired HTTP handler.
func (s *Server) Handler() http.Handler { return s.app.Handler() }

// Instance returns the instance label.
func (s *Server) Instance() string { return s.app.Instance() }
