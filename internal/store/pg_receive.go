package store

import (
	"context"
	"database/sql"
	"time"

	"workbroker/internal/kernel"
	"workbroker/internal/protocol"
)

func (p *Postgres) Enqueue(ctx context.Context, in *EnqueueInput) (*protocol.Message, protocol.Event, error) {
	res := kernel.EnqueueOne(*in, p.clk.Now())
	err := withTx(ctx, p.db, func(tx *sql.Tx) error {
		if err := insertMessageTx(ctx, tx, res.Message); err != nil {
			return err
		}
		ev := res.Event
		return insertEventTx(ctx, tx, &ev)
	})
	if err != nil {
		return nil, protocol.Event{}, mapErr("enqueue", err)
	}
	return res.Message, res.Event, nil
}

// Receive reaps expired inflight messages and delivers visible messages in a
// single transaction, locking every candidate row FOR UPDATE and processing
// it in partition FIFO order. Concurrent receivers serialize on those row
// locks; each message is delivered exactly once per visibility window.
func (p *Postgres) Receive(ctx context.Context, in ReceiveInput) ([]Received, error) {
	max := in.MaxMessages
	if max <= 0 {
		max = 1
	}
	now := p.clk.Now()
	var out []Received

	err := withTx(ctx, p.db, func(tx *sql.Tx) error {
		// Phase 1: reap every expired inflight message in this partition.
		// SKIP LOCKED lets other rows proceed; locked ones are being handled
		// by the competing transaction under the same rules.
		expiredRows, err := tx.QueryContext(ctx, `
			SELECT `+messageColumns+` FROM messages
			WHERE partition_key=$1 AND status='inflight' AND visibility_deadline <= $2
			ORDER BY visibility_deadline, id
			FOR UPDATE SKIP LOCKED`, in.Partition, now)
		if err != nil {
			return err
		}
		type exp struct{ m *protocol.Message }
		var expired []exp
		for expiredRows.Next() {
			m, err := scanMessage(expiredRows)
			if err != nil {
				expiredRows.Close()
				return err
			}
			expired = append(expired, exp{m})
		}
		expiredRows.Close()
		if err := expiredRows.Err(); err != nil {
			return err
		}
		for _, e := range expired {
			if err := reapLockedTx(ctx, tx, e.m, now); err != nil {
				return err
			}
		}

		// Phase 2: deliver due visible messages, FIFO, up to max.
		availRows, err := tx.QueryContext(ctx, `
			SELECT `+messageColumns+` FROM messages
			WHERE partition_key=$1 AND status='available' AND available_at <= $2
			ORDER BY created_at, id
			LIMIT $3
			FOR UPDATE SKIP LOCKED`,
			in.Partition, now, max)
		if err != nil {
			return err
		}
		var due []*protocol.Message
		for availRows.Next() {
			m, err := scanMessage(availRows)
			if err != nil {
				availRows.Close()
				return err
			}
			due = append(due, m)
		}
		availRows.Close()
		if err := availRows.Err(); err != nil {
			return err
		}
		for _, m := range due {
			res := kernel.ReceiveOne(m, in.WorkerID, in.Visibility, now)
			if err := updateMessageTx(ctx, tx, res.Message); err != nil {
				return err
			}
			if err := insertReceiptTx(ctx, tx, res.Receipt); err != nil {
				return err
			}
			ev := res.Event
			if err := insertEventTx(ctx, tx, &ev); err != nil {
				return err
			}
			out = append(out, Received{Message: res.Message, Receipt: res.Receipt})
		}
		return nil
	})
	if err != nil {
		return nil, mapErr("receive", err)
	}
	return out, nil
}

// reapLockedTx persists one kernel.ReapOne for a locked expired inflight row.
func reapLockedTx(ctx context.Context, tx *sql.Tx, m *protocol.Message, now time.Time) error {
	var rcp *protocol.Receipt
	row := tx.QueryRowContext(ctx,
		`SELECT `+receiptColumns+` FROM receipts WHERE id=$1 FOR UPDATE`, m.ReceiptID)
	r, err := scanReceipt(row)
	if err != nil {
		return err
	}
	rcp = r
	if rcp.Consumed {
		// Another transaction handled it; nothing to do.
		return nil
	}
	prior, err := loadPriorFailuresTx(ctx, tx, m.ID)
	if err != nil {
		return err
	}
	res := kernel.ReapOne(m, rcp, now, prior)
	if err := updateMessageTx(ctx, tx, res.Message); err != nil {
		return err
	}
	if err := updateReceiptTx(ctx, tx, res.Receipt); err != nil {
		return err
	}
	if err := insertFailureTx(ctx, tx, res.Failure); err != nil {
		return err
	}
	if res.Dead != nil {
		if err := setDeadReasonTx(ctx, tx, res.Message, res.Dead); err != nil {
			return err
		}
	}
	ev := res.Event
	return insertEventTx(ctx, tx, &ev)
}
