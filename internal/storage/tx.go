package storage

import (
	"context"
	"database/sql"

	"lifecycle.local/v1/internal/model"
)

func (s *sqliteStore) RunInTx(ctx context.Context, fn func(Queryer) error) error {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "begin tx: %v", err)
	}
	if err := fn(tx); err != nil {
		if rbErr := tx.Rollback(); rbErr != nil {
			return model.Errorf(model.ErrKindStorage, "rollback (%v) after: %v", rbErr, err)
		}
		return err
	}
	if err := tx.Commit(); err != nil {
		return model.Errorf(model.ErrKindStorage, "commit tx: %v", err)
	}
	return nil
}

func (s *sqliteStore) ReadTx(ctx context.Context, fn func(Queryer) error) error {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{
		Isolation: sql.LevelSerializable,
		ReadOnly:  true,
	})
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "begin read tx: %v", err)
	}
	defer func() { _ = tx.Rollback() }()
	return fn(tx)
}

func (s *sqliteStore) MetaGet(ctx context.Context, key string) (string, bool, error) {
	var v string
	err := s.db.QueryRowContext(ctx, `SELECT value FROM meta WHERE key = ?`, key).Scan(&v)
	if err == sql.ErrNoRows {
		return "", false, nil
	}
	if err != nil {
		return "", false, model.Errorf(model.ErrKindStorage, "meta get %q: %v", key, err)
	}
	return v, true, nil
}

func (s *sqliteStore) MetaSet(ctx context.Context, key, value string) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO meta(key, value) VALUES(?, ?)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value`, key, value)
	if err != nil {
		return model.Errorf(model.ErrKindStorage, "meta set %q: %v", key, err)
	}
	return nil
}
