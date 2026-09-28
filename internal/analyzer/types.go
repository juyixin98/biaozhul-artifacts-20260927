package analyzer

import (
	"fwrule/internal/config"
	"fwrule/internal/netmodel"
)

// Category is the diagnostic classification of a rule.
type Category string

const (
	// CatFullShadow: not one packet exists for which the rule is the
	// first-match decision (match set empty or wholly covered by earlier rules).
	CatFullShadow Category = "FULL_SHADOW"
	// CatPartialShadow: at least one packet in the rule's own match set is
	// decided by an earlier rule, while other packets still reach it.
	CatPartialShadow Category = "PARTIAL_SHADOW"
	// CatRedundant: the rule is reachable (has packets it wins) but deleting
	// it leaves the policy's packet-to-action function unchanged.
	CatRedundant Category = "REDUNDANT"
	// CatEmptyMatch: the rule's parsed match set is provably empty (currently
	// emitted for v4/v6 cross-family src/dst pairs).
	CatEmptyMatch Category = "EMPTY_MATCH"
)

// WitnessJSON is one concrete packet that demonstrates a claim.
type WitnessJSON struct {
	Family    string `json:"family"`
	Protocol  uint8  `json:"protocol"`
	ProtoName string `json:"protocol_name,omitempty"`
	SrcIP     string `json:"src_ip"`
	SrcPort   uint16 `json:"src_port,omitempty"`
	DstIP     string `json:"dst_ip"`
	DstPort   uint16 `json:"dst_port,omitempty"`
	// DecidedBy: rule id that first-matches this packet ("<default:deny>" when
	// no rule matches). Makes the witness self-verifying when replayed.
	DecidedBy string `json:"decided_by"`
	Action    string `json:"action"`
}

// CoverPartition is one rectangle of the coverage witness. It names an exact
// region of packet space (src block × dst block × src port interval × dst port
// interval), its size, a concrete packet in it, and the rule that decides it.
type CoverPartition struct {
	SrcCIDR     string      `json:"src_cidr"`
	DstCIDR     string      `json:"dst_cidr"`
	SrcPorts    string      `json:"src_ports,omitempty"`
	DstPorts    string      `json:"dst_ports,omitempty"`
	PacketCount string      `json:"packet_count"`
	Witness     WitnessJSON `json:"witness"`
	// WinningRule is the rule that first-matches every packet in the region;
	// for shadowed regions it is the earlier rule that steals them.
	WinningRule string `json:"winning_rule,omitempty"`
}

// Diagnostic is one finding about one rule.
type Diagnostic struct {
	RuleID string   `json:"rule_id"`
	Index  int      `json:"index"`
	Kind   Category `json:"kind"`
	Detail string   `json:"detail"`
	// Removable: deleting the rule preserves the policy decision for every
	// packet in both address families.
	Removable bool `json:"removable"`
	// Evidence: concrete witnesses / coverage partitions proving the claim.
	Witness    *WitnessJSON     `json:"witness,omitempty"`
	Partitions []CoverPartition `json:"partitions,omitempty"`
	// TruncatedPartitions: more cover exists than the reported cap.
	TruncatedPartitions bool `json:"truncated_partitions,omitempty"`
}

// RuleSummary is the analyzer's per-rule verdict.
type RuleSummary struct {
	Index        int        `json:"index"`
	RuleID       string     `json:"rule_id"`
	Action       string     `json:"action"`
	Family       string     `json:"family,omitempty"`
	Protocol     string     `json:"protocol"`
	Kinds        []Category `json:"kinds"`
	Reachable    bool       `json:"reachable"`
	Removable    bool       `json:"removable"`
	Uncertain    bool       `json:"uncertain"`
	UncertainWhy string     `json:"uncertain_why,omitempty"`
}

// Uncertainty is a conclusion that could not be made with full confidence.
type Uncertainty struct {
	Scope  string `json:"scope"`
	RuleID string `json:"rule_id,omitempty"`
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

// Report is the complete analysis result.
type Report struct {
	PolicyName    string            `json:"policy_name"`
	PolicyVersion int64             `json:"policy_version"`
	Default       map[string]string `json:"default"`
	RuleCount     int               `json:"rule_count"`
	Rules         []*RuleSummary    `json:"rules"`
	Diagnostics   []Diagnostic      `json:"diagnostics"`
	Uncertainties []Uncertainty     `json:"uncertainties"`
	// Stats are reproducibility hints about how the evidence was computed.
	Stats map[string]int `json:"stats"`
}

// Analyze performs exact first-match shadow/redundancy analysis of a policy.
// policyVersion is stamped into the report (storage version); pass 0 for an
// ad-hoc analysis.
func Analyze(p *config.Policy, policyVersion int64) *Report {
	a := &analyzer{pol: p, version: policyVersion}
	return a.run()
}

type analyzer struct {
	pol     *config.Policy
	version int64
	stats   map[string]int
	uncs    []Uncertainty
}

var famForAny = []netmodel.Family{netmodel.FamV4, netmodel.FamV6}
