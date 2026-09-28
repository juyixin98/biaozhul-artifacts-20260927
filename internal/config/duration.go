package config

import (
	"fmt"
	"time"
)

// durationType is an alias so signatures read naturally.
type durationType = time.Duration

func parseDur(s string) (time.Duration, error) {
	if s == "" {
		return 0, fmt.Errorf("empty duration")
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, err
	}
	if d <= 0 {
		return 0, fmt.Errorf("duration must be positive, got %s", s)
	}
	return d, nil
}
