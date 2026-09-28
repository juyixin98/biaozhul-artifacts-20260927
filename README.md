# hlsplan — 本地 HLS 播放列表版本对比与播放计划服务

对本地 HLS 媒体播放列表（m3u8）做版本对比、生成可下载播放计划与连续播放边界。
范围限**非加密分段媒体**：含 `EXT-X-KEY`（METHOD ≠ NONE）的播放列表直接拒绝。
不访问任何外部媒体地址，全部输入由调用方以文本提交。

## 模块关系

```
hlsplan/
  config.py       配置（环境变量加载：DB 路径、时长容差、正文大小上限）
  models.py       Segment / PlaylistSnapshot / FailureCategory；
                  媒体序号、discontinuity 序号在模型层即分列
  parser.py       文本 -> PlaylistSnapshot；结构校验与字节范围隐式偏移继承
  timeline.py     时间与信号内核（NumPy）：起播时刻前缀和、连续播放区间、缺段
  compare.py      两版本对比：窗口前移 / 撤回 / 冲突 / ENDLIST 规则
  planner.py      快照 -> 可下载计划 + 连续播放边界
  jobs.py         SQLite：版本快照表 + 作业状态机（PENDING->DONE|FAILED）
  diagnostics.py  诊断记录、request_id、URI 脱敏
  api.py          FastAPI 验证接口（唯一对外入口）
tests/            pytest 测试与 m3u8 夹具，与实现独立组织
```

调用方向：`api.py` → `parser.py` / `compare.py` / `planner.py` → `timeline.py`；
`jobs.py` 只被 `api.py` 使用；`diagnostics.py` 被各层引用但无反向依赖。

## 算法假设

1. **序号分列维护**：媒体序号由 `EXT-X-MEDIA-SEQUENCE` + 位置推出；
   discontinuity 序号由 `EXT-X-DISCONTINUITY-SEQUENCE` + 已见
   `EXT-X-DISCONTINUITY` 个数推出；两者互不影响。时间线不存储，
   一律由 timeline 内核按时长前缀和推导，且**跨 discontinuity 的区间各自归零**
   （不连续的时间轴不可拼接）。
2. **窗口前移 ≠ 撤回**：序号 < 新版 `media_sequence` 的消失记为 `expired`（正常）；
   序号 ≥ 新版 `media_sequence` 却消失、或原为内容新版变为 `EXT-X-GAP` 占位，
   记为 `retracted`（异常，类别 `SEGMENT_RETRACTED`）。
3. **已见序号冲突单列**：两版都有的序号，URI 不同记 `URI_CONFLICT`，
   时长差超过容差（默认 0.001s，`HLSPLAN_DURATION_TOLERANCE` 可调）记
   `DURATION_CONFLICT`，discontinuity 序号变化记
   `DISCONTINUITY_SEQUENCE_CONFLICT`；不计入撤回或新增。
4. **字节范围隐式偏移**：`EXT-X-BYTERANGE` 缺 `@offset` 时，仅当上一分段是
   **同一 URI** 的子范围才继承其 `offset+length`，否则 `BYTERANGE_UNRESOLVABLE`。
   `EXT-X-MAP` 的 `BYTERANGE` 缺省偏移按 0 处理（实现假设）。
5. **ENDLIST 后追加拒绝**：旧版含 `EXT-X-ENDLIST` 且新版出现更大序号的分段，
   对比结果 `rejected=true`（类别 `APPEND_AFTER_ENDLIST`，HTTP 409）。
6. **缺段表达**：媒体播放列表的分段序号按位置分配，"缺段"以 `EXT-X-GAP`
   占位表达；GAP 分段不进下载计划、不参与连续区间，列入 `missing_sequences`。
7. **单例标签重复**（如两个 `EXT-X-MEDIA-SEQUENCE`）为硬失败
   （`DUPLICATE_TAG`，HTTP 422）。

## 诊断与脱敏

每个响应带 `request_id`（同时写入 `X-Request-Id` 响应头）与 `diagnostics`
列表；每条诊断含 `code`（失败类别或信息码）、`severity`、`message` 和关键状态
（序号、版本、行号等），说明为何接受 / 拒绝 / 无法判定。诊断中的 URI 一律
剥去 query/fragment（token 等敏感参数不落诊断），作业记录与对比结果同样脱敏。

## 依赖版本

Python 3.12；fastapi 0.141.1、uvicorn 0.54.0、numpy 2.5.3、pydantic 2.13.5、
httpx 0.28.1、pytest 9.1.1（见 `requirements.txt`）；SQLite 使用标准库
`sqlite3`（3.45.1）。

## 本地验证

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 单元/端到端测试（33 项）
.venv/bin/python -m pytest

# 2) 启动服务（库路径可用 HLSPLAN_DB_PATH 指定）
.venv/bin/python -m uvicorn hlsplan.api:app --port 8471

# 3) 提交两个版本并对比（窗口前移 + 缺段撤回）
B=http://127.0.0.1:8471
curl -X POST $B/v1/playlists/live/versions \
  -H 'Content-Type: application/vnd.apple.mpegurl' --data-binary @tests/fixtures/window_v1.m3u8
curl -X POST $B/v1/playlists/live/versions \
  -H 'Content-Type: application/vnd.apple.mpegurl' --data-binary @tests/fixtures/missing_v2.m3u8
curl -X POST $B/v1/compare -H 'Content-Type: application/json' \
  -d '{"name":"live","from_version":1,"to_version":2}'
curl -X POST $B/v1/plans -H 'Content-Type: application/json' \
  -d '{"name":"live","version":2}'
curl $B/v1/jobs/<job_id>   # job_id 来自 compare/plans 响应
```

预期判断方式：

| 操作 | 预期 |
|---|---|
| 提交 `window_v1.m3u8` | 201，`version=1`，5 个分段，序号 100–104 |
| 对比 v1→v2（`missing_v2`） | 200；`expired=[100,101]`（窗口前移，非撤回），`retracted=[103]`，`appended=[105,106]` |
| 计划 v2 | `entries` 不含 103；`missing_sequences=[103]`；连续边界 `(102,102)`、`(104,106)` |
| 对比 `endlist_v1`→`endlist_v2_append` | 409，`rejected=true`，诊断含 `APPEND_AFTER_ENDLIST` |
| 提交 `duplicate_tag.m3u8` | 422，`failure_categories` 含 `DUPLICATE_TAG` |
| 提交 `encrypted.m3u8` | 422，含 `ENCRYPTION_UNSUPPORTED`，响应中无密钥 query 参数 |
| 提交 `conflict_v2` 后对比 | 200，`conflicts` 单列 `(103,URI_CONFLICT)`、`(104,DURATION_CONFLICT)`，URI 已脱敏 |

## 测试状态

- 已运行：`.venv/bin/python -m pytest` → **33 passed**（parser 11、timeline 4、
  compare 5、planner 4、api 9）。
- 参考答案（起播时刻、区间边界、继承偏移、expired/retracted/appended 清单）
  均为手工计算后硬编码在断言中，非由被测核心生成。
- 未覆盖（如实标注）：LL-HLS 部分分段（`EXT-X-PART`）、`EXT-X-PROGRAM-DATE-TIME`
  挂钟对齐、多码率主播放列表（master playlist）——均不在本期范围，无对应测试。
