package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite" // 纯 Go SQLite 驱动，无需 CGO 工具链联网。

	"ipfragreasm/internal/netmodel"
)

// SQLiteStore 通过 database/sql 持久化组与分片。
type SQLiteStore struct {
	db *sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS frag_groups (
    key_text     TEXT PRIMARY KEY,
    src          TEXT NOT NULL,
    dst          TEXT NOT NULL,
    protocol     INTEGER NOT NULL,
    frag_id      INTEGER NOT NULL,
    state        TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    deadline     INTEGER NOT NULL,
    terminal_at  INTEGER NOT NULL DEFAULT 0,
    expires_at   INTEGER NOT NULL DEFAULT 0,
    has_last     INTEGER NOT NULL DEFAULT 0,
    last_offset  INTEGER NOT NULL DEFAULT 0,
    total_length INTEGER NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    assembled    BLOB
);
CREATE INDEX IF NOT EXISTS idx_groups_state ON frag_groups(state);
CREATE INDEX IF NOT EXISTS idx_groups_expires ON frag_groups(expires_at);

CREATE TABLE IF NOT EXISTS frag_fragments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key_text  TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    offset    INTEGER NOT NULL,
    length    INTEGER NOT NULL,
    more      INTEGER NOT NULL,
    duplicate INTEGER NOT NULL DEFAULT 0,
    seen_at   INTEGER NOT NULL,
    payload   BLOB NOT NULL,
    UNIQUE(key_text, seq)
);
CREATE INDEX IF NOT EXISTS idx_frag_key ON frag_fragments(key_text);
`

// OpenSQLite 打开（必要时创建）SQLite 存储并执行 schema 迁移。
// dsn 形如 file:data/reasm.sqlite?cache=shared；目录会自动创建。
// 传入 ":memory:" 使用私有内存数据库。
func OpenSQLite(ctx context.Context, dsn string) (*SQLiteStore, error) {
	if dsn == "" {
		dsn = ":memory:"
	}
	if path, ok := sqliteFilePath(dsn); ok && path != ":memory:" {
		if dir := filepath.Dir(path); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, fmt.Errorf("创建 SQLite 目录 %s: %w", dir, err)
			}
		}
	}

	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite %q: %w", dsn, err)
	}
	// 单写者模型下 WAL + busy_timeout 提供最稳的本地行为。
	if _, err := db.ExecContext(ctx, `
		PRAGMA journal_mode=WAL;
		PRAGMA busy_timeout=5000;
		PRAGMA foreign_keys=ON;
	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite pragmas: %w", err)
	}
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite schema: %w", err)
	}
	return &SQLiteStore{db: db}, nil
}

// sqliteFilePath 从 file:/path?query 或纯路径 DSN 中提取文件路径。
func sqliteFilePath(dsn string) (string, bool) {
	if dsn == ":memory:" {
		return dsn, true
	}
	rest := strings.TrimPrefix(dsn, "file:")
	if rest == dsn {
		// 纯路径或带查询串
		if i := strings.IndexByte(rest, '?'); i >= 0 {
			return rest[:i], true
		}
		return rest, true
	}
	if i := strings.IndexByte(rest, '?'); i >= 0 {
		rest = rest[:i]
	}
	return rest, true
}

// Close 关闭数据库句柄。
func (s *SQLiteStore) Close() error { return s.db.Close() }

func addrText(a netip.Addr) string { return a.String() }

func parseKey(src, dst string, proto uint8, id uint16) (netmodel.FragKey, error) {
	sa, err := netip.ParseAddr(src)
	if err != nil {
		return netmodel.FragKey{}, fmt.Errorf("存储中源地址非法 %q: %w", src, err)
	}
	da, err := netip.ParseAddr(dst)
	if err != nil {
		return netmodel.FragKey{}, fmt.Errorf("存储中目的地址非法 %q: %w", dst, err)
	}
	return netmodel.FragKey{Src: sa, Dst: da, Protocol: netmodel.Protocol(proto), ID: id}, nil
}

// UpsertGroup 覆盖写组记录。
func (s *SQLiteStore) UpsertGroup(ctx context.Context, g GroupRecord) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO frag_groups
		  (key_text, src, dst, protocol, frag_id, state, started_at, deadline,
		   terminal_at, expires_at, has_last, last_offset, total_length, reason, assembled)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(key_text) DO UPDATE SET
		  state=excluded.state,
		  started_at=excluded.started_at,
		  deadline=excluded.deadline,
		  terminal_at=excluded.terminal_at,
		  expires_at=excluded.expires_at,
		  has_last=excluded.has_last,
		  last_offset=excluded.last_offset,
		  total_length=excluded.total_length,
		  reason=excluded.reason,
		  assembled=excluded.assembled
	`,
		g.Key.String(), addrText(g.Key.Src), addrText(g.Key.Dst),
		uint8(g.Key.Protocol), g.Key.ID, string(g.State),
		g.StartedAt.UnixNano(), g.Deadline.UnixNano(),
		g.TerminalAt.UnixNano(), g.ExpiresAt.UnixNano(),
		boolInt(g.HasLast), g.LastOffset, g.TotalLength, g.Reason, g.Assembled,
	)
	if err != nil {
		return fmt.Errorf("upsert group: %w", err)
	}
	return nil
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// GetGroup 读取单个组。
func (s *SQLiteStore) GetGroup(ctx context.Context, key netmodel.FragKey) (GroupRecord, bool, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT src,dst,protocol,frag_id,state,started_at,deadline,terminal_at,
		       expires_at,has_last,last_offset,total_length,reason,assembled
		FROM frag_groups WHERE key_text=?
	`, key.String())
	g, err := scanGroup(row)
	if errors.Is(err, sql.ErrNoRows) {
		return GroupRecord{}, false, nil
	}
	if err != nil {
		return GroupRecord{}, false, err
	}
	return g, true, nil
}

