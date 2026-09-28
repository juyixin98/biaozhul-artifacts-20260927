// Package trie 实现地址族无关的路径压缩二进制前缀树（Patricia trie）。
//
// 设计要点：
//   - 键是定宽比特串（IPv4 32 位 / IPv6 128 位），节点存储“从父节点
//     分叉位之后到本节点分叉位之间”被压缩掉的公共比特段（skip/prefix），
//     因此树高只随不同前缀数增长，不随地址长度增长；
//   - 所有写操作写时复制（COW）：插入/删除返回新根，沿途克隆节点，
//     旧根保持只读快照，RIB 借此实现“读不持锁、批量替换整表原子可见”；
//   - 节点可同时携带值与子节点，所以默认路由（/0）与更具体前缀可共存，
//     最长前缀匹配（LPM）在下行过程中记录沿途最深的带值节点。
package trie

import "math/bits"

// node 是前缀树节点。prefix 左对齐存放 skip 个被压缩比特。
// branch 位不单独存储——它是“段之后的下一比特”，由 depth+skip 决定。
type node[V any] struct {
	skip     int
	prefix   []byte
	hasValue bool
	value    V
	children [2]*node[V]
}

// Trie 是一棵不可变快照语义的前缀树。零值不可用，请用 New。
type Trie[V any] struct {
	root *node[V]
}

func New[V any]() *Trie[V] { return &Trie[V]{} }

// rootNode 暴露根节点给同包测试/遍历使用。
func (t *Trie[V]) rootNode() *node[V] { return t.root }

func bitAt(b []byte, i int) int {
	return int((b[i/8] >> (7 - uint(i%8))) & 1)
}

// commonPrefixLen 返回 a、b 从 0 开始相同的前导比特数，最多比较 max 位。
// 调用方保证 a、b 至少有 ceil(max/8) 字节。
func commonPrefixLen(a, b []byte, max int) int {
	n := 0
	for n < max {
		ai := n / 8
		if ai >= len(a) || ai >= len(b) {
			break
		}
		x := a[ai] ^ b[ai]
		if x != 0 {
			n += bits.LeadingZeros8(x)
			break
		}
		n += 8
	}
	if n > max {
		n = max
	}
	return n
}

// extractBits 从 key 的 start 位起取出 n 个比特，返回左对齐字节切片。
func extractBits(key []byte, start, n int) []byte {
	if n <= 0 {
		return nil
	}
	out := make([]byte, (n+7)/8)
	for i := 0; i < n; i++ {
		if bitAt(key, start+i) == 1 {
			out[i/8] |= 1 << (7 - uint(i%8))
		}
	}
	return out
}

// bitsBuilder 逐比特拼装左对齐比特串。
type bitsBuilder struct {
	buf []byte
	n   int
}

func (w *bitsBuilder) writeBit(bit int) {
	if w.n%8 == 0 {
		w.buf = append(w.buf, 0)
	}
	if bit == 1 {
		w.buf[w.n/8] |= 1 << (7 - uint(w.n%8))
	}
	w.n++
}

func (w *bitsBuilder) writeBits(src []byte, start, count int) {
	for i := 0; i < count; i++ {
		w.writeBit(bitAt(src, start+i))
	}
}

func (w *bitsBuilder) bytes() []byte {
	if w.n == 0 {
		return nil
	}
	return append([]byte(nil), w.buf...)
}

func (n *node[V]) shallowCopy() *node[V] {
	cp := *n
	return &cp
}

// newLeaf 为在 depth 处开始、键长为 keyBits 的键创建叶子段节点，
// 段覆盖 [depth, keyBits)，值挂在段末位置。
func newLeaf[V any](key []byte, depth, keyBits int, value V) *node[V] {
	return &node[V]{
		skip:     keyBits - depth,
		prefix:   extractBits(key, depth, keyBits-depth),
		hasValue: true,
		value:    value,
	}
}

