// Package trie implements a compressed binary prefix tree ("Patricia"/radix
// tree) over fixed-width keys.
//
// The tree is persistent: every mutating call returns a new root and never
// modifies the receiver, because every node along the changed path is cloned
// (siblings and untouched subtrees are shared). A snapshot taken before a
// batch replacement therefore keeps serving lookups while the batch is
// validated and stored, which is what lets a whole batch become visible at
// one single new table version.
//
// One trie is used per address family (see internal/rib); keys are 32 bits
// (IPv4) or 128 bits (IPv6), so families never share nodes.
//
// Position convention (the easy place for an off-by-one to hide): a node
// stores a non-empty compressed label. The label of the root is empty and
// starts at bit position 0; a child reached by following branch bit p has a
// label that starts at bit position p+1, because taking the branch consumes
// one bit.
package trie

// bitLen is the key width.
type bitLen int

const (
	IPv4Bits bitLen = 32
	IPv6Bits bitLen = 128
)

// node is a radix node. segLen is the number of compressed label bits this
// node contributes after its parent edge. A key terminates at this node
// exactly when value != nil; child0/child1 branch on the bit that follows the
// label (the branch bit).
type node[V any] struct {
	segLen int
	label  []byte // segLen bits, left aligned
	value  *V
	child0 *node[V]
	child1 *node[V]
}

// Trie is an immutable handle: a width plus a root.
type Trie[V any] struct {
	bits bitLen
	root *node[V]
}

// New creates an empty trie of the given key width.
func New[V any](bits bitLen) *Trie[V] {
	return &Trie[V]{bits: bits, root: &node[V]{}}
}

func bitAt(b []byte, i int) byte {
	return (b[i>>3] >> (7 - uint(i&7))) & 1
}

// labelBits extracts bitLen bits from key starting at bit offset start,
// left-aligned in a byte slice.
func labelBits(key []byte, start, bitLen int) []byte {
	if bitLen <= 0 {
		return nil
	}
	out := make([]byte, (bitLen+7)/8)
	for i := 0; i < bitLen; i++ {
		if bitAt(key, start+i) == 1 {
			out[i>>3] |= 1 << (7 - uint(i&7))
		}
	}
	return out
}

// commonBits returns how many of the first max bits of label equal
// key[pos:].
func commonBits(label, key []byte, pos, max int) int {
	n := 0
	for n < max {
		if bitAt(label, n) != bitAt(key, pos+n) {
			break
		}
		n++
	}
	return n
}

// KeyLenError is returned when a prefix length is outside the key width.
type KeyLenError struct{ Len, Width int }

func (e *KeyLenError) Error() string { return "trie: key length out of range" }

// KeyWidthError is returned when the key byte slice is narrower than width.
type KeyWidthError struct{ Have, Want int }

func (e *KeyWidthError) Error() string { return "trie: key byte slice too short" }

// Insert sets the value for exact (key, klen). It returns a new trie; the
// receiver is unchanged.
func (t *Trie[V]) Insert(key []byte, klen int, v V) (*Trie[V], error) {
	if klen < 0 || int(t.bits) < klen {
		return nil, &KeyLenError{Len: klen, Width: int(t.bits)}
	}
	if len(key)*8 < int(t.bits) {
		return nil, &KeyWidthError{Have: len(key) * 8, Want: int(t.bits)}
	}
	return &Trie[V]{bits: t.bits, root: insertNode(t.root, key, klen, v, 0)}, nil
}

// insertNode inserts into n whose label starts at key bit position pos.
func insertNode[V any](n *node[V], key []byte, klen int, v V, pos int) *node[V] {
	c := *n // shallow clone; untouched children stay shared

	if c.segLen > 0 {
		maxMatch := c.segLen
		if rem := klen - pos; rem < maxMatch {
			maxMatch = rem
		}
		common := commonBits(c.label, key, pos, maxMatch)
		if common != c.segLen {
			// Divergence inside the label, or the new key ends inside it.
			return splitNode(&c, key, klen, v, pos, common)
		}
		pos += c.segLen
	}

	// Label consumed; node is positioned exactly at pos.
	if pos == klen {
		c.value = &v
		return &c
	}
	b := bitAt(key, pos)
	if b == 0 {
		c.child0 = insertChild(c.child0, key, klen, v, pos)
	} else {
		c.child1 = insertChild(c.child1, key, klen, v, pos)
	}
	return &c
}

// insertChild inserts into the child selected by branch bit branchPos.
func insertChild[V any](child *node[V], key []byte, klen int, v V, branchPos int) *node[V] {
	childPos := branchPos + 1 // taking the branch consumes the branch bit
	if child == nil {
		return &node[V]{
			segLen: klen - childPos,
			label:  labelBits(key, childPos, klen-childPos),
			value:  &v,
		}
	}
	return insertNode(child, key, klen, v, childPos)
}

// splitNode rebuilds n (a shallow clone still holding its original label)
// when the new key diverges from the label at offset common, or terminates
// there (pos+common == klen). pos is the label start.
func splitNode[V any](n *node[V], key []byte, klen int, v V, pos, common int) *node[V] {
	oldBit := bitAt(n.label, common)

	// Existing suffix after the divergence bit.
	rem := &node[V]{
		segLen: n.segLen - common - 1,
		label:  labelBits(n.label, common+1, n.segLen-common-1),
		value:  n.value,
		child0: n.child0,
		child1: n.child1,
	}

	// n becomes the branch point carrying only the common prefix.
	n.segLen = common
	n.label = labelBits(n.label, 0, common)
	n.value = nil
	n.child0, n.child1 = nil, nil

	newDepth := pos + common
	if newDepth == klen {
		n.value = &v
	} else {
		newBit := bitAt(key, newDepth)
		suffix := &node[V]{
			segLen: klen - newDepth - 1,
			label:  labelBits(key, newDepth+1, klen-newDepth-1),
			value:  &v,
		}
		if newBit == 0 {
			n.child0 = suffix
		} else {
			n.child1 = suffix
		}
	}
	if oldBit == 0 {
		n.child0 = rem
	} else {
		n.child1 = rem
	}
	return n
}

