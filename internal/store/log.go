package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"fwrule/internal/replay"
)

// LogRecord is a persisted replay row.
type LogRecord struct {
	RequestID  string
	Version    int64
	PolicyName string
	Request    replay.Request
	Decision   replay.Decision
	CreatedAt  time.Time
}

// LogDecision persists one decision under its request id. A repeated request
// id is a collision error so an explanation can never be silently overwritten.
func (s *Store) LogDecision(ctx context.Context, req replay.Request, dec *replay.Decision) error {
	reqJSON, err := json.Marshal(req)
	if err != nil {
		return err
	}
	traceJSON, err := json.Marshal(dec.Trace)
	if err != nil {
		return err
	}
	var ruleID sql.NullString
	var ruleIdx sql.NullInt64
	if dec.MatchedRuleIndex >= 0 {
		ruleID = sql.NullString{String: dec.DecidedBy, Valid: true}
		ruleIdx = sql.NullInt64{Int64: int64(dec.MatchedRuleIndex), Valid: true}
	}
	var errCode, errDetail sql.NullString
	if dec.Status == replay.StatusError {
		errCode = sql.NullString{String: dec.ErrorCode, Valid: true}
		errDetail = sql.NullString{String: dec.ErrorDetail, Valid: true}
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO replay_log(
			request_id, version, policy_name, request_json,
			matched_rule_id, matched_rule_index, action, decided_by,
			status, error_code, error_detail, trace_json, created_at)
		 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		dec.RequestID, dec.Version, dec.PolicyName, string(reqJSON),
		ruleID, ruleIdx, dec.Action, dec.DecidedBy,
		dec.Status, errCode, errDetail, string(traceJSON),
		time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("request_id %q already exists: %w", dec.RequestID, ErrDuplicateRequest)
		}
		return err
	}
	return nil
}

// GetLog fetches one persisted decision by request id.
func (s *Store) GetLog(ctx context.Context, requestID string) (*LogRecord, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT request_id, version, policy_name, request_json,
			matched_rule_id, matched_rule_index, action, decided_by,
			status, error_code, error_detail, trace_json, created_at
		 FROM replay_log WHERE request_id=?`, requestID)
	return scanLog(row)
}

// ListLogs returns recent decisions newest-first.
func (s *Store) ListLogs(ctx context.Context, limit int) ([]LogRecord, error) {
	if limit <= 0 {
		limit = 50
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT request_id, version, policy_name, request_json,
			matched_rule_id, matched_rule_index, action, decided_by,
			status, error_code, error_detail, trace_json, created_at
		 FROM replay_log ORDER BY rowid DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []LogRecord
	for rows.Next() {
		rec, err := scanLog(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *rec)
	}
	return out, rows.Err()
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanLog(row rowScanner) (*LogRecord, error) {
	var (
		rec                                           LogRecord
		reqJSON, traceJSON, decidedBy, action, status string
		policyName                                    string
		ruleID, errCode, errDetail, createdAt         sql.NullString
		ruleIdx                                       sql.NullInt64
	)
	if err := row.Scan(
		&rec.RequestID, &rec.Version, &policyName, &reqJSON,
		&ruleID, &ruleIdx, &action, &decidedBy,
		&status, &errCode, &errDetail, &traceJSON, &createdAt,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrLogNotFound
		}
		return nil, err
	}
	rec.PolicyName = policyName
	if err := json.Unmarshal([]byte(reqJSON), &rec.Request); err != nil {
		return nil, err
	}
	d := replay.Decision{
		RequestID: rec.RequestID, Version: rec.Version, PolicyName: policyName,
		Status: status, Action: action, DecidedBy: decidedBy,
		MatchedRuleIndex: -1,
	}
	if ruleIdx.Valid {
		d.MatchedRuleIndex = int(ruleIdx.Int64)
	}
	if errCode.Valid {
		d.ErrorCode = errCode.String
		d.ErrorDetail = errDetail.String
	}
	if err := json.Unmarshal([]byte(traceJSON), &d.Trace); err != nil {
		return nil, err
	}
	rec.Decision = d
	rec.CreatedAt, _ = time.Parse(time.RFC3339Nano, createdAt.String)
	return &rec, nil
}

func isUniqueViolation(err error) bool {
	// modernc.org/sqlite reports constraint failures with this marker.
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint failed")
}