// matchSegment 比较 key 自 depth 起与节点压缩段的重合情况：
//   - full=true：skip 位全部相同（key 足够长）；
//   - full=false，common 为首个不同位的段内下标，或 key 在段内耗尽时
//     已匹配的位数（即“分歧位”位置）。
func (n *node[V]) matchSegment(key []byte, depth, keyBits int) (full bool, common int) {
	avail := keyBits - depth
	lim := n.skip
	if avail < lim {
		lim = avail
	}
	for i := 0; i < lim; i++ {
		if bitAt(key, depth+i) != bitAt(n.prefix, i) {
			return false, i
		}
	}
	if avail < n.skip {
		return false, lim // key 在段内结束，分歧点恰为键尾
	}
	return true, n.skip
}

// insert 在以 n 为根、已消费 depth 个键比特的子树写入。
func (n *node[V]) insert(key []byte, depth, keyBits int, value V) *node[V] {
	if n == nil {
		return newLeaf(key, depth, keyBits, value)
	}

	full, common := n.matchSegment(key, depth, keyBits)
	if !full {
		return n.split(key, depth, keyBits, value, common)
	}

	pos := depth + n.skip // 段末分叉位
	if pos >= keyBits {
		// 键恰好结束在本段末（pos==keyBits）：值挂本节点。
		cp := n.shallowCopy()
		cp.hasValue = true
		cp.value = value
		return cp
	}
	b := bitAt(key, pos)
	cp := n.shallowCopy()
	cp.children[b] = cp.children[b].insert(key, pos+1, keyBits, value)
	return cp
}

// split 在段内第 j 位（相对段起点）处分叉，重排为 父节点P -> 旧节点/新键。
// j == keyBits-depth 表示新键在分歧位前结束（值挂在新父节点上）。
func (n *node[V]) split(key []byte, depth, keyBits int, value V, j int) *node[V] {
	// 父节点接管公共段 [0,j)。
	parent := &node[V]{
		skip:   j,
		prefix: extractBits(n.prefix, 0, j),
	}

	// 旧节点成为父节点在“旧段第 j 位”一侧的孩子，并截掉 [0,j] 位。
	oldSide := bitAt(n.prefix, j)
	old := n.shallowCopy()
	old.prefix = extractBits(n.prefix, j+1, n.skip-j-1)
	old.skip = n.skip - j - 1
	parent.children[oldSide] = old

	if depth+j == keyBits {
		// 新键在分叉位结束：父节点自身携带值。
		parent.hasValue = true
		parent.value = value
	} else {
		newSide := bitAt(key, depth+j)
		parent.children[newSide] = newLeaf(key, depth+j+1, keyBits, value)
	}
	return parent
}

// delete 移除精确匹配 key 的值；返回（新子树根，是否删除过）。
func (n *node[V]) delete(key []byte, depth, keyBits int) (*node[V], bool) {
	if n == nil {
		return nil, false
	}
	full, _ := n.matchSegment(key, depth, keyBits)
	if !full {
		return n, false // 键在段内分歧或耗尽：值不在此子树
	}
	pos := depth + n.skip
	if pos == keyBits {
		if !n.hasValue {
			return n, false
		}
		cp := n.shallowCopy()
		cp.hasValue = false
		var zero V
		cp.value = zero
		return cp.prune(), true
	}
	if pos > keyBits {
		return n, false
	}
	b := bitAt(key, pos)
	child, removed := n.children[b].delete(key, pos+1, keyBits)
	if !removed {
		return n, false
	}
	cp := n.shallowCopy()
	cp.children[b] = child
	return cp.prune(), true
}

