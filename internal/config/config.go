// Package config loads JSON configuration for the three executables. It uses
// the standard library only and validates required fields.
package config

import (
	"encoding/json"
	"fmt"
	"os"
)

// APIServer configures the desired-state API.
type APIServer struct {
	Listen           string `json:"listen"`
	DatabasePath     string `json:"databasePath"`
	ControllerAuth   string `json:"controllerAuth"`
	DisableEventSink bool   `json:"disableEventSink"`
}

// ActualServer configures the physical-resource service.
type ActualServer struct {
	Listen       string `json:"listen"`
	DatabasePath string `json:"databasePath"`
}

// Controller configures the reconciler.
type Controller struct {
	DesiredURL      string `json:"desiredURL"`
	ActualURL       string `json:"actualURL"`
	ControllerAuth  string `json:"controllerAuth"`
	DatabasePath    string `json:"databasePath"`
	DiagnosticsAddr string `json:"diagnosticsAddr"`
	ResyncInterval  string `json:"resyncInterval"`
	BackoffBaseMS   int    `json:"backoffBaseMS"`
	BackoffMaxMS    int    `json:"backoffMaxMS"`
}

// Load reads and parses a JSON config file into v.
func Load(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("parse config %s: %w", path, err)
	}
	return nil
}

// ValidateAPIServer checks the API server config.
func ValidateAPIServer(c *APIServer) error {
	if c.Listen == "" {
		return fmt.Errorf("listen is required")
	}
	if c.DatabasePath == "" {
		return fmt.Errorf("databasePath is required")
	}
	return nil
}

// ValidateActualServer checks the actual service config.
func ValidateActualServer(c *ActualServer) error {
	if c.Listen == "" {
		return fmt.Errorf("listen is required")
	}
	if c.DatabasePath == "" {
		return fmt.Errorf("databasePath is required")
	}
	return nil
}

// ValidateController checks the controller config.
func ValidateController(c *Controller) error {
	if c.DesiredURL == "" {
		return fmt.Errorf("desiredURL is required")
	}
	if c.ActualURL == "" {
		return fmt.Errorf("actualURL is required")
	}
	if c.DatabasePath == "" {
		return fmt.Errorf("databasePath is required")
	}
	return nil
}
