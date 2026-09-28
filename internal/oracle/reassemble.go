// Package oracle 提供一份独立于 reasm 包的“参考答案”重组实现。
//
// 独立性体现：
//   - 不 import 被测核心 reasm，也不调用 netmodel.ParseIPv4；输入是人工规格；
//   - 算法形态刻意不同：用位图标记字节覆盖 + 每片与历史片逐一比对，而不是
//     被测实现的 map 区间相交判断；
//   - 完全重复、重叠、冲突末片、超时缺片各自给出独立的 OracleVerdict。
//
// 测试同时驱动 oracle 与 reasm，并要求两边结论与重组字节一致。
package oracle

import "fmt"

// Verdict 是参考实现对一次喂片序列的最终判定。
type Verdict string

const (
	VerdictComplete Verdict = "complete"
	VerdictPending  Verdict = "pending"
	VerdictRejected Verdict = "rejected"
	VerdictTimedOut Verdict = "timed_out"
)

// Category 是拒绝原因（独立命名，测试再映射到 reasm.ErrorKind 交叉校验）。
type Category string

const (
	CategoryOverlap      Category = "overlap"
	CategoryConflictLast Category = "conflict_last"
	CategoryTooLarge     Category = "too_large"
	CategoryUnaligned    Category = "unaligned"
)

// Frag 是喂给参考实现的分片规格（直接用字节偏移，模拟解析后的事实）。
type Frag struct {
	Offset  int
	Payload []byte
	More    bool
}

// Result 是参考实现结论。
type Result struct {
	Verdict    Verdict
	Category   Category
	Assembled  []byte
	Duplicates int
	Detail     string
}

// Reassemble 以独立算法逐片处理。maxBytes 为重组上限。
// timedOut=true 时序列结束后仍未完成的组直接判定为超时（模拟已越过 deadline）。
func Reassemble(frags []Frag, maxBytes int, timedOut bool) Result {
	const uninitialized = -1
	var (
		covered    []int // 每个字节记录首个覆盖它的片序号
		totalLen   = uninitialized
		dupCount   int
		lastOffset = uninitialized
	)

	grow := func(end int) {
		if len(covered) >= end {
			return
		}
		grown := make([]int, end)
		for i := range grown {
			grown[i] = uninitialized
		}
		copy(grown, covered)
		covered = grown
	}
	reject := func(cat Category, format string, args ...any) Result {
		return Result{Verdict: VerdictRejected, Category: cat,
			Duplicates: dupCount, Detail: fmt.Sprintf(format, args...)}
	}

	for idx, f := range frags {
		start, end := f.Offset, f.Offset+len(f.Payload)

		if f.More && len(f.Payload)%8 != 0 {
			return reject(CategoryUnaligned, "片#%d MF=1 长度 %d 非 8 倍数", idx, len(f.Payload))
		}
		if end > maxBytes {
			return reject(CategoryTooLarge, "片#%d 终点 %d 超过上限 %d", idx, end, maxBytes)
		}

		// 先做字节级不变量：完全重复 vs 非完全重复的区间相交。
		// 与被测核心一致：当一个片同时构成覆盖与末片冲突时，优先归类为重叠。
		dup, collision := false, false
		for j := 0; j < idx; j++ {
			prev := frags[j]
			pEnd := prev.Offset + len(prev.Payload)
			sameRange := prev.Offset == start && pEnd == end
			switch {
			case sameRange && prev.More == f.More && bytesEqual(prev.Payload, f.Payload):
				dup = true
			case start < pEnd && prev.Offset < end:
				collision = true
			}
		}
		if dup {
			dupCount++
			continue
		}
		if collision {
			return reject(CategoryOverlap,
				"片#%d 区间[%d,%d) 与既有片重叠且非完全重复", idx, start, end)
		}

		// 再做末片一致性（含“非末片越过已宣告总长”的冲突）。
		if !f.More {
			if lastOffset != uninitialized && end != totalLen {
				return reject(CategoryConflictLast,
					"片#%d 末片终点 %d 与已有总长度 %d 冲突", idx, end, totalLen)
			}
			if lastOffset == uninitialized {
				lastOffset = start
				totalLen = end
			}
		}
		if totalLen != uninitialized && end > totalLen {
			return reject(CategoryConflictLast,
				"片#%d 越过末片宣告总长(end=%d total=%d)", idx, end, totalLen)
		}

		grow(end)
		for pos := start; pos < end; pos++ {
			covered[pos] = idx
		}
	}

	missing := -1
	if totalLen == uninitialized {
		if timedOut {
			return Result{Verdict: VerdictTimedOut, Duplicates: dupCount, Detail: "未见末片且已超时"}
		}
		return Result{Verdict: VerdictPending, Duplicates: dupCount, Detail: "末片未到，禁止提前输出"}
	}
	for pos := 0; pos < totalLen; pos++ {
		if pos >= len(covered) || covered[pos] == uninitialized {
			missing = pos
			break
		}
	}
	if missing >= 0 {
		if timedOut {
			return Result{Verdict: VerdictTimedOut, Duplicates: dupCount,
				Detail: fmt.Sprintf("位置 %d 缺片且已超时（末片已宣告总长 %d）", missing, totalLen)}
		}
		return Result{Verdict: VerdictPending, Duplicates: dupCount,
			Detail: fmt.Sprintf("末片已到但位置 %d 存在缺口（total=%d），禁止提前输出", missing, totalLen)}
	}

	out := make([]byte, totalLen)
	for _, f := range frags {
		copy(out[f.Offset:], f.Payload)
	}
	return Result{Verdict: VerdictComplete, Duplicates: dupCount, Assembled: out}
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
