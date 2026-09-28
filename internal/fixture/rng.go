package fixture

// RNG 是确定性的 64 位线性同余生成器（Numerical Recipes 参数），
// 让测试载荷在不同机器/运行之间可复现，且不依赖全局 math/rand 种子。
type RNG struct{ state uint64 }

// NewRNG 以 seed 初始化。
func NewRNG(seed uint64) *RNG { return &RNG{state: seed | 1} }

// Uint64 返回下一个伪随机数。
func (r *RNG) Uint64() uint64 {
	r.state = r.state*6364136223846793005 + 1442695040888963407
	return r.state
}

// Bytes 生成长度 n 的确定性载荷字节。
func (r *RNG) Bytes(n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = byte(r.Uint64() >> 56)
	}
	return out
}

// PatternPayload 生成“位置可辨识”载荷：字节 i 取值为 i 的线性混合，
// 重组后可逐字节断言位置是否正确（重叠错位会立刻暴露）。
func PatternPayload(n int) []byte {
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		out[i] = byte((i*37 + 11) % 251)
	}
	return out
}
