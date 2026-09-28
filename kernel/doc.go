// Package kernel 实现 Dijkstra-Scholten（DS）式终止检测状态机。
//
// # 模型
//
// 每个任务是一个节点；父任务派生子任务时建立一条因果边（消息）。
// 网络从根节点启动，根节点初始即为 engaged（已介入）且无入边。
//
// 经典 DS 判据（以“每个非根节点恰有一条入边、入边来自其派生者”特化）：
//
//   - deficit(P) = P 已派生但尚未被清偿的子任务数（未确认因果边数）。
//   - 节点 passive：自身的工作已完成并被确认（收到报告）。
//   - 节点 settle：自身 passive 且 deficit=0 时，向父发送唯一一条信号，
//     父 deficit--，结算级联向上传播。
//   - 根判定全局终止：根 passive 且 deficit=0 —— 此刻不存在任何在途任务、
//     未确认因果边，所有节点都已结算。
//
// 空本地队列绝不等于终止：队列虽空，只要存在 ready/claimed 任务
// （在途）或 engaged 节点（未清偿边），根都不会被结算。
//
// 每次任务转移携带唯一 TaskID；报告凭 TaskID+LeaseID 识别，重复确认
// 只产生一条 duplicate_ignored 事件，不再次结算、不减少任何计数。
package kernel

// Version 是核心机制的语义版本，随证据（Evidence）与 /version 输出暴露，
// 便于解释“由哪个版本、在哪一步”产生结论。
const Version = "ds-kernel/1.0.0"
