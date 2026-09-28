// Package ingest 负责离线数据包（合成夹具）的文件装载与校验。
//
// Bundle 是一次装载单元：一个标签快照 + 一个策略集合。
// 它让测试与示例可以用纯本地 JSON 夹具驱动整个后端，无需任何真实账号/业务数据。
package ingest

import (
	"encoding/json"
	"fmt"
	"os"

	"netpolreach/internal/model"
	"netpolreach/internal/policy"
)

// Bundle 是离线装载数据包。
type Bundle struct {
	Snapshot  model.Snapshot  `json:"snapshot"`
	PolicySet model.PolicySet `json:"policy_set"`
}

// LoadBundleFile 从 JSON 文件读取并校验数据包（不落库，由调用方决定如何保存）。
func LoadBundleFile(path string) (Bundle, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Bundle{}, fmt.Errorf("读取数据包失败: %w", err)
	}
	return ParseBundle(raw)
}

// ParseBundle 解析并校验数据包。
func ParseBundle(raw []byte) (Bundle, error) {
	var b Bundle
	if err := json.Unmarshal(raw, &b); err != nil {
		return Bundle{}, fmt.Errorf("数据包 JSON 非法: %w", err)
	}
	if err := ValidateBundle(b); err != nil {
		return Bundle{}, err
	}
	return b, nil
}

// ValidateBundle 校验数据包内部一致性。
func ValidateBundle(b Bundle) error {
	if err := policy.ValidateSnapshot(b.Snapshot); err != nil {
		return fmt.Errorf("快照非法: %w", err)
	}
	if err := policy.ValidatePolicySet(b.PolicySet); err != nil {
		return fmt.Errorf("策略集合非法: %w", err)
	}
	// 不强制版本相等：允许装载“版本不一致”数据包来驱动 UNKNOWN 诊断，
	// 但必须显式提示。
	return nil
}
