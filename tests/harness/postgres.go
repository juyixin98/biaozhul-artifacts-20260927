package harness

import (
	"context"
	"strings"

	"clsnap/internal/apperr"
	"clsnap/internal/store"

	"github.com/jackc/pgx/v5"
)

// openFreshPostgres (re)creates an isolated per-node database and returns a
// connected store. dsnTmpl contains the placeholder "{db}" which is replaced
// with dbName for the data connection and with "postgres" for the admin
// connection that performs CREATE DATABASE. This gives every node a fully
// independent durable store in the local cluster.
func openFreshPostgres(ctx context.Context, dsnTmpl, dbName string) (*store.Postgres, error) {
	if !strings.Contains(dsnTmpl, "{db}") {
		return nil, apperr.Inputf(apperr.CodeMalformed,
			"postgres dsn template must contain {db} placeholder")
	}
	adminDSN := strings.ReplaceAll(dsnTmpl, "{db}", "postgres")
	conn, err := pgx.Connect(ctx, adminDSN)
	if err != nil {
		return nil, apperr.Failure(apperr.CodeStoreIO, "openFreshPostgres", "admin connect", err)
	}
	_, _ = conn.Exec(ctx, `DROP DATABASE IF EXISTS `+quoteIdent(dbName))
	if _, err := conn.Exec(ctx, `CREATE DATABASE `+quoteIdent(dbName)); err != nil {
		conn.Close(ctx)
		return nil, apperr.Failure(apperr.CodeStoreIO, "openFreshPostgres", "create database "+dbName, err)
	}
	conn.Close(ctx)

	st, err := store.OpenPostgres(ctx, strings.ReplaceAll(dsnTmpl, "{db}", dbName))
	if err != nil {
		return nil, err
	}
	return st, nil
}

// quoteIdent is a minimal safe identifier quoter: dbName is produced by
// sanitize() so it is already [a-z0-9_], but double-quote anyway.
func quoteIdent(s string) string {
	return `"` + strings.ReplaceAll(s, `"`, "") + `"`
}
