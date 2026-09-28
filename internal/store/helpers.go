package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"

	"crcontroller/internal/model"
	"crcontroller/internal/specutil"
)

func encodeJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "null"
	}
	return string(b)
}

func decodeJSON(s string, v any) error {
	if strings.TrimSpace(s) == "" {
		s = "null"
	}
	return json.Unmarshal([]byte(s), v)
}

func hashSpec(spec map[string]any) string { return specutil.Hash(spec) }

func isUnique(err error) bool {
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint failed")
}

func getForUpdate(ctx context.Context, tx *sql.Tx, uid string) (*model.Object, error) {
	row := tx.QueryRowContext(ctx,
		`SELECT `+objectCols+` FROM resources WHERE uid = ?`, uid)
	return scanObject(row)
}
