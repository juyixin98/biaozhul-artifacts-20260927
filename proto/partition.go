package proto

// 分区/任务协议：作业被哈希固定到有限个分区（分片）。每个状态变更
// 事务先取该分区的事务级咨询锁，保证同一作业的 DS 计数变更是全序的；
// 不同作业（不同分区）可以在不同数据库连接上并行推进。
//
// 分区只决定锁路由，不决定任务归属——任务始终属于其作业，父→子的
// 因果边永远落在同一作业同一分区内。

// PartitionShards 是固定分区数。取 64 以便哈希均匀且咨询锁空间稀疏。
const PartitionShards = 64

// PartitionFor 返回作业所在分区号 [0, PartitionShards)。
// 使用 FNV-1a 32 位（无外部依赖、跨进程稳定）。
func PartitionFor(job JobID) int {
	const (
		offset32 = uint32(2166136261)
		prime32  = uint32(16777619)
	)
	h := offset32
	for _, b := range []byte(string(job)) {
		h ^= uint32(b)
		h *= prime32
	}
	return int(h % PartitionShards)
}

// AdvisoryLockKey 返回该分区的 PostgreSQL 咨询锁键（事务级）。
// 加上固定高位魔数，避免与系统中其他咨询锁使用者冲突。
func AdvisoryLockKey(job JobID) int64 {
	return int64(0x4453_4e45_5400_0000) | int64(PartitionFor(job))
}
