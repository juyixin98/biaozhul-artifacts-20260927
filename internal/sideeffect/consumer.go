// Package sideeffect is a local synthetic participant: a consumer that
// performs an external-looking side effect per delivery and records how many
// times it actually ran.
//
// The broker provides at-least-once delivery only; making side effects
// idempotent is the CONSUMER's responsibility. This type demonstrates exactly
// that boundary by keying applied effects on the stable message id (not on the
// receipt, which changes on every redelivery).
package sideeffect

import (
	"fmt"
	"sync"
)

// Effect is the thing the consumer "does" for a message. Kept as data so tests
// can assert exact contents; nothing here talks to a real external system.
type Effect struct {
	MessageID string
	Payload   string
}

// Consumer applies effects. When Dedup=true (the correct consumer posture),
// an effect for a given stable message id runs exactly once even if the broker
// redelivers it under a fresh receipt. With Dedup=false the naive consumer
// re-runs the effect on each delivery, which demonstrates why dedup is the
// consumer's job.
type Consumer struct {
	mu       sync.Mutex
	Dedup    bool
	applied  map[string]Effect
	order    []string
	Runs     int // number of times the handler body actually executed
	Replays  int // number of deliveries seen for an already-applied id
}

// NewConsumer returns a consumer; dedup toggles idempotent protection.
func NewConsumer(dedup bool) *Consumer {
	return &Consumer{Dedup: dedup, applied: map[string]Effect{}}
}

// Handle models processing one delivery. It returns "committed=true" when the
// side effect ran (or had already run) and the caller should ack.
func (c *Consumer) Handle(messageID, payload string) (ran bool, committed bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.applied[messageID]; ok {
		c.Replays++
		// Correct behaviour: recognize our own prior effect by the stable id
		// and ack the redelivery without re-executing.
		return false, true
	}
	if !c.Dedup {
		// Naive posture: no idempotency key, run blindly on every delivery.
		c.Runs++
		c.order = append(c.order, messageID)
		c.applied[messageID] = Effect{MessageID: messageID, Payload: payload}
		return true, true
	}
	c.Runs++
	c.order = append(c.order, messageID)
	c.applied[messageID] = Effect{MessageID: messageID, Payload: payload}
	return true, true
}

// AppliedCount reports how many distinct effects have been applied.
func (c *Consumer) AppliedCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.applied)
}

// RunCount reports total handler-body executions.
func (c *Consumer) RunCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.Runs
}

// EffectOf returns the recorded effect for an id.
func (c *Consumer) EffectOf(id string) (Effect, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.applied[id]
	if !ok {
		return Effect{}, fmt.Errorf("no effect recorded for %s", id)
	}
	return e, nil
}
