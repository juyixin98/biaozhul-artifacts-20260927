# 边界语义（Boundary Semantics）

本文件明确分析器在每个模糊边界上的**确切行为**、对应诊断代码与理由。
行为与 `tests/` 中断言一一对应。

## 1. 字节流成帧

* **初始锁同步**：从扫描起点向量化查找 `0x47`；候选必须在 `+188`、`+376`
  （`sync_confirm_packets=3`）处同样为 `0x47` 才确认。单独一个 `0x47`
  或非 188 周期的假候选不会锁定。
* **前置垃圾**：确认锁定前跳过的字节计入 `framing.bytes_skipped`，发出
  `sync_recovered`（warning，含 `skipped_bytes`）；随后逐 PID 的 CC 基线
  **重新建立**（`cc_reset_after_sync`），不为无法解析的垃圾段虚构丢包。
* **有限扫描恢复**：在 `max_sync_scan_bytes`（默认 4096）内没有候选能确认时，
  发出 `sync_recovery_failed`（error），作业结果 `framing.fatal` 非空，
  `/validate` 判为 `indeterminate`。不做无限扫描，也不按固定 PID 猜测对齐。
* **流中失步**：已锁定后遇到非 `0x47`，发 `sync_lost` 并重新进入**有界**
  扫描；恢复成功再发 `sync_recovered`。
* **结构损坏的包**：适配字段长度越界、`adaptation_field_control=0`（保留值）
  发 `packet_parse_error`（error），按字节步进后重新同步。
* **尾部残片**：不足 188 字节的尾部不解析、不补零，发 `truncated_tail`（info），
  字节数计入 `framing.leftover_bytes`。

## 2. 包头门控

* **TEI（transport_error_indicator）**：发 `transport_error_indicator`（error）。
  CC 仍照常检查（计数器是传输层信号），但**净荷绝不**送入表/PES/PCR 重组。
* **加扰（transport_scrambling_control != 0）**：发 `scrambled_packet`（info），
  净荷不解释；CC 统计照常。
* **空包 PID 0x1FFF**：参与成帧与 CC 统计，不进入任何重组。

## 3. 连续计数器（逐 PID 独立）

参考 ISO/IEC 13818-1 §2.4.3.2。状态机判定为互斥的一类 verdict：

| 情况 | verdict | 诊断 | 是否计丢包 |
| --- | --- | --- | --- |
| PID 首包 / 失步重建后首包 | `first` / `reset_after_sync` | `cc_reset_after_sync` | 否 |
| CC == 期望，带负载 | `ok` | — | 否 |
| adaptation-only，CC 重复上个负载 CC | `af_only_repeat` | —（合法，静默） | 否 |
| adaptation-only，CC 却 +1 | `af_only_increment` | `af_only_cc_increment`（info，容忍） | 否 |
| CC 重复且负载字节完全相同 | `duplicate` | `duplicate_packet`（warning） | 否 |
| CC 重复但负载字节不同 | `stall` | `cc_stall_without_duplicate`（error） | 否 |
| CC 跳变（带负载） | `lost` | `continuity_lost`（error，含 `missing`） | 是（按 4 位回绕差值） |
| 适配字段 `discontinuity_indicator=1` | `discontinuity_declared` | `discontinuity_indicator`（info） | **否** |

要点：

* **“有无负载对计数器增量影响按协议处理”**：只有携带负载的包推进 CC；
  adaptation-only 不推进。重复 CC 是其合法行为。
* **discontinuity 标记与真实缺包严格区分**：显式标记表示发送方声明“这里可能不连续”，
  分析器接受其 CC 值、重置该 PID 的比较基线、记录一次 `declared_discontinuities`，
  **不**在 `lost` 中加任何数。真实 CC 跳变才计 `missing`。
* 重复包（相同字节）不二次喂给重组，因此 PES/PSI 不会被同一字节污染。
* 同步丢失期间不做 CC 推断：该段可能是任意垃圾，基线在恢复后重建。

## 4. PSI（PAT/PMT）

* **PID 角色不预设**：PID 0 无条件按 PSI 处理；其它 PID 只有在**当前 PAT**
  把它列为某节目的 PMT PID 后才按 PSI 处理。在 PAT 出现前，PMT PID 上的字节
  只是“未知 PID”，只得到 CC/统计处理（见 not-current / 早到 PMT 的边界）。
* **pointer_field**：PUSI 包首字节为 pointer_field。`pointer>0` 表示该包前部
  承载**上一节的尾部**：组装器先把这段尾字节并入旧缓冲并尝试完成旧节，
  然后才开始新节。pointer 越界发 `pointer_field_out_of_range`。
* **跨包重组**：节在任意包边界续接；`section_length` 声明的总长度达到后整节取出。
  尚不足 3 字节或未到声明长度时保持缓冲；输入结束仍不完整发 `section_incomplete`。
