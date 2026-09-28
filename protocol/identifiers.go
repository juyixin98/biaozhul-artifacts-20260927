// Package protocol defines the wire-level and journal-level types for the
// partitioned-log consumer-group coordinator. All other modules depend on
// these types but the protocol module itself has no internal dependencies, so
// it can be used independently by the independent test oracle.
package protocol

import "fmt"

// Topic is the name of a partitioned log topic.
type Topic string

// Partition is the zero-based index of a partition within a topic.
type Partition int32

// MemberID identifies a consumer-group member.
type MemberID string

// Generation is the monotonically increasing epoch of a group. Generation 0
// denotes the initial, unassigned state.
type Generation int64

// Seq is the per-group monotonic sequence number of a journal event.
type Seq int64

// TP is a topic/partition pair. It is the canonical identifier of a
// partition and is JSON-safe both as a struct and as a map key.
type TP struct {
	Topic     Topic     `json:"topic"`
	Partition Partition `json:"partition"`
}

// String renders "topic#partition".
func (t TP) String() string { return fmt.Sprintf("%s#%d", t.Topic, t.Partition) }

// MarshalText implements encoding.TextMarshaler so TP may be used as a JSON
// map key.
func (t TP) MarshalText() ([]byte, error) {
	return []byte(t.String()), nil
}

// UnmarshalText implements encoding.TextUnmarshaler.
func (t *TP) UnmarshalText(b []byte) error {
	var topic string
	var part int
	n, err := fmt.Sscanf(string(b), "%[^#]#%d", &topic, &part)
	if err != nil || n != 2 {
		return fmt.Errorf("protocol: invalid TP key %q", string(b))
	}
	t.Topic = Topic(topic)
	t.Partition = Partition(part)
	return nil
}
