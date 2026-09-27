# driftcorr — 两路合成音频采样时钟的漂移估计与时间轴校正

对两段同一节目的录音（参考时钟 / 漂移时钟），利用已知同步脉冲做互相关检测，
用**显式鲁棒拟合**（Theil–Sen + MAD 内点筛选 + 残差合理性上界）估计固定偏移与
漂移 ppm，然后分别输出两类结果：

1. **重采样校正音频**（窗函数 sinc 插值到参考采样网格）；
2. **元数据时间映射**（闭式仿射映射 `t_ref = (t_target - offset) / (1 + drift)`，
   与音频渲染完全分离）。

估计证据不足（内点太少、残差与检测精度不符）时**拒绝校正**并给出分类失败原因。
相关性峰值只证明两段录音的相对对齐，**不构成绝对时间证明**——每份报告都显式
标注 `absolute_time_verified: false`。

## 模块拆分

```
src/driftcorr/
├── media/          # 媒体解析：WAV 读写（stdlib wave）、JSON 边车元数据
├── core/           # 时间与信号内核
│   ├── sync_points.py  # 归一化互相关脉冲检测 + 期望时刻配对
│   ├── fit.py          # Theil–Sen 鲁棒直线拟合 + MAD 内点筛选
│   ├── drift.py        # 偏移/漂移估计、残差统计、可用区间
│   ├── resample.py     # 窗函数 sinc 重采样校正
│   ├── timeline.py     # 元数据时间映射（独立于音频）
│   └── report.py       # 报告组装（残差、可用区间、免责说明）
├── jobs/           # 作业状态：SQLite 存储、状态机、失败分类
├── api/            # 验证接口：FastAPI 应用、请求身份关联
├── fixtures/       # 合成夹具（解析信号，独立于被测核心）
└── pipeline.py     # 编排：媒体 → 同步点 → 拟合 → 两类输出 → 报告
config/default.json # 独立配置（检测阈值、拟合参数、输出目录等）
scripts/make_fixtures.py  # 重新生成 data/fixtures/ 样例数据
tests/                  # 单元 + 集成测试
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"

# 运行测试（33 个：单元 + 端到端 + API 集成）
.venv/bin/python -m pytest tests/ -q

# 重新生成样例数据（已随仓库提交在 data/fixtures/）
.venv/bin/python scripts/make_fixtures.py

# 启动服务（配置默认读 config/default.json，可用 DRIFTCORR_CONFIG 覆盖）
.venv/bin/uvicorn driftcorr.api.app:app --port 8391
```

## 使用示例

```bash
curl -s -X POST localhost:8391/jobs \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-req-001' \
  -d '{
    "reference_path": "data/fixtures/corrupted/reference.wav",
    "target_path": "data/fixtures/corrupted/target.wav",
    "reference_metadata_path": "data/fixtures/corrupted/reference.meta.json",
    "target_metadata_path": "data/fixtures/corrupted/target.meta.json"
  }'

curl -s localhost:8391/jobs/<job_id>     # 查看完整报告
curl -s localhost:8391/jobs              # 作业列表
```

`X-Request-ID` 可省略（服务端生成）；它会被写入响应头、作业行和每一条
流水线日志（`request_id=... job_id=... step=...`），三方可以互相对上。

## 夹具与实测结论

夹具真值：偏移 **0.123 s**、漂移 **+75 ppm**、8 个同步脉冲（0.5–7.5 s）。
`corrupted` 场景额外注入：target 7.32 s 处掉帧 25 ms、target 3.55 s 处一个
假同步脉冲。实测（本仓库当前代码，Python 3.12）：

| 场景 | 结果 |
|---|---|
| clean | offset=0.123006 s（误差 6 µs），drift=75.03 ppm（误差 0.03 ppm），0 离群，校正后对齐残差 RMS 0.004 ms |
| corrupted | offset/drift 同样恢复；恰好标记 ref 3.5 s（假脉冲）与 ref 7.5 s（掉帧）两个离群点；对齐检查 max 残差 25 ms，如实暴露未被建模的掉帧 |
| degenerate（仅 2 个脉冲） | 作业 `failed`，`error_class=insufficient_evidence`，**不输出校正** |

测试断言的是具体数值与失败类别（如 `sorted(outliers) == [3.5, 7.5]`、
`drift_ppm ≈ 75±5`），不是“接口能调通”。拟合/映射/重采样的单元测试答案
来自手算字面值与解析信号（如已知频率正弦的解析采样），不由被测核心自生成。

## 报告里有什么

- `estimate`：offset / drift_ppm（含粗略不确定度）、逐同步点残差与内点标记、
  拟合方法、RMS/最大残差；
- `usable_interval_s`：首个到末个**内点**同步点覆盖的区间；区间外为外推，
  元数据事件落在区间外会被标记 `within_usable_interval: false`；
- `corrected_audio`：重采样输出路径与采样网格映射参数；
- `time_mapping`：与音频分离的闭式映射及逐事件映射结果；
- `alignment_check`：对校正后音频重新检测同步脉冲的对齐残差（ms）；
- `absolute_time_verified: false` 及说明——互相关只证明相对对齐；
- `failure`（失败时）：`error_class` ∈ `no_sync_points` /
  `insufficient_evidence` / `invalid_input` / `media_decode_error` 等 +
  人可读原因。

## 配置（config/default.json）

| 键 | 含义 | 默认 |
|---|---|---|
| `detection.correlation_threshold` | 相关峰检测阈值 | 0.55 |
| `detection.max_pairing_offset_s` | 期望时刻配对窗口 | 0.4 s |
| `fit.min_inliers` | 允许校正的最少内点数 | 3 |
| `fit.mad_multiplier` / `min_residual_threshold_s` | 内点阈值 = max(下限, k·MAD) | 6.0 / 4 ms |
| `fit.max_inlier_residual_s` | 内点残差 RMS 合理性上界，超出即证据不足 | 50 ms |
| `resample.half_width_taps` | sinc 插值半宽 | 16 |

环境变量：`DRIFTCORR_CONFIG`（配置文件路径）、`DRIFTCORR_DB_PATH`、
`DRIFTCORR_OUTPUT_DIR`。

## 方法说明与限制

- 时钟模型：target 时刻 = offset + (1 + drift_ppm·1e-6) × ref 时刻；
  估计的是**仿射**模型，无法表示时变漂移（漂移突变会表现为离群点或残差增大）。
- 掉帧（样本缺失）不是仿射误差：落在两个同步点之间的掉帧无法被模型吸收，
  会体现在校正后对齐残差里（见 corrupted 场景），不会静默消失。
- Theil–Sen 崩溃点约 29%：离群同步点过多时会触发 `insufficient_evidence`
  而不是给出错误校正——这是设计意图。
- 同步脉冲的声明时刻来自参考侧元数据；本系统不与任何绝对时钟源对时。
