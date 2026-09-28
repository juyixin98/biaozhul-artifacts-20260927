package engine

import (
	"pvsim/config"
)

// Collector is the in-memory Sink used by replay: it captures every
// intermediate artifact of one run for persistence and for the returned
// Result. Traces are recorded in delivery order.
type Collector struct {
	Events    []ExternalDelivery
	Decisions []Decision
	Traces    []Trace
}

// ExternalDelivery records one injected synthetic event at its delivery
// version, together with its original seq for replay correlation.
type ExternalDelivery struct {
	Version int
	Seq     int
	Router  string
	Peer    string
	Kind    string
	Prefix  string
}

// NewCollector returns an empty collector.
func NewCollector() *Collector { return &Collector{} }

// ExternalDelivered implements Sink.
func (c *Collector) ExternalDelivered(version int, ev *config.Event) {
	c.Events = append(c.Events, ExternalDelivery{
		Version: version, Seq: ev.Seq, Router: ev.Router, Peer: ev.Peer,
		Kind: ev.Kind, Prefix: ev.Prefix,
	})
}

// Decision implements Sink.
func (c *Collector) Decision(d Decision) { c.Decisions = append(c.Decisions, d) }

// Trace implements Sink.
func (c *Collector) Trace(t Trace) { c.Traces = append(c.Traces, t) }

// Attach returns the collector itself for engine.Run sink arguments.
func (c *Collector) Attach() Sink { return c }