type scannable interface {
	Scan(dest ...any) error
}

func scanGroup(row scannable) (GroupRecord, error) {
	var (
		src, dst, state, reason          string
		proto, fragID                    int
		started, deadline, term, expires int64
		hasLast                          int
		lastOff, total                   int
		assembled                        []byte
	)
	if err := row.Scan(&src, &dst, &proto, &fragID, &state, &started, &deadline, &term,
		&expires, &hasLast, &lastOff, &total, &reason, &assembled); err != nil {
		return GroupRecord{}, err
	}
	key, err := parseKey(src, dst, uint8(proto), uint16(fragID))
	if err != nil {
		return GroupRecord{}, err
	}
	return GroupRecord{
		Key:         key,
		State:       state,
		StartedAt:   time.Unix(0, started),
		Deadline:    time.Unix(0, deadline),
		TerminalAt:  time.Unix(0, term),
		ExpiresAt:   time.Unix(0, expires),
		HasLast:     hasLast != 0,
		LastOffset:  lastOff,
		TotalLength: total,
		Reason:      reason,
		Assembled:   assembled,
	}, nil
}

// DeleteGroup 在单个事务内删除组行与其分片行。
func (s *SQLiteStore) DeleteGroup(ctx context.Context, key netmodel.FragKey) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `DELETE FROM frag_fragments WHERE key_text=?`, key.String()); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM frag_groups WHERE key_text=?`, key.String()); err != nil {
		return err
	}
	return tx.Commit()
}

// AddFragment 插入一片；同 (key,seq) 重复插入报错（由调用方保证序号唯一）。
func (s *SQLiteStore) AddFragment(ctx context.Context, f FragmentRecord) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO frag_fragments (key_text, seq, offset, length, more, duplicate, seen_at, payload)
		VALUES (?,?,?,?,?,?,?,?)
	`, f.Key.String(), f.Seq, f.Offset, f.Length, boolInt(f.More), boolInt(f.Duplicate),
		f.SeenAt.UnixNano(), f.Payload)
	if err != nil {
		return fmt.Errorf("add fragment: %w", err)
	}
	return nil
}

// ListFragments 按 seq 升序返回。
func (s *SQLiteStore) ListFragments(ctx context.Context, key netmodel.FragKey) ([]FragmentRecord, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT seq, offset, length, more, duplicate, seen_at, payload
		FROM frag_fragments WHERE key_text=? ORDER BY seq ASC
	`, key.String())
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []FragmentRecord
	for rows.Next() {
		var (
			seq, off, length int
			more, dup        int
			seen             int64
			payload          []byte
		)
		if err := rows.Scan(&seq, &off, &length, &more, &dup, &seen, &payload); err != nil {
			return nil, err
		}
		out = append(out, FragmentRecord{
			Key: key, Seq: seq, Offset: off, Length: length,
			More: more != 0, Duplicate: dup != 0,
			SeenAt: time.Unix(0, seen), Payload: payload,
		})
	}
	return out, rows.Err()
}

// DeleteFragments 在组终结时清空活动分片，组行保留到留存到期。
func (s *SQLiteStore) ListOpenGroups(ctx context.Context) ([]GroupRecord, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT src,dst,protocol,frag_id,state,started_at,deadline,terminal_at,
		       expires_at,has_last,last_offset,total_length,reason,assembled
		FROM frag_groups WHERE state IN ('pending')
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectGroups(rows)
}

func collectGroups(rows *sql.Rows) ([]GroupRecord, error) {
	var out []GroupRecord
	for rows.Next() {
		g, err := scanGroup(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// ListTerminalExpired 返回留存到期的终结组。
func (s *SQLiteStore) ListTerminalExpired(ctx context.Context, now time.Time) ([]GroupRecord, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT src,dst,protocol,frag_id,state,started_at,deadline,terminal_at,
		       expires_at,has_last,last_offset,total_length,reason,assembled
		FROM frag_groups
		WHERE state IN ('complete','rejected','timed_out')
		  AND expires_at > 0 AND expires_at <= ?
	`, now.UnixNano())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectGroups(rows)
}

// ListAllGroups 返回全部组（诊断用）。
func (s *SQLiteStore) ListAllGroups(ctx context.Context) ([]GroupRecord, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT src,dst,protocol,frag_id,state,started_at,deadline,terminal_at,
		       expires_at,has_last,last_offset,total_length,reason,assembled
		FROM frag_groups ORDER BY key_text
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectGroups(rows)
}

// CountFragments 返回活动分片行数。
func (s *SQLiteStore) CountFragments(ctx context.Context) (int, error) {
	var n int
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM frag_fragments`).Scan(&n); err != nil {
		return 0, err
	}
	return n, nil
}

// DeleteFragments 清空组的分片行。
func (s *SQLiteStore) DeleteFragments(ctx context.Context, key netmodel.FragKey) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM frag_fragments WHERE key_text=?`, key.String())
	return err
}
