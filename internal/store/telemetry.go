package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"replicactl/internal/controller"
	"replicactl/internal/model"
)

// InsertSample records one load report from a synthetic instance.
func (s *Store) InsertSample(ctx context.Context, sm model.Sample) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO samples(instance_id,metric,value,observed_at,received_at) VALUES(?,?,?,?,?)`,
		sm.InstanceID, sm.Metric, sm.Value,
		sm.ObservedAt.UTC().Format(time.RFC3339Nano),
		sm.ReceivedAt.UTC().Format(time.RFC3339Nano))
	return err
}

// LatestSamples returns the freshest retained report per active instance for
// the configured metric. Callers decide freshness against the returned
// ObservedAt, so both fresh and expired rows come back (needed to tell
// "stale" apart from "missing"). Rows belonging to instances no longer active
// are also returned harmlessly; the controller classifies against the fleet.
func (s *Store) LatestSamples(ctx context.Context, now time.Time) ([]model.Sample, error) {
	var cfgMetric string
	if err := s.db.QueryRowContext(ctx,
		`SELECT json_extract(payload,'$.metric') FROM config WHERE id=1`).Scan(&cfgMetric); err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT s.instance_id, s.metric, s.value, s.observed_at, s.received_at
		FROM samples s
		JOIN (
			SELECT instance_id, MAX(observed_at) AS max_obs
			FROM samples WHERE metric = ?
			GROUP BY instance_id
		) m ON m.instance_id = s.instance_id AND m.max_obs = s.observed_at
		WHERE s.metric = ?
		ORDER BY s.instance_id`, cfgMetric, cfgMetric)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Sample
	for rows.Next() {
		var sm model.Sample
		var obs, rec string
		if err := rows.Scan(&sm.InstanceID, &sm.Metric, &sm.Value, &obs, &rec); err != nil {
			return nil, err
		}
		sm.ObservedAt, _ = time.Parse(time.RFC3339Nano, obs)
		sm.ReceivedAt, _ = time.Parse(time.RFC3339Nano, rec)
		out = append(out, sm)
	}
	return out, rows.Err()
}

// PruneSamples deletes reports older than before.
func (s *Store) PruneSamples(ctx context.Context, before time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`DELETE FROM samples WHERE observed_at < ?`, before.UTC().Format(time.RFC3339Nano))
	return err
}

// InsertDemand records an external work signal.
func (s *Store) InsertDemand(ctx context.Context, d model.Demand) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO demand(pending,observed_at,received_at) VALUES(?,?,?)`,
		d.Pending, d.ObservedAt.UTC().Format(time.RFC3339Nano),
		d.ReceivedAt.UTC().Format(time.RFC3339Nano))
	return err
}

// LatestDemand returns the most recently observed demand signal.
func (s *Store) LatestDemand(ctx context.Context, now time.Time) (model.Demand, bool, error) {
	var d model.Demand
	var obs, rec string
	err := s.db.QueryRowContext(ctx,
		`SELECT pending,observed_at,received_at FROM demand ORDER BY observed_at DESC, id DESC LIMIT 1`).
		Scan(&d.Pending, &obs, &rec)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return model.Demand{}, false, nil
		}
		return model.Demand{}, false, err
	}
	d.ObservedAt, _ = time.Parse(time.RFC3339Nano, obs)
	d.ReceivedAt, _ = time.Parse(time.RFC3339Nano, rec)
	return d, true, nil
}

func isNoRows(err error) bool { return errors.Is(err, sql.ErrNoRows) }

// SaveObservation records one stable-window recommendation.
func (s *Store) SaveObservation(ctx context.Context, at time.Time, replicas int32) error {
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO downscale_observations(at,replicas) VALUES(?,?)
		 ON CONFLICT(at) DO UPDATE SET replicas=excluded.replicas`,
		at.UTC().Format(time.RFC3339Nano), replicas)
	return err
}

// ObservationsSince returns recommendations with at >= since, oldest first.
func (s *Store) ObservationsSince(ctx context.Context, since time.Time) ([]controller.Observation, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT at,replicas FROM downscale_observations WHERE at >= ? ORDER BY at ASC`,
		since.UTC().Format(time.RFC3339Nano))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []controller.Observation
	for rows.Next() {
		var at string
		var r int32
		if err := rows.Scan(&at, &r); err != nil {
			return nil, err
		}
		t, _ := time.Parse(time.RFC3339Nano, at)
		out = append(out, controller.Observation{At: t, Replicas: r})
	}
	return out, rows.Err()
}

// PruneObservations deletes observations older than before.
func (s *Store) PruneObservations(ctx context.Context, before time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`DELETE FROM downscale_observations WHERE at < ?`, before.UTC().Format(time.RFC3339Nano))
	return err
}
