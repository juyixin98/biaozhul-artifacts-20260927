// Package replay loads synthetic scenarios, runs them through the
// membership engine with a fresh injected clock, and packages the
// observable outcome: transitions, rejections, forwarding-table retention
// intervals and final group state.
package replay

import (
	"encoding/json"
	"fmt"

	"igmpq/internal/cats"
	"igmpq/internal/config"
	"igmpq/internal/engine"
	"igmpq/internal/model"
	"igmpq/internal/simclock"
)

// Scenario is one replayable input. Config is kept raw so that config
// errors surface as categorized failures rather than JSON decode errors.
type Scenario struct {
	Name     string          `json:"name"`
	Config   json.RawMessage `json:"config"`
	Events   []model.Event   `json:"events"`
	RunUntil *int64          `json:"run_until,omitempty"`
}

// Result is the full observable outcome of a replay.
type Result struct {
	RunID       int64                        `json:"run_id,omitempty"`
	Name        string                       `json:"name"`
	Config      config.Config                `json:"config"`
	Derived     config.Derived               `json:"derived"`
	Transitions []engine.Transition          `json:"transitions"`
	Rejections  []engine.Rejection           `json:"rejections"`
	Intervals   map[string][]engine.Interval `json:"intervals"`
	FinalGroups map[string]engine.GroupView  `json:"final_groups"`
}

// Run executes a scenario deterministically and returns its result.
// A non-nil error is always a *cats.Error with a stable category.
func Run(sc Scenario) (*Result, error) {
	cfg, err := config.Parse(sc.Config)
	if err != nil {
		return nil, err
	}
	if len(sc.Events) == 0 && sc.RunUntil == nil {
		return nil, cats.New(cats.InvalidScenario, "scenario has no events and no run_until")
	}

	clk := simclock.NewManual(0)
	eng := engine.New(cfg, clk)
	for _, ev := range sc.Events {
		eng.Apply(ev)
	}
	if sc.RunUntil != nil {
		if *sc.RunUntil < clk.Now() {
			return nil, cats.New(cats.InvalidScenario,
				fmt.Sprintf("run_until %d is before the last processed event time %d", *sc.RunUntil, clk.Now()))
		}
		eng.RunUntil(*sc.RunUntil)
	}

	return &Result{
		Name:        sc.Name,
		Config:      cfg,
		Derived:     eng.Derived(),
		Transitions: eng.Transitions(),
		Rejections:  eng.Rejections(),
		Intervals:   eng.Intervals(),
		FinalGroups: eng.FinalGroups(),
	}, nil
}

// LoadFile reads and decodes a scenario file.
func LoadFile(data []byte) (Scenario, error) {
	var sc Scenario
	if err := json.Unmarshal(data, &sc); err != nil {
		return Scenario{}, cats.New(cats.BadRequest, "scenario is not valid JSON: "+err.Error())
	}
	return sc, nil
}