// Delete removes the value at exact (key, klen). It returns a new trie and
// whether a value existed.
func (t *Trie[V]) Delete(key []byte, klen int) (*Trie[V], bool, error) {
	if klen < 0 || int(t.bits) < klen {
		return nil, false, &KeyLenError{Len: klen, Width: int(t.bits)}
	}
	if len(key)*8 < int(t.bits) {
		return nil, false, &KeyWidthError{Have: len(key) * 8, Want: int(t.bits)}
	}
	root, removed := deleteNode(t.root, key, klen, 0)
	if root == nil {
		root = &node[V]{}
	}
	return &Trie[V]{bits: t.bits, root: root}, removed, nil
}

func deleteNode[V any](n *node[V], key []byte, klen, pos int) (*node[V], bool) {
	if n == nil {
		return nil, false
	}
	c := *n
	if c.segLen > 0 {
		maxMatch := c.segLen
		if rem := klen - pos; rem < maxMatch {
			maxMatch = rem
		}
		if commonBits(c.label, key, pos, maxMatch) != c.segLen || pos+c.segLen > klen {
			return n, false // label diverges or key ends before the node
		}
		pos += c.segLen
	}
	if pos == klen {
		if c.value == nil {
			return n, false
		}
		c.value = nil
		return compress(&c), true
	}
	b := bitAt(key, pos)
	var ok bool
	if b == 0 {
		c.child0, ok = deleteNode(c.child0, key, klen, pos+1)
	} else {
		c.child1, ok = deleteNode(c.child1, key, klen, pos+1)
	}
	if !ok {
		return n, false
	}
	return compress(&c), true
}

// compress re-compacts a value-less node after deletion: if it has exactly
// one child its own label, the branch bit and the child label are
// concatenated into one node. With no children the node vanishes (the Delete
// wrapper restores an empty root).
func compress[V any](n *node[V]) *node[V] {
	if n.value != nil {
		return n
	}
	if n.child0 != nil && n.child1 != nil {
		return n
	}
	only := n.child0
	var branchBit byte
	if only == nil {
		only = n.child1
		branchBit = 1
	}
	if only == nil {
		return nil
	}
	merged := *only
	// New label = n.label ++ branchBit ++ only.label.
	merged.segLen = n.segLen + 1 + only.segLen
	merged.label = concatBits(n.label, n.segLen, branchBit, only.label, only.segLen)
	return &merged
}

// concatBits builds (aBits from a) ++ one bit ++ (bBits from b).
func concatBits(a []byte, aBits int, bit byte, b []byte, bBits int) []byte {
	total := aBits + 1 + bBits
	out := make([]byte, (total+7)/8)
	put := func(off int, v byte) {
		if v == 1 {
			out[off>>3] |= 1 << (7 - uint(off&7))
		}
	}
	for i := 0; i < aBits; i++ {
		put(i, bitAt(a, i))
	}
	put(aBits, bit)
	for i := 0; i < bBits; i++ {
		put(aBits+1+i, bitAt(b, i))
	}
	return out
}

// Hit is one terminating prefix encountered while descending.
type Hit[V any] struct {
	KeyLen int
	Value  V
}

// MatchChain walks key down the trie and returns every terminating prefix
// that contains key, in descending prefix-length order (longest first). The
// root value (a default route) appears last.
func (t *Trie[V]) MatchChain(key []byte) ([]Hit[V], error) {
	if len(key)*8 < int(t.bits) {
		return nil, &KeyWidthError{Have: len(key) * 8, Want: int(t.bits)}
	}
	var chain []Hit[V]
	n := t.root
	pos := 0
	for {
		if n.segLen > 0 {
			if commonBits(n.label, key, pos, n.segLen) != n.segLen {
				break
			}
			pos += n.segLen
		}
		// The node is now fully consumed: a value terminates exactly here.
		if n.value != nil {
			chain = append(chain, Hit[V]{KeyLen: pos, Value: *n.value})
		}
		if pos == int(t.bits) {
			break
		}
		var next *node[V]
		if bitAt(key, pos) == 0 {
			next = n.child0
		} else {
			next = n.child1
		}
		if next == nil {
			break
		}
		pos++ // branch bit consumed
		n = next
	}
	for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
		chain[i], chain[j] = chain[j], chain[i]
	}
	return chain, nil
}

// Each visits every stored value in tree order (label bits ascending,
// child0 before child1).
func (t *Trie[V]) Each(fn func(klen int, v V) bool) {
	eachNode(t.root, 0, fn)
}

func eachNode[V any](n *node[V], depth int, fn func(klen int, v V) bool) bool {
	if n == nil {
		return true
	}
	depth += n.segLen
	if n.value != nil {
		if !fn(depth, *n.value) {
			return false
		}
	}
	if n.child0 != nil && !eachNode(n.child0, depth+1, fn) {
		return false
	}
	if n.child1 != nil && !eachNode(n.child1, depth+1, fn) {
		return false
	}
	return true
}

// Len returns the number of stored values.
func (t *Trie[V]) Len() int {
	n := 0
	t.Each(func(int, V) bool { n++; return true })
	return n
}