// prune 在节点不再携带值时压缩单子树：把分叉位与孩子段并回本节点。
func (n *node[V]) prune() *node[V] {
	if n.hasValue {
		return n
	}
	if n.children[0] != nil && n.children[1] != nil {
		return n
	}
	var child *node[V]
	side := 0
	if n.children[1] != nil {
		child, side = n.children[1], 1
	} else if n.children[0] != nil {
		child = n.children[0]
	} else {
		return nil
	}
	var w bitsBuilder
	w.writeBits(n.prefix, 0, n.skip)
	w.writeBit(side)
	w.writeBits(child.prefix, 0, child.skip)
	return &node[V]{
		skip:     n.skip + 1 + child.skip,
		prefix:   w.bytes(),
		hasValue: child.hasValue,
		value:    child.value,
		children: child.children,
	}
}

// get 精确取值。
func (n *node[V]) get(key []byte, depth, keyBits int) (V, bool) {
	var zero V
	if n == nil {
		return zero, false
	}
	full, _ := n.matchSegment(key, depth, keyBits)
	if !full {
		return zero, false
	}
	pos := depth + n.skip
	if pos == keyBits {
		return n.value, n.hasValue
	}
	if pos > keyBits {
		return zero, false
	}
	return n.children[bitAt(key, pos)].get(key, pos+1, keyBits)
}

// lpm 沿 key 下行，返回沿途最深的带值节点（最长前缀匹配）。
func (n *node[V]) lpm(key []byte, keyBits int) (V, bool) {
	var best V
	found := false
	depth := 0
	cur := n
	for cur != nil {
		avail := keyBits - depth
		lim := cur.skip
		if avail < lim {
			lim = avail
		}
		mismatch := false
		for i := 0; i < lim; i++ {
			if bitAt(key, depth+i) != bitAt(cur.prefix, i) {
				mismatch = true
				break
			}
		}
		if mismatch || lim < cur.skip {
			break // 段内分歧或键在段内耗尽，更深处不可能匹配
		}
		pos := depth + cur.skip
		if cur.hasValue {
			best, found = cur.value, true
		}
		if pos >= keyBits {
			break
		}
		depth = pos + 1
		cur = cur.children[bitAt(key, pos)]
	}
	return best, found
}

// walk 以中序（前序）遍历所有携带值的节点。
func (n *node[V]) walk(fn func(V) bool) bool {
	if n == nil {
		return true
	}
	if n.hasValue && !fn(n.value) {
		return false
	}
	if !n.children[0].walk(fn) {
		return false
	}
	return n.children[1].walk(fn)
}

// ---- Trie 对外方法 ----

// Put 写入或覆盖键前 bits 位处的值（bits 可为 0，即默认路由）。
// key 必须容纳 bits 个比特；查询时仍用全宽地址比特串。
func (t *Trie[V]) Put(key []byte, bits int, value V) {
	t.root = t.root.insert(key, 0, bits, value)
}

// Delete 删除前 bits 位精确匹配的节点值；不存在返回 false。
func (t *Trie[V]) Delete(key []byte, bits int) bool {
	r, removed := t.root.delete(key, 0, bits)
	if removed {
		t.root = r
	}
	return removed
}

// Get 取前 bits 位精确匹配的节点值。
func (t *Trie[V]) Get(key []byte, bits int) (V, bool) {
	return t.root.get(key, 0, bits)
}

// LongestPrefix 返回与 addrBits 匹配的最深节点值。
// addrBits 必须与建表键同宽（同族）。
func (t *Trie[V]) LongestPrefix(addrBits []byte) (V, bool) {
	return t.root.lpm(addrBits, len(addrBits)*8)
}

// Snapshot 返回当前根的不可变视图。持有快照期间，原 Trie 的写入
// 不会影响快照（COW 保证）。
func (t *Trie[V]) Snapshot() *Trie[V] {
	return &Trie[V]{root: t.root}
}

// Walk 遍历当前树中全部值，fn 返回 false 可提前终止。
func (t *Trie[V]) Walk(fn func(V) bool) {
	t.root.walk(fn)
}

// Count 统计带值节点数（测试与诊断用）。
func (t *Trie[V]) Count() int {
	n := 0
	t.Walk(func(V) bool { n++; return true })
	return n
}
