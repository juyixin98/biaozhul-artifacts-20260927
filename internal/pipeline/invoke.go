package pipeline

import (
	"context"
	"fmt"
	"time"

	"admission/internal/plugins"
)

// panicError carries a recovered panic value as an ordinary error so classify
// can label it compute_failure rather than crashing the admission process.
type panicError struct{ value any }

func (e *panicError) Error() string { return fmt.Sprintf("%v", e.value) }

func isPanic(err error) (string, bool) {
	var pe *panicError
	if asPanic(err, &pe) {
		return pe.Error(), true
	}
	return "", false
}

func asPanic(err error, target **panicError) bool {
	if pe, ok := err.(*panicError); ok {
		*target = pe
		return true
	}
	return false
}

// invokeWithTimeout runs fn under a per-plugin deadline and recovers panics.
// A timeout <= 0 means "no framework deadline" (the plugin still sees the
// request context).
func invokeWithTimeout[T any](parent context.Context, d time.Duration, fn func(context.Context) (T, error)) (T, error) {
	return invoke(parent, d, fn)
}

func invokeValidatorWithTimeout(parent context.Context, d time.Duration,
	fn func(context.Context) (plugins.Verdict, error)) (plugins.Verdict, error) {
	return invoke(parent, d, fn)
}

func invoke[T any](parent context.Context, d time.Duration, fn func(context.Context) (T, error)) (zero T, err error) {
	ctx := parent
	var cancel context.CancelFunc
	if d > 0 {
		ctx, cancel = context.WithTimeout(parent, d)
		defer cancel()
	}

	type result struct {
		v   T
		err error
	}
	ch := make(chan result, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				ch <- result{err: &panicError{value: r}}
			}
		}()
		v, e := fn(ctx)
		ch <- result{v: v, err: e}
	}()

	select {
	case <-ctx.Done():
		// Only call the plugin's deadline a timeout when the request context
		// itself is still alive; parent cancellation stays "canceled".
		if parent.Err() == nil && d > 0 {
			return zero, context.DeadlineExceeded
		}
		return zero, ctx.Err()
	case r := <-ch:
		return r.v, r.err
	}
}
