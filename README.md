# hlsdiff — 本地 HLS 播放列表版本对比与播放计划服务

范围:非加密分段媒体(`EXT-X-KEY METHOD` 非 `NONE` 直接拒绝)。所有输入为本地文本/夹具,不访问外部媒体地址。

## 模块关系

```
hlsdiff/
  errors.py     失败类别枚举(FailureCategory)+ PlaylistParseError,全链路按类别断言
  models.py     Segment / Playlist / CompareReport / PlaybackPlan 等数据模型
  parser.py     M3U8 解析 → Playlist;字节范围隐式偏移在此解析为绝对偏移
  timeline.py   时间与信号内核(NumPy):媒体序号、discontinuity 序号、时间线三个维度分别维护
  compare.py    相邻版本对比 → CompareReport(accept / conflict / reject)
  plan.py       可下载计划 + 连续播放边界(基于 timeline 内核)
  store.py      SQLite:快照版本(snapshots)与作业状态机(jobs: PENDING→DONE/FAILED)
  api.py        FastAPI 验证接口;每响应带 request_id 与关键状态
tests/          pytest 套件 + tests/fixtures/*.m3u8 合成夹具
```

依赖方向:`parser → models/errors`;`timeline → models`;`compare → models/errors`;
`plan → timeline/models`;`store → models`;`api → compare/parser/plan/store`。
核心逻辑(parser/timeline/compare/plan)不依赖 FastAPI,可独立测试。

## 算法假设

1. **三个维度分别维护**:媒体序号标识分段、discontinuity 序号标识信号不连续组、
   时间线是累积时长的展示维度;任何维度不由其他维度推导。
2. **窗口前移 ≠ 撤回**:旧版本中序号 `<` 新版本 `MEDIA-SEQUENCE` 的分段记为窗口
   前移(正常滚动);序号 `>=` 新版本 `MEDIA-SEQUENCE` 却缺失才判为内容撤回(reject)。
3. **冲突单列**:同一已见序号 URI 不同或时长差 > 1ms 记为冲突,判定 conflict,
   与 reject 分开报告。
4. **结束后追加拒绝**:旧版本含 `EXT-X-ENDLIST` 且新版本出现新序号 → reject
   (`append-after-endlist`);单文档内 ENDLIST 后的内容在解析期即拒绝
   (`content-after-endlist`)。
5. **字节范围隐式偏移**:`EXT-X-BYTERANGE:n`(无 `@o`)必须与前一分段同 URI,
   偏移继承为前一分段 `offset+length`;否则解析失败
   (`byte-range-offset-unresolvable`)。
6. **discontinuity 序号变化只报告**,不影响 accept/reject。
7. 单例标签(MEDIA-SEQUENCE、TARGETDURATION、VERSION、DISCONTINUITY-SEQUENCE、
   PLAYLIST-TYPE)重复出现 → `duplicate-tag`。
8. 诊断脱敏:日志与冲突报告中的 URI 去掉查询串(`?` 后内容替换为 `<redacted>`)。

## 本地验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q        # 期望:37 passed
```

预期判断方式(关键用例):

| 夹具 | 验证点 | 预期 |
|---|---|---|
| window_v1 → window_v2 | 窗口前移 | accept,window_advanced=[0,1],appended=[5,6] |
| window_v1 → missing_segment_v2 | 缺段 | reject,retracted=[4],原因含 `content-retracted` |
| window_v1 → conflict_v2 | 已见序号换 URI | conflict,conflicts 单列 seq=3 |
| ended_v1 → ended_v2_appended | 结束后追加 | reject,原因含 `append-after-endlist` |
| append_after_endlist.m3u8 | 文档内 ENDLIST 后内容 | 解析拒绝 `content-after-endlist` |
| duplicate_tag.m3u8 | 重复单例标签 | 解析拒绝 `duplicate-tag` |
| byterange_v1.m3u8 | 隐式偏移继承 | 绝对偏移 (0,100),(100,150),(400,120) |
| discontinuity_v1.m3u8 | 断点 | dseq=[5,5,6,6],边界 index=2 @ 8.0s |

启动服务手工验证:

```bash
.venv/bin/uvicorn hlsdiff.api:app --port 8000
curl -X POST localhost:8000/streams/s1/playlists \
  -H 'Content-Type: application/json' \
  -d "{\"content\": $(python3 -c 'import json;print(json.dumps(open("tests/fixtures/window_v1.m3u8").read()))')}"
# 再 ingest window_v2.m3u8 后:
curl -X POST localhost:8000/streams/s1/compare   # 期望 decision=accept
curl localhost:8000/streams/s1/plan              # 期望 5 条 entries,total_duration=30.0
curl localhost:8000/jobs/<job_id>                # 期望 state=DONE
```

接口一览:

- `POST /streams/{id}/playlists` —  ingest 一个版本;201 接受 / 422 + `category` 拒绝
- `POST /streams/{id}/compare?from_version=&to_version=` — 对比(默认最近两版)
- `GET  /streams/{id}/plan?version=` — 可下载计划与连续播放边界
- `GET  /jobs/{job_id}` — 作业状态回放

每个响应带 `request_id`;compare/plan 同时落一条作业记录,诊断含判定理由
(为什么接受/拒绝/无法判定)。

## 依赖版本

Python 3.12;fastapi 0.141.1、starlette 1.7.0、uvicorn 0.54.0、numpy 2.5.3、
pydantic 2.13.5、httpx 0.28.1、pytest 9.1.1(见 requirements.txt)。

## 测试状态

- 已运行:`pytest -q` → **37 passed**(parser 12、timeline 4、compare 7、plan 5、api 9)。
- 未覆盖(如实标记):多线程并发写入 SQLite 的压测、EXT-X-MAP 初始化段的
  完整语义、PROGRAM-DATE-TIME 对齐(当前按非关键标签忽略)。
