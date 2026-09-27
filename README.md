# mp4-timeline：受限非加密 MP4 时间线解析服务

读取轨道样本表（stts/ctts/stsc/stsz/stco/stss）与 edit list（elst），
把每个媒体样本映射到电影时间轴上的呈现时刻与播放区间，并给出样本在文件中的
字节范围。仅限**非分片、非加密** MP4；分片布局（moof/mvex）与加密样本
（encv/enca/enct）明确拒绝。

## 本地验证命令

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 一键：构建夹具 + 全部测试
bash scripts/run_checks.sh

# 或分步
.venv/bin/python fixtures/build_fixtures.py   # 生成 fixtures/generated/*.mp4 与 payloads.json
.venv/bin/python -m pytest                    # 46 个用例

# 启动服务并在线验证
.venv/bin/python -m mp4timeline.api           # 127.0.0.1:8321
curl -s http://127.0.0.1:8321/health
curl -s -X POST http://127.0.0.1:8321/jobs \
  -H 'Content-Type: application/json' \
  -d '{"path": "fixtures/generated/bframes.mp4"}'
curl -s http://127.0.0.1:8321/validate        # overall 应为 "pass"
```

### 预期判断方式

- `pytest`：46 个用例全部 passed；每个用例断言**具体数值或具体错误类别**
  （如 `BoxLengthError`、`UnsupportedLayoutError`），不是“接口能调通”。
- `GET /validate`：`overall == "pass"`，且 5 个正例夹具各自的全部检查为
  `pass`。任何一步异常记为 `error`、数值不符记为 `fail`，绝不统一返回成功。
- 测试日志：`logs/testrun-<run_id>.jsonl`，头部含 run_id、Python/依赖版本、
  git commit、各夹具 sha256；随后每条记录是用例的关键计算值、判定依据
  （参考来源）与 verdict，可凭 run_id 关联一次运行。
- 作业审计：SQLite（默认 `data/jobs.sqlite3`）中每个作业有
  queued→running→done|failed 状态迁移、进度与日志行，输入以 sha256 关联。

## 算法假设

1. **DTS/PTS**：DTS 由 stts 累计（首样本为 0）；`PTS = DTS + CTO`，
   CTO 来自 ctts，version 1 按**有符号** int32 解码，符号保留（可为负）。
2. **timescale 换算**：全部用 `fractions.Fraction`，不经过浮点。
   90000→1000 等非整除比例产生精确分数（如 `100/3`），API 以 `num/den`
   字符串输出。
3. **edit list 映射**：顺序处理每条 edit，维护电影时间游标：
   - 空编辑（`media_time == -1`）：游标前进 `segment_duration`，不呈现样本
     （电影轴上形成空档）；
   - 普通编辑：媒体区间 `[media_time, media_time + segment_duration *
     media_timescale / movie_timescale)` 内的样本被呈现，
     `movie_time = cursor + (PTS - media_time) * movie_timescale /
     media_timescale`；样本末端越过段尾时在段边界裁剪。
4. **边界规则**：段起点闭、段终点开 —— `PTS == 段起点` 包含，
   `PTS == 段终点` 排除。空编辑本身定义为空档，无样本归属。
5. **未覆盖样本**：不被任何 edit 覆盖的样本列入 `unpresented`（带原因），
   不静默丢弃；同一样本被多段覆盖时先出现的编辑优先。
6. **无 elst**：按恒等编辑处理（整段媒体映射到电影轴原点）。
7. **media_rate**：仅支持 1.0，其余明确拒绝（`EditListError`）。
8. **拒绝范围**：盒长度越界（`BoxLengthError`）；分片布局 moof/mvex、
   加密样本条目（`UnsupportedLayoutError`）；缺必需盒（`MissingBoxError`）；
   样本表内部不一致（`SampleTableError`）。

## 模块关系

```
mp4timeline/
├── config.py              配置层：路径/限制，环境变量覆盖
├── errors.py              错误类别（测试按类别断言）
├── mp4parse/              媒体解析层（结构）
│   ├── boxes.py           盒读取：32/64 位长度、size==0、越界校验
│   ├── sample_table.py    stts/ctts/stsc/stsz/stco/stss → 逐样本记录（NumPy 展开）
│   ├── editlist.py        elst v0/v1 → Edit（空编辑、速率校验）
│   └── parser.py          整文件 → Movie/Track；分片/加密拒绝；字节范围复核
├── timeline/              时间与信号内核
│   ├── rational.py        Fraction 换算（rescale/to_seconds）
│   └── kernel.py          edit list 呈现映射 → TrackTimeline
├── jobs/                  作业状态层
│   ├── store.py           SQLite 状态机（queued→running→done|failed）+ 日志
│   └── runner.py          执行管线：读文件→解析→时间线→序列化
├── api/app.py             FastAPI：/health /jobs /jobs/{id}/result /validate
└── validation/checks.py   证据校验：对照手写参考 + 构建器载荷 sha1

fixtures/
├── build_fixtures.py      合成 MP4 写出器（两遍布局算 stco；独立于被测核心）
├── references/*.json      手写参考时间表（不经被测核心生成）
└── generated/             构建产物：*.mp4 + payloads.json（样本 sha1）

tests/                     独立测试层（conftest 产出 logs/testrun-<run_id>.jsonl）
```

数据流：`api/jobs.runner` 调 `mp4parse.parser.parse_movie` 得 `Movie`，
再对每轨调 `timeline.kernel.build_track_timeline` 得 `TrackTimeline`，
序列化落 SQLite。`validation` 复用同一管线，对照 `fixtures/references`
（手写）与 `fixtures/generated/payloads.json`（构建器记录）逐项判定。

## 夹具与证据设计

| 夹具 | 验证点 |
|---|---|
| `bframes.mp4` | B 帧重排：ctts v1 含**负偏移**，解码序≠呈现序；2 样本/chunk 的 stsc 分组 |
| `empty_edit.mp4` | 前置空编辑：电影轴前 500 单位空档，媒体从 500 开始呈现 |
| `trim_edit.mp4` | 裁剪边界：段起点包含、段终点排除；未覆盖样本列入 unpresented |
| `multi_edit.mp4` | 两段编辑之间的样本不被任何段覆盖 |
| `multitrack.mp4` | 视频 90000 / 音频 48000 / 电影 1000 的有理数换算（`100/3` 精确分数） |
| `bad_length.mp4` | 盒声明长度越界 → `BoxLengthError` |
| `fragmented.mp4` | 含 moof → `UnsupportedLayoutError` |
| `encrypted.mp4` | stsd 条目 encv → `UnsupportedLayoutError` |

参考答案三重独立来源：手写 JSON（`fixtures/references/`）、测试内字面量
（`tests/test_samples.py::BFRAMES_EXPECTED`）、构建器载荷 sha1
（`payloads.json`）——均不由被测核心生成。`tests/test_validation.py` 还
验证篡改样本字节必被检出、坏文件记 `error` 而非成功。

## 依赖版本（requirements.txt 锁定）

- Python 3.12.3
- fastapi 0.141.1 / uvicorn 0.54.0 / pydantic 2.13.5
- numpy 2.5.3（样本表 run-length 展开、PTS 稳定排序）
- pytest 9.1.1 / httpx 0.28.1（TestClient）
- SQLite 使用 Python 标准库 `sqlite3`

## 已知限制

- 仅支持非分片、非加密 MP4；`media_rate != 1.0` 的变速编辑拒绝处理。
- 作业同步执行（请求内完成），无并发队列；状态机已落库，可平滑替换为
  后台 worker。
- stsd 仅读取样本条目类型用于加密判定，不解析编码私有数据。
