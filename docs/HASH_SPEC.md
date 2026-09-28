# 哈希规范（Rust 与 Python 参考实现共用）

所有运算为 64 位 wrapping，小端序存储。

## 原语

```
mix64(x):                       splitmix64 finalizer
  x ^= x >> 30
  x *= 0xBF58476D1CE4E5B9
  x ^= x >> 27
  x *= 0x94D049BB133111EB
  x ^= x >> 31

keyed64(bytes, domain):         分块 splitmix 混合（低模数下仍有良好雪崩）
  h = mix64(domain ^ (len(bytes) << 56) ^ 0xD1B54A2E7E6D73B3)
  for 每个 8 字节小端块 chunk:
      h = mix64(h ^ mix64(chunk + GAMMA))
  尾部不足 8 字节时按小端数 tail:
      h = mix64(h ^ mix64(tail + 0x9E3779B9))
  return mix64(h)
```

> 不使用 FNV-1a：其乘法递推在短而相近的键上低位雪崩很差（`aa/bb/cc` 模小 m 高度重合），
> 会产生大量退化超边。

## 派生值

```
GAMMA      = 0x9E3779B97F4A7C15
FP_DOMAIN  = 0xF9B0DC5E3A71D2C3

vertex_hash(seed, i, key) = keyed64(key, seed ^ mix64(GAMMA + i)), i ∈ {0,1,2}
fingerprint(seed, key)    = keyed64(key, seed ^ FP_DOMAIN)
attempt_seed(base, k)     = base + k                          (wrapping, k 从 0 起)
```

顶点哈希与指纹使用不同域分隔常数，互不相关。

## 构建参数

```
n = 去重后键数
m = 0                        (n = 0，空索引)
m = max(3, 2n)               (1 ≤ n ≤ 64，小集合)
m = ceil(1.5 · n)            (65 ≤ n ≤ 2048，过渡档)
m = ceil(1.23 · n)           (n > 2048)
边 e(key) = [vertex_hash(seed,0,key) mod m, ... i=1, i=2]
```

小集合的随机超图行为偏离渐近阈值（部分 `(n, m)` 组合结构上不可剥离），剥离成功阈值收敛较慢，因此按规模分档提高顶点预算。

三个顶点不两两互异（退化边）时，该种子整次作废，进入下一次尝试。

## 剥离（peeling）

1. `degree[v]` 初始化为覆盖顶点 v 的边数；所有 `degree == 1` 的顶点入队
2. 出队一个度仍为 1 的顶点 v，找到它唯一未删除的边 e，记录 `(e, v)` 到剥离栈，删除 e，并将其三个顶点度数减 1；新降为 1 的顶点入队
3. 栈长度为 n 即成功；否则剩余图含 2-核，本次种子失败

## 赋值（assignment）

逆剥离栈处理，槽位计数器从 0 单调递增（单调发放保证最小、确定）：

```
g[所有顶点] = 0
for (edge e, critical vertex cv) in reverse(stack):
    slot = next_slot++
    g[cv] = (slot - g[other1] - g[other2]) mod n
```

## 查询

```
slot = (g[v0] + g[v1] + g[v2]) mod n
accept 当且仅当 fps[slot] == fingerprint(seed, key)
```

集合外键可以落到任意候选槽位，但只有指纹匹配才会被接受；64 位指纹的误报概率约为 2⁻⁶⁴。
