// Package hashx pins the exact hash function used by the router.
//
// The digest is FNV-1a 64-bit (Fowler–Noll–Vo) followed by a splitmix64
// finalizer. The finalizer matters: vnode keys share long prefixes
// ("vnode-v1|<memberID>#") and differ only in a short decimal suffix, and
// plain FNV-1a has poor avalanche over such inputs, clustering a member's
// vnodes on the circle and badly skewing realized traffic share (measured:
// weight 1 member receiving ~7% instead of ~25% on a 10k corpus). splitmix64
// (Stafford's Mix13 constants) restores full avalanche while staying in the
// standard-library-only dependency budget. This is recorded as hash version
// v1; two independent key spaces are derived by domain prefixes so that flow
// keys and vnode identities can never collide into the same ordinal
// interpretation:
//
//   - Flow:  splitmix64(fnv1a64("flow-v1|" + canonical five-tuple key))
//   - VNode: splitmix64(fnv1a64("vnode-v1|" + memberID + "#" + replicaIndex))
//
// The versioned prefixes ("-v1") allow a future hash change to be introduced
// deliberately rather than by accident; changing the algorithm is a ring-wide
// migration and is meant to be explicit.
package hashx

import (
	"hash/fnv"
	"strconv"
)

const (
	flowPrefix  = "flow-v1|"
	vnodePrefix = "vnode-v1|"
)

// FNV1a64 returns the raw FNV-1a 64-bit digest of data.
func FNV1a64(data []byte) uint64 {
	h := fnv.New64a()
	_, _ = h.Write(data)
	return h.Sum64()
}

// SplitMix64 applies the splitmix64 finalizer (Stafford Mix13 constants) to h.
// It is its own inverse-free bijection and provides strong avalanche.
func SplitMix64(h uint64) uint64 {
	h += 0x9e3779b97f4a7c15
	h = (h ^ (h >> 30)) * 0xbf58476d1ce4e5b9
	h = (h ^ (h >> 27)) * 0x94d049bb133111eb
	return h ^ (h >> 31)
}

// Hash64 returns splitmix64(FNV-1a-64(data)).
func Hash64(data []byte) uint64 {
	return SplitMix64(FNV1a64(data))
}

// FlowHash hashes a canonical flow key.
func FlowHash(canonicalKey string) uint64 {
	return Hash64(append([]byte(flowPrefix), canonicalKey...))
}

// VNodeHash hashes one virtual node replica of a member.
func VNodeHash(memberID string, replica int) uint64 {
	b := make([]byte, 0, len(vnodePrefix)+len(memberID)+12)
	b = append(b, vnodePrefix...)
	b = append(b, memberID...)
	b = append(b, '#')
	b = strconv.AppendInt(b, int64(replica), 10)
	return Hash64(b)
}
