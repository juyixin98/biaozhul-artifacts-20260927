// Package compute 是计算内核：一组纯函数。它不做 IO、不持有状态，
// 是“实际工作”的承担者，也是层级派生结构（fanout）的唯一真实来源。
//
// 派生模型：一个作业由分层计划 Plan 描述。
//
//	type Plan = { fanouts: [f0, f1, ..., fL-1], hash_iters: n }
//
// 根任务在第 0 层，执行时派生 f0 个一层节点；第 d 层 fanout 节点
// （1 <= d < L-1）派生 f_d 个下层节点；第 L-1 层节点派生 hashchain
// 叶子。所有 fanout 节点携带同一份计划，子结构只由“深度+计划”决定，
// 因此完全确定：工作者只报告“我完成了”，无权决定派生出多少边、边指向谁。
//
// 独立测试的 oracle 可以只依赖本包（Validate/Execute/ExpectedTreeSize）
// 推导预期形状，而不需要引用被测的 kernel 包。
package compute

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"dsnet/proto"
)

// 输入参数的硬边界。所有校验都是确定性的，不依赖时钟或外部状态。
const (
	// MaxFanout 单层允许的最大扇出数。
	MaxFanout = 16
	// MaxDepth 叶子所在最大深度（根为 0，即最多 MaxDepth 层扇出）。
	MaxDepth = 4
	// MaxHashIters 叶子哈希链最大迭代次数。
	MaxHashIters = 5_000_000
	// MinHashIters 叶子哈希链最小迭代次数。
	MinHashIters = 1
)

// Plan 是一个作业的分层派生计划（根与每个 fanout 节点携带同一计划）。
type Plan struct {
	// Fanouts[k] 是第 k 层每个节点的扇出数，k 从 0（根的下一层）起。
	// 最后一层扇出产生的是 hashchain 叶子。长度 [1, MaxDepth]。
	Fanouts []int `json:"fanouts"`
	// HashIters 是每个叶子哈希链的迭代次数。
	HashIters int `json:"hash_iters"`
}

// HashchainPayload 是叶子任务的输入（种子由内核确定性生成）。
type HashchainPayload struct {
	Seed  string `json:"seed"`
	Iters int    `json:"iters"`
}

// ChildSpec 描述一个将要被派生的子任务的规格要素。
// 身份（TaskID）由状态机分配，计算内核只决定“结构”。
type ChildSpec struct {
	Kind    proto.TaskKind `json:"kind"`
	Depth   int            `json:"depth"`
	Payload map[string]any `json:"payload"`
}

// Result 是一次执行的结果。
type Result struct {
	Output   map[string]any `json:"output"`
	Children []ChildSpec    `json:"children"`
}

// LeafCount 返回计划中的叶子（hashchain）数。
func (p Plan) LeafCount() int64 {
	n := 1
	for _, f := range p.Fanouts {
		n *= f
	}
	return int64(n)
}

// Layers 返回扇出层数（= len(Fanouts)）。
func (p Plan) Layers() int { return len(p.Fanouts) }

// ValidatePlan 确定性校验计划。
func ValidatePlan(p Plan) error {
	if len(p.Fanouts) < 1 || len(p.Fanouts) > MaxDepth {
		return proto.Fail(proto.FailValidation,
			"fanouts 层数必须在 [1,%d]，实际 %d", MaxDepth, len(p.Fanouts))
	}
	for i, f := range p.Fanouts {
		if f < 1 || f > MaxFanout {
			return proto.Fail(proto.FailValidation,
				"fanouts[%d] 必须在 [1,%d]，实际 %d", i, MaxFanout, f)
		}
	}
	if p.HashIters < MinHashIters || p.HashIters > MaxHashIters {
		return proto.Fail(proto.FailValidation,
			"hash_iters 必须在 [%d,%d]，实际 %d", MinHashIters, MaxHashIters, p.HashIters)
	}
	return nil
}

// childKindAt 返回第 layer 层节点派生子节点的种类。
// layer 从 0（根所在层）到 L-1；最后一层派生叶子。
func childKindAt(p Plan, layer int) proto.TaskKind {
	if layer >= p.Layers()-1 {
		return proto.KindHashchain
	}
	return proto.KindFanout
}

// Validate 确定性校验一个任务规格是否可执行。返回的错误类别可直接断言。
func Validate(spec proto.TaskSpec) error {
	switch spec.Kind {
	case proto.KindRoot:
		if spec.Depth != 0 {
			return proto.Fail(proto.FailValidation, "根任务深度必须为 0，实际 %d", spec.Depth)
		}
		p, err := DecodePlan(spec.Payload)
		if err != nil {
			return err
		}
		if err := ValidatePlan(p); err != nil {
			return err
		}
		if spec.ParentID != "" {
			return proto.Fail(proto.FailValidation, "根任务不能有父节点")
		}
		return nil
	case proto.KindFanout:
		p, err := DecodePlan(spec.Payload)
		if err != nil {
			return err
		}
		if err := ValidatePlan(p); err != nil {
			return err
		}
		if spec.Depth < 1 || spec.Depth > p.Layers()-1 {
			return proto.Fail(proto.FailValidation,
				"fanout 节点深度 %d 超出计划层数 %d", spec.Depth, p.Layers())
		}
		if spec.ParentID == "" {
			return proto.Fail(proto.FailValidation, "非根任务必须有父节点")
		}
		return nil
	case proto.KindHashchain:
		p, err := DecodeHashchain(spec.Payload)
		if err != nil {
			return err
		}
		if p.Iters < MinHashIters || p.Iters > MaxHashIters {
			return proto.Fail(proto.FailValidation,
				"iters 必须在 [%d,%d]，实际 %d", MinHashIters, MaxHashIters, p.Iters)
		}
		if p.Seed == "" {
			return proto.Fail(proto.FailValidation, "seed 不能为空")
		}
		if spec.ParentID == "" {
			return proto.Fail(proto.FailValidation, "叶子任务必须有父节点")
		}
		return nil
	default:
		return proto.Fail(proto.FailValidation, "未知任务种类: %q", spec.Kind)
	}
}

