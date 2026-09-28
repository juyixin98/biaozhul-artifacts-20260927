package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"

	"workbroker/internal/kernel"
	"workbroker/internal/protocol"
)

// loadReceiptTx locks and returns the receipt (nil when absent), plus its
// message row (nil when absent).
func loadReceiptTx(ctx context.Context, tx *sql.Tx, receiptID string) (*protocol.Receipt, *protocol.Message, error) {
	rcpRow := tx.QueryRowContext(ctx,
		`SELECT `+receiptColumns+` FROM receipts WHERE id=$1 FOR UPDATE`, receiptID)
	rcp, err := scanReceipt(rcpRow)
	if err == sql.ErrNoRows {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	msgRow := tx.QueryRowContext(ctx,
		`SELECT `+messageColumns+` FROM messages WHERE id=$1 FOR UPDATE`, rcp.MessageID)
	msg, err := scanMessage(msgRow)
	if err == sql.ErrNoRows {
		return rcp, nil, nil
	}
	if err != nil {
		return rcp, nil, err
	}
	return rcp, msg, nil
}

// reapMessageIfDueTx mirrors the memory store's pre-action reap: while the
// message row is locked, if it is expired inflight, apply ReapOne. This is the
// atomic boundary that decides extend/ack vs timeout — there is no window in
// which a past-deadline receipt is accepted.
func reapMessageIfDueTx(ctx context.Context, tx *sql.Tx, msg *protocol.Message, now time.Time) error {
	if msg.Status != protocol.StatusInFlight || now.Before(msg.VisibilityDeadline) {
		return nil
	}
	return reapLockedTx(ctx, tx, msg, now)
}

func (p *Postgres) Extend(ctx context.Context, receiptID string, extend time.Duration) (*protocol.Message, protocol.Event, error) {
	now := p.clk.Now()
	var outMsg *protocol.Message
	var outEv protocol.Event
	err := withTx(ctx, p.db, func(tx *sql.Tx) error {
		rcp, msg, err := loadReceiptTx(ctx, tx, receiptID)
		if err != nil {
			return err
		}
		if rcp == nil {
			return protocol.NewFailure("extend", protocol.FailReceiptNotFound,
				"receipt does not exist: "+receiptID, nil)
		}
		if msg == nil {
			return protocol.NewFailure("extend", protocol.FailMessageNotFound,
				"message referenced by receipt is gone: "+rcp.MessageID, nil)
		}
		if err := reapMessageIfDueTx(ctx, tx, msg, now); err != nil {
			return err
		}
		// Reload after possible reap so the guard sees the post-timeout state.
		if msg.Status == protocol.StatusInFlight {
			row := tx.QueryRowContext(ctx,
				`SELECT `+receiptColumns+` FROM receipts WHERE id=$1 FOR UPDATE`, receiptID)
			rcp, err = scanReceipt(row)
			if err != nil {
				return err
			}
		} else {
			// After reap the receipt is consumed; re-read it.
			row := tx.QueryRowContext(ctx,
				`SELECT `+receiptColumns+` FROM receipts WHERE id=$1`, receiptID)
			rcp, err = scanReceipt(row)
			if err != nil {
				return err
			}
		}
		newer, err := newerReceiptCountTx(ctx, tx, rcp)
		if err != nil {
			return err
		}
		res, f := kernel.ExtendOne(msg, rcp, newer, extend, now)
		if f != nil {
			return f
		}
		if err := updateMessageTx(ctx, tx, res.Message); err != nil {
			return err
		}
		if err := updateReceiptTx(ctx, tx, res.Receipt); err != nil {
			return err
		}
		ev := res.Event
		if err := insertEventTx(ctx, tx, &ev); err != nil {
			return err
		}
		outMsg, outEv = res.Message, ev
		return nil
	})
	if err != nil {
		return nil, protocol.Event{}, mapErr("extend", err)
	}
	return outMsg, outEv, nil
}

func (p *Postgres) Ack(ctx context.Context, receiptID string) (*protocol.Message, protocol.Event, error) {
	now := p.clk.Now()
	var outMsg *protocol.Message
	var outEv protocol.Event
	err := withTx(ctx, p.db, func(tx *sql.Tx) error {
		rcp, msg, err := loadReceiptTx(ctx, tx, receiptID)
		if err != nil {
			return err
		}
		if rcp == nil {
			return protocol.NewFailure("ack", protocol.FailReceiptNotFound,
				"receipt does not exist: "+receiptID, nil)
		}
		if msg == nil {
			return protocol.NewFailure("ack", protocol.FailMessageNotFound,
				"message referenced by receipt is gone: "+rcp.MessageID, nil)
		}
		if err := reapMessageIfDueTx(ctx, tx, msg, now); err != nil {
			return err
		}
		if msg.Status != protocol.StatusInFlight {
			row := tx.QueryRowContext(ctx,
				`SELECT `+receiptColumns+` FROM receipts WHERE id=$1`, receiptID)
			if rcp, err = scanReceipt(row); err != nil {
				return err
			}
		}
		newer, err := newerReceiptCountTx(ctx, tx, rcp)
		if err != nil {
			return err
		}
		res, f := kernel.AckOne(msg, rcp, newer, now)
		if f != nil {
			return f
		}
		if err := updateMessageTx(ctx, tx, res.Message); err != nil {
			return err
		}
		if err := updateReceiptTx(ctx, tx, res.Receipt); err != nil {
			return err
		}
		ev := res.Event
		if err := insertEventTx(ctx, tx, &ev); err != nil {
			return err
		}
		outMsg, outEv = res.Message, ev
		return nil
	})
	if err != nil {
		return nil, protocol.Event{}, mapErr("ack", err)
	}
	return outMsg, outEv, nil
}

func (p *Postgres) Nack(ctx context.Context, receiptID, reason string) (*protocol.Message, protocol.Event, error) {
	now := p.clk.Now()
	var outMsg *protocol.Message
	var outEv protocol.Event
	err := withTx(ctx, p.db, func(tx *sql.Tx) error {
		rcp, msg, err := loadReceiptTx(ctx, tx, receiptID)
		if err != nil {
			return err
		}
		if rcp == nil {
			return protocol.NewFailure("nack", protocol.FailReceiptNotFound,
				"receipt does not exist: "+receiptID, nil)
		}
		if msg == nil {
			return protocol.NewFailure("nack", protocol.FailMessageNotFound,
				"message referenced by receipt is gone: "+rcp.MessageID, nil)
		}
		if err := reapMessageIfDueTx(ctx, tx, msg, now); err != nil {
			return err
		}
		if msg.Status != protocol.StatusInFlight {
			row := tx.QueryRowContext(ctx,
				`SELECT `+receiptColumns+` FROM receipts WHERE id=$1`, receiptID)
			if rcp, err = scanReceipt(row); err != nil {
				return err
			}
		}
		newer, err := newerReceiptCountTx(ctx, tx, rcp)
		if err != nil {
			return err
		}
		prior, err := loadPriorFailuresTx(ctx, tx, msg.ID)
		if err != nil {
			return err
		}
		res, f := kernel.NackOne(msg, rcp, newer, reason, now, prior)
		if f != nil {
			return f
		}
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
		if err := insertEventTx(ctx, tx, &ev); err != nil {
			return err
		}
		outMsg, outEv = res.Message, ev
		return nil
	})
	if err != nil {
		return nil, protocol.Event{}, mapErr("nack", err)
	}
	return outMsg, outEv, nil
}

func (p *Postgres) DeadList(ctx context.Context, partition string, limit int) ([]DeadRecord, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := p.db.QueryContext(ctx, `
		SELECT `+messageColumns+`, COALESCE(dead_reason, '{}'::jsonb)
		FROM messages
		WHERE status='dead' AND partition_key=$1
		ORDER BY created_at, id
		LIMIT $2`, partition, limit)
	if err != nil {
		return nil, mapErr("deadlist", err)
	}
	defer rows.Close()

	var out []DeadRecord
	for rows.Next() {
		var m protocol.Message
		var status string
		var inflightAt, deadline sql.NullTime
		var receiptID, workerID sql.NullString
		var rawDead []byte
		if err := rows.Scan(
			&m.ID, &m.Partition, &m.Body, &status,
			&m.Attempts, &m.MaxAttempts,
			&m.AvailableAt, &inflightAt, &deadline, &receiptID, &workerID,
			&m.CreatedAt, &m.UpdatedAt, &rawDead,
		); err != nil {
			return nil, mapErr("deadlist", err)
		}
		m.Status = protocol.Status(status)
		var reason protocol.DeadReason
		if err := json.Unmarshal(rawDead, &reason); err != nil {
			return nil, mapErr("deadlist", err)
		}
		out = append(out, DeadRecord{Message: &m, Reason: &reason})
	}
	return out, rows.Err()
}

func (p *Postgres) Events(ctx context.Context, afterSeq int64, limit int) ([]protocol.Event, error) {
	if limit <= 0 {
		limit = 1000
	}
	rows, err := p.db.QueryContext(ctx, `
		SELECT seq, event_type, message_id, partition_key, receipt_id, worker_id,
		       occurred_at, attempt, class, reason, version, COALESCE(data, '{}'::jsonb)
		FROM events
		WHERE seq > $1
		ORDER BY seq
		LIMIT $2`, afterSeq, limit)
	if err != nil {
		return nil, mapErr("events", err)
	}
	defer rows.Close()
	var out []protocol.Event
	for rows.Next() {
		var ev protocol.Event
		var tp, class string
		var data []byte
		if err := rows.Scan(&ev.Seq, &tp, &ev.MessageID, &ev.Partition,
			&ev.ReceiptID, &ev.WorkerID, &ev.At, &ev.Attempt, &class,
			&ev.Reason, &ev.Version, &data); err != nil {
			return nil, mapErr("events", err)
		}
		ev.Type = protocol.EventType(tp)
		ev.Class = protocol.AttemptCause(class)
		ev.Data = data
		out = append(out, ev)
	}
	return out, rows.Err()
}
