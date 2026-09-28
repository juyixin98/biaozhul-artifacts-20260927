# 线格式规范：`LZ71` 块帧

小端序。一个块 = 固定 58 字节头 + LZ77 token 负载。

```
偏移   长度  字段
0      4    magic        = 0x4C 0x5A 0x37 0x31 ("LZ71")
4      1    version      = 0x01
5      1    flags        bit0 = DEPENDENT；其余位保留，必须为 0
6      32   dict_digest  解码器字典尾部窗口的 SHA-256；独立块为全 0
38     8    data_len     本块解压后字节数 u64-LE
46     8    payload_len  token 负载字节数 u64-LE
54     4    crc32        payload 的 IEEE CRC-32（zlib/PNG 多项式）u32-LE
58     ..   payload      LZ77 token 流
```

## Token 流

| 字节 | 含义 |
|---|---|
| `0x00` `b` | 输出字面量字节 `b`（2 字节） |
| `0x01` `d_lo d_hi len` | 从 `d = u16-LE`（**1 基**）字节之前复制 `len+3` 字节 |
| `0xFF` | 流结束（必须且仅出现一次，其后不允许余字节） |

* `d ∈ [1, 4096]`：`d=0`、`d > 已产生字节数(含字典)`、`d > 4096` 都是 `input_error`。
* `len+3 ∈ [3, 258]`。
* `len+3 > d` 合法：自重叠复制，产生以 `d` 为周期的重复（RLE 即 d=1）。
  语义定义为**逐字节**：`for k in 0..length { out.push(out[out.len()-d]) }`。

## 独立块 vs 依赖块

* 独立块：`flags=0`，`dict_digest` 全 0，必须在空字典下解码。
* 依赖块：`flags=1`，`dict_digest = SHA256(prev_output_tail_4096)`。
  接收方计算当前字典尾窗摘要与之比较；不符为 `state_conflict`
  （线数据本身没坏，是接收方状态不对：缺前块 / 链断裂 / 前块不一致）。

## 解码前安全策略（只看头部，不按声明分配）

按以下顺序拒绝：

1. 长度 `< 58`、magic/version/保留位错误 → `input_error`；
2. 独立块携带非零摘要 → `input_error`；
3. `payload_len == 0` 或与帧实际负载长度不符 → `input_error`；
4. payload CRC-32 不符 → `input_error`；
5. `data_len > 8 MiB` → `resource_exhausted`；
6. `data_len > 100 * payload_len` → `resource_exhausted`。

token 解码期间再做逐 token 约束（距离窗口、不得超产 `data_len`），因此：

* 一个 1 GiB 的 `data_len` 声明在第 5 步即被拒，解码器不会见到它、更不会按它分配；
* 真实输出由 token 数决定，且倍率门已把总输出夹在 8 MiB / 100× 内；
* 链解析最多跟随 64 个 `prev_id`，超出为 `resource_exhausted`。

## CRC-32 实现约定

反射多项式 `0xEDB88320`，初值 `0xFFFF_FFFF`，终值异或 `0xFFFF_FFFF`
（与 zlib/PNG/gzip 一致）。校验向量：`crc32("123456789") = 0xCBF43926`。

## 存储布局（非线格式，仅本地持久化）

```
<store>/manifest.json        {blocks: {id: BlockMeta}, tip, next_seq}
<store>/blocks/<id>.lz71     原始块帧
```

提交顺序：写 `*.tmp` → `rename` 成帧 → 重写 manifest，保证 manifest 不会指向未写完的帧。
