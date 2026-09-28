# 执行结果记录（如实）

记录环境、命令与实际输出摘要。所有数字为本次交付时在本机真实执行所得。

## 环境

- OS：Linux 6.8.0-90-generic (Ubuntu)，Python **3.12.3**
- 依赖（`pip install -r requirements.txt`，精确版本见 requirements.txt）：
  fastapi 0.141.1 / uvicorn 0.54.0 / pydantic 2.13.5 / grapheme 0.6.0
  （Unicode **13.0.0**）/ pytest 9.1.1 / httpx 0.28.1 / regex 2026.9.10

## 单元/端到端测试

命令：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m pytest tests/ -q
```

实际结果：**710 passed**（2 个警告，均为第三方库自带的弃用提示：
starlette TestClient 关于 httpx、及一条 httpx 原始字节用法提示，非本项目代码）。

测试分布：
- 编码/规范化：非法 UTF-8 五种形态、JSON 孤立代理项、前导字节谓词、NFC
- 分段/双向索引：8 个手算夹具（组合符、US/JP 旗帜、三 RIS、ZWJ 五口之家、
  肤色修饰、CRLF、综合串）+ 三空间往返
- 属性测试：300 个随机棘手 Unicode 串，与独立 `regex \X` 预言机逐表一致
- 增量编辑：旗帜奇偶、ZWJ 合并、CRLF 不可拆、三锚点；150 NONE + 80 NFC
  随机编辑断言「增量 == 完整重建 == 预言机」
- 存储/版本：Blob 位翻转校验和、截断、魔数、版本身份不匹配、摘要冲突(409)、
  修订历史、跨连接持久化
- 失败类别：`invalid_utf8`、`illegal_byte_boundary`、
  `illegal_codepoint_boundary`、`position_out_of_range`、
  `document_too_large`(413)、`too_many_clusters`(413)、`storage_full`(507)、
  `digest_mismatch`(409)、`segmenter_error`(500)、`index_corrupt`(500)

## 额外压力验证（开发期临时脚本，非测试套件）

- 三锚点（grapheme/codepoint/byte）随机增量编辑 **3000 例**：与完整重建
  不一致 **0** 例。
- NFC 规范化增量编辑 **1000 例**：不一致 **0** 例。
- 300 个随机串逐字节核对错误归类（延续字节→illegal_byte_boundary；
  簇内前导字节→illegal_codepoint_boundary）：归类错误 **0**。

## 干净目录复现

把源码（排除 `.venv/data/logs/__pycache__`）复制到空目录 `/tmp/ti-clean`，
新建 venv → `pip install -r requirements.txt` → `pytest`：
**709 passed**（当时 storage_full 用例尚未加入；加入后在主工作区为 710）。
随后在该副本以 uvicorn 启动并执行 `scripts/request_samples.sh`，各端点返回
与预期一致（版本固定 13.0.0、旗帜/组合符分簇、两类边界 422、非法 UTF-8 400、
编辑后 validate 与重建一致、版本历史含两个不同摘要）。

## 过程中发现并处理的真实问题

1. **上游库缺陷**：`grapheme 0.6.0` 的 FSM 在 `Prepend` 状态遇到 LF/CONTROL
   后错误回到 `default`，可使后续 ZWJ 被错误并入控制符簇（违反 GB4/GB5 优先
   于 GB9b）。在 `segmenter.py` 按 UAX #29 GB3–GB5 硬性不变量做窄幅修正，
   并以独立 `regex` 预言机在 300 随机串上交叉验证（修复前 20+ 处分歧，
   修复后 0 处分歧）。详见 README「固定的数据版本」。
2. **增量窗口不足**：初版窗口仅外扩一簇，RIS 奇偶配对在窗口边缘会被破坏。
   改为上下文安全窗口（吞入整个 RIS 运行段、CR/LF、Prepend 链、尾部 ZWJ），
   修复后 3000 例模糊全部与重建一致。
3. 若干**测试自身**的手算/标注错误（夹具 A 字节数 8→7、簇索引区间、
   码点数、字节偏移）均已纠正；核心实现的相应行为经独立预言机确认正确。

## 日志与回放（实际执行）

```text
$ PYTHONPATH=src python -m scripts.replay tests/_run_artifacts/<run>.tests.jsonl --summary
test log ...: {'PASS': 36, 'FAIL': 0}      # 退出码 0
```

服务运行日志为 JSONL，每行含 `run_id/seq/op/outcome/code/intermediate/
reason/request_id`；样例实跑后可见一次成功编辑的窗口与位移中间态，以及
`invalid_utf8`、`illegal_byte_boundary`、`illegal_codepoint_boundary` 的
结构化失败记录。
