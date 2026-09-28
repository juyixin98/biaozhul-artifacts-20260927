package store

import (
	"context"
	"database/sql"
)

// withTx runs fn in one transaction with the store's standard isolation.
// READ COMMITTED combined with explicit SELECT ... FOR UPDATE row locks gives
// the atomicity the broker needs: every decision (kernel.*) is computed while
// holding the lock on exactly the rows it mutates.
func withTx(ctx context.Context, db *sql.DB, fn func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}
