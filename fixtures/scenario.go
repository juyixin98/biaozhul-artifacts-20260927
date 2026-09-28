package fixtures

// MemberPolicy controls how a synthetic member reacts to revocation.
type MemberPolicy string

const (
	// PolicyCooperative: the member confirms every revocation immediately.
	PolicyCooperative MemberPolicy = "cooperative"
	// PolicySlow: the member never confirms revocations on its own. Tests
	// either drive the quarantine path or ack explicitly (or late).
	PolicySlow MemberPolicy = "slow"
	// PolicyLate: the member confirms only after a delay the test applies
	// explicitly via scenario ticks.
	PolicyLate MemberPolicy = "late"
)

// MemberSpec is a fixture-level member definition.
type MemberSpec struct {
	ID       string
	Topics   []string
	Policy   MemberPolicy
	// LateAckDelayMS: for PolicyLate, how far past revoke deadline the ack
	// arrives. Zero means "never until the test forces it".
	LateAckDelayMS int
	// SessionTimeoutMS overrides the group default for this member.
	SessionTimeoutMS int
}

// PartitionCount maps topic name -> partition count.
type Scenario struct {
	Group    string
	Topics   map[string]int
	// RevokeTimeoutMS / QuarantineTimeoutMS / SessionTimeoutMS configure the
	// group. Short values keep tests fast and deterministic.
	RevokeTimeoutMS     int
	QuarantineTimeoutMS int
	SessionTimeoutMS    int
	Members             []MemberSpec
}

// StandardScenario is the canonical fixture: one topic, six partitions,
// room to show balancing, retention and transfer.
func StandardScenario() Scenario {
	return Scenario{
		Group:               "orders",
		Topics:              map[string]int{"orders": 6},
		RevokeTimeoutMS:     200,
		QuarantineTimeoutMS: 500,
		SessionTimeoutMS:    1000,
		Members: []MemberSpec{
			{ID: "alice", Topics: []string{"orders"}, Policy: PolicyCooperative},
			{ID: "bob", Topics: []string{"orders"}, Policy: PolicyCooperative},
			{ID: "carol", Topics: []string{"orders"}, Policy: PolicySlow},
		},
	}
}

// MultiTopicScenario exercises independent per-topic assignment.
func MultiTopicScenario() Scenario {
	return Scenario{
		Group:               "mixed",
		Topics:              map[string]int{"orders": 4, "payments": 2},
		RevokeTimeoutMS:     200,
		QuarantineTimeoutMS: 500,
		SessionTimeoutMS:    1000,
		Members: []MemberSpec{
			{ID: "a1", Topics: []string{"orders", "payments"}, Policy: PolicyCooperative},
			{ID: "a2", Topics: []string{"orders"}, Policy: PolicyCooperative},
			{ID: "a3", Topics: []string{"payments"}, Policy: PolicyCooperative},
		},
	}
}
