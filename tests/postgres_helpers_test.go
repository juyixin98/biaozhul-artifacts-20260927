package tests

import (
	"context"
	"errors"
	"strings"
	"testing"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
	"clsnap/internal/snapshot"
	"clsnap/internal/store"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type mapRouter map[string]protocol.NodeID

func staticRoutes(m map[string]protocol.NodeID) snapshot.Router { return mapRouter(m) }
func (r mapRouter) OwnerOf(a string) (protocol.NodeID, bool) { n, ok := r[a]; return n, ok }

func isConflictCode(err error, code string) bool {
	ae, ok := apperr.As(err)
	return ok && ae.Kind == apperr.KindConflict && ae.Code == code
}

// openPGNode connects to an isolated test database, creating it if absent.
// Data is PRESERVED across repeated calls within a test so a reopen simulates
// a process restart. Set fresh=true to drop/recreate first.
func openPGNode(t *testing.T, dsnTmpl, dbName, node string, accounts []protocol.Account) (*store.Postgres, error) {
	t.Helper()
	return openPGNodeFresh(t, dsnTmpl, dbName, node, accounts, false)
}

func openPGNodeFresh(t *testing.T, dsnTmpl, dbName, node string, accounts []protocol.Account, fresh bool) (*store.Postgres, error) {
	t.Helper()
	adminDSN := strings.ReplaceAll(dsnTmpl, "{db}", "postgres")
	conn, err := pgx.Connect(context.Background(), adminDSN)
	if err != nil {
		return nil, err
	}
	if fresh {
		_, _ = conn.Exec(context.Background(), `DROP DATABASE IF EXISTS `+quotePG(dbName))
	}
	_, err = conn.Exec(context.Background(), `CREATE DATABASE `+quotePG(dbName))
	if err != nil {
		// "already exists" is expected on a reopening test.
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "42P04" {
			conn.Close(context.Background())
			return nil, err
		}
	}
	conn.Close(context.Background())

	st, err := store.OpenPostgres(context.Background(), strings.ReplaceAll(dsnTmpl, "{db}", dbName))
	if err != nil {
		return nil, err
	}
	t.Cleanup(func() { st.Close() })
	if err := st.Bootstrap(context.Background(), protocol.NodeID(node), accounts); err != nil {
		return nil, err
	}
	return st, nil
}

func quotePG(s string) string { return `"` + strings.ReplaceAll(s, `"`, "") + `"` }