// Execute 执行一个任务。纯函数：相同输入永远产生相同输出与子任务结构。
func Execute(spec proto.TaskSpec) (*Result, error) {
	if err := Validate(spec); err != nil {
		return nil, err
	}
	switch spec.Kind {
	case proto.KindRoot, proto.KindFanout:
		p, _ := DecodePlan(spec.Payload)
		layer := spec.Depth // 根在第 0 层
		kind := childKindAt(p, layer)
		fanout := p.Fanouts[layer]
		children := make([]ChildSpec, 0, fanout)
		for i := 0; i < fanout; i++ {
			cs := ChildSpec{Kind: kind, Depth: layer + 1}
			if kind == proto.KindHashchain {
				cs.Payload = EncodeHashchain(HashchainPayload{
					Seed:  childSeed(spec.TaskID, i),
					Iters: p.HashIters,
				})
			} else {
				cs.Payload = EncodePlan(p) // 下层节点携带同一计划
			}
			children = append(children, cs)
		}
		return &Result{
			Output: map[string]any{
				"layer":      layer,
				"spawned":    fanout,
				"child_kind": string(kind),
			},
			Children: children,
		}, nil
	case proto.KindHashchain:
		p, _ := DecodeHashchain(spec.Payload)
		sum := sha256.Sum256([]byte(p.Seed))
		for i := 1; i < p.Iters; i++ {
			sum = sha256.Sum256(sum[:])
		}
		return &Result{Output: map[string]any{
			"digest": hex.EncodeToString(sum[:]),
			"iters":  p.Iters,
			"seed":   p.Seed,
		}}, nil
	default:
		return nil, proto.Fail(proto.FailInternal, "Execute 未覆盖种类 %q", spec.Kind)
	}
}

// ExpectedChildren 供独立 oracle 使用：在不执行副作用的情况下给出预期子结构。
func ExpectedChildren(spec proto.TaskSpec) ([]ChildSpec, error) {
	r, err := Execute(spec)
	if err != nil {
		return nil, err
	}
	return r.Children, nil
}

// ExpectedTreeSize 返回计划对应的派生树总任务数（含根）。
// 计划 [f0,...,fL-1]：1 + f0 + f0*f1 + ... + f0*...*fL-1。
// 供 oracle 做计数断言。
func ExpectedTreeSize(p Plan) int {
	total, nodes := 1, 1
	for _, f := range p.Fanouts {
		nodes *= f
		total += nodes
	}
	return total
}

// ExpectedSignals 返回完整结算时应有的信号（已清偿因果边）数：总任务数-1。
func ExpectedSignals(p Plan) int { return ExpectedTreeSize(p) - 1 }

// DecodePlan 从通用 map 解出分层计划（宽容 JSON 数字类型）。
func DecodePlan(m map[string]any) (Plan, error) {
	var p Plan
	raw, ok := m["fanouts"]
	if !ok {
		return p, proto.Fail(proto.FailValidation, "缺少字段 fanouts")
	}
	arr, ok := raw.([]any)
	if !ok {
		return p, proto.Fail(proto.FailValidation, "fanouts 必须是数组")
	}
	p.Fanouts = make([]int, 0, len(arr))
	for i, v := range arr {
		n, err := toInt(v)
		if err != nil {
			return p, proto.Fail(proto.FailValidation, "fanouts[%d] 必须是整数", i)
		}
		p.Fanouts = append(p.Fanouts, n)
	}
	if v, ok := m["hash_iters"]; ok {
		n, err := toInt(v)
		if err != nil {
			return p, proto.Fail(proto.FailValidation, "hash_iters 必须是整数")
		}
		p.HashIters = n
	}
	return p, nil
}

// EncodePlan 编码计划。
func EncodePlan(p Plan) map[string]any {
	fs := make([]any, len(p.Fanouts))
	for i, f := range p.Fanouts {
		fs[i] = f
	}
	return map[string]any{"fanouts": fs, "hash_iters": p.HashIters}
}

// DecodeHashchain 从通用 map 解出 hashchain 负载。
func DecodeHashchain(m map[string]any) (HashchainPayload, error) {
	var p HashchainPayload
	p.Seed, _ = m["seed"].(string)
	v, ok := m["iters"]
	if !ok {
		return p, proto.Fail(proto.FailValidation, "缺少字段 iters")
	}
	n, err := toInt(v)
	if err != nil {
		return p, proto.Fail(proto.FailValidation, "iters 必须是整数")
	}
	p.Iters = n
	return p, nil
}

// EncodeHashchain 编码 hashchain 负载。
func EncodeHashchain(p HashchainPayload) map[string]any {
	return map[string]any{"seed": p.Seed, "iters": p.Iters}
}

func childSeed(parent proto.TaskID, idx int) string {
	return fmt.Sprintf("%s:%d", parent, idx)
}

// toInt 宽容 JSON 数字：encoding/json 默认解为 float64。
func toInt(v any) (int, error) {
	switch n := v.(type) {
	case int:
		return n, nil
	case int64:
		return int(n), nil
	case float64:
		if n != float64(int(n)) {
			return 0, fmt.Errorf("not an integer")
		}
		return int(n), nil
	default:
		return 0, fmt.Errorf("not numeric")
	}
}