* **长度上限**：超过 `max_section_bytes`（默认 4096）发 `section_oversized` 并丢弃。
* **CC 缺包**：PSI PID 一旦发生真实 CC 丢包/非法复用，未完成的半节立即丢弃，
  发 `section_gap`，避免把跨缺口的字节拼成一个“看似完整”的节。
* **CRC-32**：对“table_id … CRC 前一字节”计算 MPEG-2 CRC（多项式 0x04C11DB7，
  初值 0xFFFFFFFF）。不匹配发 `table_crc_error`（error），**该节永不发布**。
* **current_next_indicator**：为 0 的节发 `table_not_current`（info）但不应用。
* **未解析表类型**：CRC 通过但 table_id 不是 PAT/PMT 时发 `unsupported_table`（info）。

### 原子版本更新

* 节必须同时满足：CRC 正确 **且** `current_next=1`，才进入发布判定。
* 与当前版本（version + 内容）完全相同的重传 → `pat_version_repeat` /
  `pmt_version_repeat`，不产生新映射。
* version 变化（或同 version 内容变化）→ 一次赋值完成原子替换，发
  `pat_version_switch` / `pmt_version_switch`，事件上下文携带 old/new 版本与
  old/new 节目或流列表。
* PAT 切换时，新 PAT 不再引用的旧 PMT PID 在**同一步**裁剪（`retired_pmt_pids`），
  调用方不可能观察到“新 PAT + 旧 PMT 流”的中间态。

## 5. 受限 PES 重组

“受限”的含义是明确的功能边界，而非偷懒：

* **只处理 PMT 声明的 ES PID**。未知 PID 上即便出现 `0x000001` 也不重组。
* **只解析 PES 封装**：起始码前缀、stream_id、PES_packet_length、
  PTS/DTS、负载边界与字节数；**绝不解码**任何媒体编码（H.264/AAC 载荷视为不透明字节）。
* 单 PID 同时只保留一个打开的 PES；新 PUSI 关闭旧的（`close_reason=next_pusi`），
  输入结束关闭为 `end_of_input`。
* 起始包不以 `0x000001` 开头 → `pes_bad_start_code`（error），记录标记 `dropped`。
* **长度语义**：
  * `PES_packet_length != 0`：按声明长度精确裁剪 ES 字节。起始包中紧跟首部的
    TS 填充字节（适配字段填充）会在首部解析后按声明长度剔除；完整性
    `complete = 已收 ES 字节 >= 声明 ES 字节`。
  * `PES_packet_length == 0`（视频合法）：没有声明结尾，只能由下一 PUSI 界定，
    或在输入末尾以一个**短净荷包**结束。此时短包内的 TS 填充无法与 ES 字节区分，
    `payload_bytes` 是**上界**——这是协议固有的歧义，文档与记录都如实标注。
* **缺口不静默拼接**：ES PID 真实 CC 丢包时，打开的 PES 标记 `gap=true` 并发
  `pes_gap`（warning）；报告里能区分“完整 PES”和“含缺口 PES”。
* **容量上限**：重组缓冲受 `max_pes_payload_bytes`（默认 256 KiB）限制，
  超出部分不收，发一次 `pes_payload_capped`（warning），记录 `capped=true`。
* **无 PES 可选头的 stream_id**（0xBC/0xBE/0xBF/0xF0/0xF1/0xF2/0xF8/0xFF）：
  负载起点按 6 字节（无 flags/header_data_length）处理。
* ES PID 上没有先见到 PUSI 就出现连续数据：发一次
  `pes_midstream_data_without_start`（warning），丢弃直到下个 PUSI。

## 6. PCR / 时序内核

* PCR 以 27 MHz 整数 ticks 存储（`base*300 + extension`），逐 PID 维护序列。
* 统计用 NumPy 计算：平均/最大 PCR 间隔（ms）、抖动（相邻间隔相对均值的最大
  绝对偏差，µs）、传输速率（首尾 PCR 间字节距离 / PCR 时间跨度）。
* **PCR 回退**是信号错误而非“负间隔”：发 `pcr_backwards`（warning），
  并清空该 PID 序列、以该点为新基线重新统计，避免污染后续均值。

## 7. 诊断与脱敏

* 每条事件带 `record_id`（作业 id 或请求 id）、`seq`、`code`、`severity`、
  `pid`、`offset` 与说明原因的 `context`。
* 任何 `bytes/bytearray/memoryview` 上下文都渲染为 `<bytes:N>`；
  字典/列表递归脱敏；字符串超过 200 字符截断。诊断可以安全落日志/入库。
* 报告 JSON 内嵌事件受 `report_event_limit` 限制；**全部**事件始终持久化在
  SQLite，可经 `/jobs/{id}/events` 分页取回，不会因报告截断而丢失。
