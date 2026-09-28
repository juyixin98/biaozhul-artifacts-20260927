# 媒体拼接样本边界规划器 (Media Concat Sample-Boundary Planner)

本地音视频片段**无重编码拼接**可行性分析服务：解析合成的本地媒体样本表，校验
编解码参数 / 时间基 / 关键帧边界，计算 DTS/PTS 时间重定位（含必要预滚），定义
音频尾部填充与 B 帧重排，输出受约束容器（`mp4-constrained/v1`）的**逐样本拼接
计划**；不兼容时明确给出 `transcode_required` 与失败类别，绝不伪装直拼。

技术栈：Python 3.12 · FastAPI · NumPy · SQLite（标准库 `sqlite3`）· pytest。
全部输入均为本地合成夹具，无生产账号、无真实业务数据。

## 工程结构

```
app/
  config.py            配置层（环境变量、容器规则集版本、依赖版本）
  models.py            领域模型：样本/流/片段描述符、逐样本计划
  errors.py            结构化失败类别（FailureCategory）
  logging_setup.py     JSON 结构化日志（run_id/job_id/步骤/版本关联）
  media/parser.py      媒体解析：加载并校验片段描述符，计算输入 sha256
  core/
    timebase.py        时间与信号内核：精确有理数时间基换算
    compat.py          编解码参数/时间基兼容性 -> 转码要求
    gop.py             呈现窗口、参考闭包、关键帧预滚（含开放 GOP）
    audio.py           音频窗口、编码器延迟 priming、尾部静音填充
    planner.py         时间重定位与计划生成（DTS/PTS 逐样本移位）
    validate.py        独立逐样本计划校验（8 条容器规则）
  jobs/store.py        SQLite 作业状态机 + 进度事件表
  jobs/service.py      作业编排（解析→兼容→选择→重定位→独立校验）
  api/app.py           FastAPI 接口
fixtures/
  generate_fixtures.py 确定性夹具生成器（手工可算的样本表）
  data/                最小数据夹具（8 个 JSON 描述符）
tests/                 53 个独立测试（期望值手工硬编码）
scripts/               服务启动与端到端调用示例
docs/                  复现文档与已保存的可复核运行结果
requirements.in        直接依赖
requirements.txt       完整传递依赖锁定（pip freeze）
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python fixtures/generate_fixtures.py     # 已提交夹具，可重复生成
.venv/bin/python -m pytest                          # 53 passed

scripts/run_server.sh                               # 默认 :8000
APP_BASE=http://127.0.0.1:8000 .venv/bin/python scripts/demo_client.py
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 + run_id |
| GET | `/version` | app/python/numpy/fastapi/pydantic 版本 + 容器规则集 |
| POST | `/jobs` | 提交拼接作业（同步完成，状态机全程落库） |
| GET | `/jobs/{id}` | 作业状态（queued/analysing/completed/failed）、decision、错误类别 |
| GET | `/jobs/{id}/plan` | 逐样本计划；无计划时 409 并给出状态/错误类别 |
| GET | `/jobs/{id}/events` | 进度步骤（queued→parse→compat→select→relocate→validate→…） |
| POST | `/jobs/{id}/validate` | 对已存计划做独立复验 |

请求体（裁剪时间为有理秒 `{num, den}`，精确映射到各流时间基）：

```json
{
  "segments": [
    {"path": "seg_ok_a.json"},
    {"path": "seg_midgop.json",
     "trim_in": {"num": 2, "den": 15},
     "trim_out": {"num": 4, "den": 15}}
  ]
}
```

## 行为要点（均有逐样本断言）

- **兼容性**：codec / profile / level / 分辨率 / pix_fmt / sample_rate / channels /
  encoder_delay / 时间基不一致 → `transcode_required`，原因带
  `CODEC_MISMATCH` / `TIMEBASE_MISMATCH` / `PARAM_MISMATCH`，计划为空。
- **时间重定位**：每轨一个整数 delta（游标 − 首保留样本 DTS），DTS 与 PTS 同量
  移位，B 帧重排偏移逐样本保留；源负 DTS（B 帧重排延迟）被抬高，输出 DTS 从 0
  起、严格单调、无负值。
- **预滚**：裁剪窗口按呈现时间选取，保留样本取 `depends_on` 传递闭包；窗口外
  参考标记 `preroll`（仅解码不呈现）。开放 GOP 恢复点（非 IDR keyframe）之后的
  leading picture 会把恢复点之前的参考拉入计划。
- **音频**：编码器延迟帧标记 `priming`（负 PTS 由 edit list 处理，
  `edit_list_media_time` 逐轨给出）；音频短于视频时长时尾部按帧向上取整填充
  静音帧（`padding`），仅允许出现在片段尾部。
- **独立校验**：计划可脱离生成器重验：负 DTS、DTS 非严格递增、PTS<DTS、priming
  越界、参考样本缺失、呈现覆盖缺口、填充误用、非关键帧边界均有独立失败类别。
- **异常不伪装成功**：输入缺失 → 422 `INPUT_ERROR`；参考缺失 → 作业 `failed` +
  `MISSING_REFERENCE`；未预期异常 → 500 `INTERNAL_ERROR` 并落失败状态。

## 可观测性

- 每条日志为 JSON 行，含 `run_id`（进程级运行身份）、`job_id`、`step`、
  输入路径与 **sha256**、依赖版本、计算数据（保留索引、预滚集合、填充数、
  游标）和判定依据。
- SQLite (`var/jobs.db`) 存作业与 `job_events` 进度表。
- 已保存的真实运行结果见 `docs/results/`。

详见 [docs/REPRODUCE.md](docs/REPRODUCE.md)。
