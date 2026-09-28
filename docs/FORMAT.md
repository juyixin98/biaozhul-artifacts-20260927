# LZ7B v1 线格式

所有多字节定长整数为**大端**；Token 流内的整数为无符号 **LEB128**。

## 块 = 30 字节定长头 + Token 载荷

| 偏移 | 长度 | 字段 | 说明 |
|---|---|---|---|
| 0 | 4 | magic | `4C 5A 37 42`（`"LZ7B"`） |
| 4 | 1 | version | 固定 `1` |
| 5 | 1 | frame type | `0` 独立块，`1` 依赖块 |
| 6 | 4 | index | 流内块序号，从 0 起，必须连续 |
| 10 | 8 | prev digest | 依赖块=前置字典摘要；独立块必须为 `0` |
| 18 | 4 | payload crc | **载荷**的 CRC-32/IEEE（不含头） |
| 22 | 8 | decompressed len | 声明的解压字节数 |
| 30 | … | payload | Token 流 |

不变量（`BlockHeader::decode` 全部强制）：

- magic/version/frame type 必须合法；
- 独立块 `prev_digest==0`，依赖块 `prev_digest!=0`；
- `decompressed_len ≤ 1_048_576`（`MAX_OUTPUT`）；
- 载荷长度 `≤ 65_536`（`MAX_PAYLOAD`）；
- `decompressed_len ≤ 200_000 × payload_len`（`MAX_EXPANSION`，绝对上限之外的
  纵深防御；诚实编码器的单字节重复块可达数万倍，故取值留足余量）；
- 载荷 CRC 必须匹配（`crc_mismatch`，input 类）。

## Token 流

| 标签 | 编码 | 语义 |
|---|---|---|
| `0x00` | LEB128 `run_len`，随后 `run_len` 个原文字节 | 字面量段 |
| `0x01` | LEB128 `distance`，LEB128 `length-3` | 回溯匹配 |
| `0x02` | — | 结束；有且仅有一个，必须是最后一个字节 |

匹配约束：

- `distance ≥ 1`，且不得超过“字典 + 已产出”历史长度（`bad_distance`，input 类）；
- `3 ≤ length ≤ 65_538`（`MIN_MATCH..=MAX_MATCH`，delta 以 u16 承载）；
- `distance ≤ 4096`（`WINDOW_SIZE`）：固定格式硬性约束，**解码器同样强制**。
  即使历史中存在更久远的字节，引用窗口之外也是 `bad_distance`；编码器只
  引用窗口内历史。

**重叠语义**：当 `length > distance` 时，源区间与目标区间重叠，部分源字节在
复制开始时还不存在。正确行为是逐字节复制——把输出周期性地重复前 `distance`
个字节（等价 DEFLATE）。例如历史尾部为 `A`，`(distance=1, length=5)` 产出
`AAAAA`。实现对增长中的历史逐字节索引，从不整体切片。

LEB128 为严格规范形式：最多 10 字节、第 10 字节仅允许 1 个值位、多字节编码的
末组不得为 0（拒绝非规范编码）。

## 字典摘要（链式绑定）

```
prev_digest = FNV1a64( "LZDICT:1\n" ‖ u32le(index) ‖ u64le(dict.len()) ‖ dict )
```

- `index` 是该依赖块自身序号（即“它前面已有 index 个块”）；
- `dict` 是前置全部已解压字节的最后 `WINDOW_SIZE` 字节；
- 域分隔前缀与长度帧防止原始载荷与真摘要碰撞，也把摘要绑定到块序号。

链式会话 (`ChainSession::decode_raw`) 的判定顺序：

1. 头部与 CRC（input 类错误先返回）；
2. `index == next_index`（否则 `index_gap`，state 类）；
3. 帧类型与 `prev_digest` 等于会话重算值（否则 `digest_mismatch`，state 类）；
4. 载荷解码（`bad_distance`/`bad_token_stream`/`length_mismatch` 等 input 类，
   或 `output_cap_exceeded` 资源类）；
5. 成功后滚动字典：`dict = (dict ‖ decoded)` 的最后 4096 字节。

任一步失败，会话状态保持不变（第 4 步对字典快照解码）。

## 错误分类契约

| 类别 | 典型 code | HTTP |
|---|---|---|
| input（不可信数据） | bad_magic, bad_version, bad_frame_type, bad_varint, bad_distance, bad_match_length, bad_token_stream, crc_mismatch, length_mismatch, bad_string, bad_request | 400 |
| state（时序冲突） | missing_predecessor（以 index_gap 体现）, digest_mismatch, index_gap, already_exists, not_found | 409（`not_found` 例外为 404；响应体仍标注 category=state） |
| resource（触发限额） | output_cap_exceeded, payload_cap_exceeded, expansion_cap_exceeded, total_cap_exceeded（磁盘聚合）, stream_output_cap_exceeded（整链解压聚合，默认 16 MiB） | 413 |
| compute（本地环境） | io（带来源标签 store/block-file/index） | 500 |
