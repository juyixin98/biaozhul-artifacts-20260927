package hashring

import (
	"hash/fnv"
	"strconv"
)

// Hashing design (deterministic, stdlib-only):
//
// FNV-1a 64 alone is unsafe for pair affinities: if two member IDs differ in
// only their last byte (e.g. "b" vs "d"), then FNV("...|mb") and
// FNV("...|md") differ by a fixed constant because FNV-1a is linear in the
// final XOR-multiply step. Comparing such scores across many buckets shows
// a systematic, correlated skew. We therefore hash the two inputs
// INDEPENDENTLY and combine them with a splitmix64-style avalanche mixer,
// which makes affinities of near-identical member IDs statistically
// independent while staying fully deterministic across platforms.
//
// Flow->bucket hashing remains plain FNV-1a with a domain prefix; there is
// no near-key comparison problem there (full tuple strings are compared for
// equality, not ordering), and keeping it simple makes the mod-N baseline
// comparison transparent.

func fnv64String(s string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(s))
	return h.Sum64()
}

// mix64 is the public splitmix64 finalizer (Stafford, Mix13 variant).
func mix64(z uint64) uint64 {
	z ^= z >> 30
	z *= 0xbf58476d1ce4e5b9
	z ^= z >> 27
	z *= 0x94d049bb133111eb
	z ^= z >> 31
	return z
}

// affinity scores a (bucket, member) affinity. The member hash is rotated
// before XOR-combining so that, even if a component is 0/small, the two
// inputs both influence the mixer state.
func affinity(bucket int, memberID string) uint64 {
	hb := fnv64String(strconv.Itoa(bucket))
	hm := fnv64String(memberID)
	combined := hb ^ ((hm << 23) | (hm >> 41))
	return mix64(combined)
}

// hashFlow maps a canonical flow key onto a bucket index in [0, n).
func hashFlow(key string, n int) int {
	h := fnv.New64a()
	h.Write([]byte("flow|"))
	h.Write([]byte(key))
	return int(h.Sum64() % uint64(n))
}
