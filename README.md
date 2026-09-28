# MP4 Timeline Service (restricted, non-encrypted)

解析受限非加密 MP4（plain / 非分片）的轨道样本表与 edit list，输出区分
DTS/PTS 的样本时间线、样本字节范围，以及映射到电影时间轴的播放区间。

## 模块关系

```
mp4timeline/
  errors.py     错误分类学：每类失败有稳定 category 字符串
  boxes.py      盒解析层：ftyp/moov/trak/.../stbl 遍历，stts/ctts/stsc/stsz/
                stco|co64/stss/elst/mdhd/mvhd/tkhd/hdlr 表读取（NumPy 数组），
                盒长度越界、分片布局、加密媒体在此拒绝
  timeline.py   时间与信号内核：样本表展开（DTS=Σstts, PTS=DTS+ctts 带符号），
                stsc/stco/stsz → 样本字节范围，elst → 电影时间轴映射
                （fractions.Fraction 精确有理数，无浮点）
  jobs.py       作业状态：SQLite 存储状态机 queued→running→done|failed，
                每步计算写入 job_events，结果绑定输入 sha256
  api.py        FastAPI 接口：/health /jobs /jobs/{id} /jobs/{id}/timeline /validate
  config.py     配置层：MP4TL_DB_PATH / MP4TL_MAX_FILE_BYTES 环境变量
fixtures/
  builder.py        合成 MP4 写入器（与解析器零共享代码）
  make_fixtures.py  生成 fixtures/data/*.mp4（5 个正例 + 5 个反例）
  expected.py       手工计算的参考时间线（字面量，非被测代码生成）
tests/              独立测试层（46 例）
```

数据流：`api.py → jobs.py → boxes.py → timeline.py → 结果回存 SQLite → API 返回`。

## 算法假设

1. **DTS/PTS**：DTS 为 stts 解码 delta 的前缀和（首样本 DTS=0）；
   PTS = DTS + CTO，ctts version 0 无符号、version 1 有符号，符号原样保留
   （`ctts_signed.mp4` 含负 PTS 样本验证）。
2. **时间换算**：媒体 timescale → 电影 timescale 全部用
   `Fraction(value) * movie_ts / media_ts`，不做浮点近似
   （`multitrack.mp4` 音频 1024/48000 s = 64/3 电影单位验证非整数有理数）。
3. **edit list**：按条目顺序推进电影游标；`media_time == -1` 为空编辑，
   占据电影时间但不映射样本（记为 gap）；普通编辑呈现 PTS 落在半开窗口
   `[media_time, media_time + seg·media_ts/movie_ts)` 内的样本，样本电影起点
   为 `cursor + (pts − media_time)` 换算值，终点在编辑末尾处裁剪；窗口未覆盖
   的电影区间定义为 *uncovered*（不补、不报错）。仅支持 rate 1.0，其余拒绝。
4. **样本呈现时长**：取该样本的 stts 解码 delta 换算到电影时间；B 帧尾部
   可能被编辑末尾裁剪（`bframes.mp4` 第 4 样本验证）。
5. **无 elst 的轨道**：隐式单编辑 = 从媒体时间 0 起覆盖整个 mdhd duration。
6. **拒绝即失败**：盒长度越界、moof/mvex/mfra（分片）、encv/enca/tenc/sinf
   （加密）、表截断、stsc/stts/stsz 互相矛盾、样本数据越出文件，均抛出带
   category 的异常；作业记为 `failed`，接口返回 422/409，绝不返回成功。

## 依赖版本（Python 3.12.3）

fastapi 0.141.1 · uvicorn 0.54.0 · numpy 2.5.3 · pydantic 2.13.5 ·
pytest 9.1.1 · httpx 0.28.1（锁定于 `requirements.txt`；SQLite 用标准库 sqlite3）

## 本地验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. 生成合成夹具（幂等，可重复）
.venv/bin/python -m fixtures.make_fixtures

# 2. 运行测试（日志含运行 ID、版本、输入 sha256、逐步 CHECK 判定依据）
.venv/bin/python -m pytest -v
# 预期：46 passed, 0 failed。日志中每个 CHECK 行显示 got=/want= 对照。

# 3. 启动服务并手工验证
MP4TL_DB_PATH=/tmp/mp4tl.db .venv/bin/uvicorn mp4timeline.api:app --port 8000
curl -s localhost:8000/health                                   # 版本信息
curl -s -XPOST localhost:8000/jobs -H 'content-type: application/json' \
     -d '{"path": "fixtures/data/multitrack.mp4"}'              # {"status":"done",...}
curl -s localhost:8000/jobs/<job_id>/timeline                   # 完整时间线 JSON
curl -s -XPOST localhost:8000/validate -H 'content-type: application/json' \
     -d '{"path": "fixtures/data/fragmented.mp4"}'              # 422 + category
```

预期判断方式：
- `pytest` 全绿；失败测试会打印具体 `got=/want=` 差异而非笼统报错。
- 正例夹具返回 `done` 且时间线与 `fixtures/expected.py` 的手工值逐项相等；
  反例夹具返回 `failed`/422，category 与 `expected.BAD_FIXTURES` 一致。
- 作业事件流（`GET /jobs/{id}` 的 `events`）应含
  `created → input_read → boxes_parsed → track_timeline_built → done`。

## 测试状态

| 层 | 文件 | 状态 |
|---|---|---|
| 盒解析（越界/分片/加密/截断/rate） | tests/test_boxes.py | ✅ 通过 |
| 时间线内核（5 正例全量对照 + 专项） | tests/test_timeline.py | ✅ 通过 |
| 作业状态机与事件流 | tests/test_jobs.py | ✅ 通过 |
| API 生命周期与失败分类 | tests/test_api.py | ✅ 通过 |

最近运行：46 passed, 0 failed（pytest 9.1.1）。无跳过、无未运行项。
