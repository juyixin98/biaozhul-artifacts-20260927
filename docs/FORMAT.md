# RBS1 二进制格式规范

版本：`0x00010000`（major=1, minor=0）。所有整数为**小端字节序**。

## 1. 文件布局

```
偏移        长度      字段
0           4         magic        = ASCII "RBS1" (0x52 0x42 0x53 0x31)
4           4         version      = 0x00010000
8           4         container_count (u32)
12          4         header_crc32c  对字节 [0, 12) 的 CRC32C（Castagnoli）
16          N         directory      container_count 个 16 字节目录项（见 §2）
16+N        M         payloads       容器负载，顺序与目录一致，连续无空隙
16+N+M      4         body_crc32c    对字节 [16, 16+N+M) 的 CRC32C
```

- `N = container_count * 16`；空集合时 `N = M = 0`，文件恰好 20 字节。
- CRC32C 多项式 0x1EDC6F41（反射 0x82F63B78），初值/终值均异或 0xFFFFFFFF，
  与 iSCSI/ext4 使用的 CRC32C 相同。检查向量：`crc32c("123456789") = 0xE3069283`。
- 头部 CRC 只覆盖头 12 字节（不含自身）；体 CRC 覆盖“目录 + 全部负载”，
  尾部 4 字节存放其值。

## 2. 目录项（16 字节）

| 偏移 | 长度 | 字段 | 约束 |
|---|---|---|---|
| 0 | 2 | `key`（高 16 位分片号） | 所有项严格升序、唯一 |
| 2 | 1 | `tag`（容器类型） | `1 = array`，`2 = bitmap`，其余拒绝 |
| 3 | 1 | `reserved` | 必须为 `0` |
| 4 | 4 | `payload_len` | array：偶数；bitmap：必须为 8192 |
| 8 | 4 | `offset` | 相对体区起点（绝对偏移 16）的负载偏移 |
| 12 | 4 | `cardinality` | 声明基数；必须与负载实际基数一致 |

偏移约束：首项 `offset == 目录长度 N`；之后每项 `offset == 上一项 offset + 上一项
payload_len`；末项末端恰为 `N + M`。即负载必须与目录同序、连续、无重叠；
任何越界（`offset < N`、末端超出体区）都报 `corrupt_bad_offset`，
而“声明末端超出实际文件长度”报 `corrupt_truncated`。

## 3. 容器负载

### 3.1 array（tag = 1）

- `payload_len / 2` 个 `u16`，为容器内低 16 位值；
- 必须**严格升序且唯一**（乱序/重复报 `corrupt_array_not_sorted`，附首个违例下标）；
- 基数 `n = payload_len/2` 必须 `≤ 4096`，且与目录项 `cardinality` 相等；
  `n > 4096` 报 `corrupt_threshold_violation`。

### 3.2 bitmap（tag = 2）

- 恰好 8 192 字节，即 1 024 个小端 `u64`；
- 位 `i`（0 ≤ i < 65 536）置位表示低 16 位值 `i` 存在：字下标 `i/64`，位 `i%64`；
- `popcount` 必须 `> 4096`（否则该容器应规范化为 array，报
  `corrupt_threshold_violation`），且与目录项 `cardinality` 完全相等；
- 不存在“未使用高位”概念：容器逻辑上覆盖完整 65 536 值，位图内无保留位，
  因此 `popcount == cardinality` 即为充分校验（不需要额外的 trailing-bits 清零检查）。

## 4. 校验顺序与错误代码

解码器按以下顺序给出错误（先到先得，保证分类稳定可断言）：

1. 长度不足 20 字节 / 声明范围超出文件 → `corrupt_truncated`
2. 魔数不符 → `corrupt_bad_magic`
3. 版本不符 → `corrupt_unsupported_version`
4. 头部 CRC 不符 → `corrupt_header_checksum`
5. 逐项目录：标签/保留字节、键序、负载长度、偏移边界
6. 结构长度不足（截断优先于体 CRC）→ `corrupt_truncated`
7. 体 CRC 不符 → `corrupt_body_checksum`
8. 偏移连续性/重叠 → `corrupt_bad_offset`
9. 逐负载：数组有序唯一与阈值、位图 popcount 与阈值、两边的基数一致性

完整错误码清单（Rust `CodecError` ↔ HTTP 422 代码 ↔ `rbtool`/Python 输出同源）：

```
corrupt_truncated
corrupt_bad_magic
corrupt_unsupported_version
corrupt_header_checksum
corrupt_body_checksum
corrupt_unknown_container_tag
corrupt_keys_not_sorted
corrupt_bad_offset
corrupt_bad_payload_length
corrupt_array_not_sorted
corrupt_threshold_violation
corrupt_cardinality_mismatch
corrupt_trailing_bits        # 保留代码（当前布局下由基数校验覆盖）
```

## 5. 设计说明

- **为什么阈值是 4096**：array 在 4096 元素时占 8 192 字节，与 bitmap 恒占的
  8 192 字节相等；低于阈值数组更省，高于阈值位图更省且支持 O(1024) 字级运算。
- **为什么目录放在负载前**：单遍顺序写入、单遍顺序校验；偏移全部相对体区起点，
  便于流式拼接与定位。
- **为什么同时有头/体两个 CRC**：头部（尤其 container_count）被篡改会改变后续
  所有解析边界，单独保护可在解析任何目录字节前拒绝；体 CRC 覆盖实际数据。
