"""验证接口 /jobs/{id}/verify：重放、切块不变性、参考语义、守恒。"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media import wav_bytes
from app.web import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path), max_samples_per_job=20_000,
        max_chunk_bytes=20_000, max_jobs=10, event_ring=1000)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


CONFIG = {
    "enter_threshold": 0.03, "exit_threshold": 0.08,
    "min_silence_ms": 100, "min_speech_ms": 50,
    "pad_before_ms": 10, "pad_after_ms": 20,
}


def _signal():
    x = np.zeros(1200, dtype=np.float32)
    x[0:200] = 0.5
    x[300:330] = 0.05          # 中间带短噪声（不应产生区间）
    x[500:580] = 0.5
    x[700:720] = 0.9           # 真 H 短噪声 20 < 50
    x[900:1000] = 0.5
    return x


def _new_pcm_job(client):
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    return r.json()["job_id"]


def test_verify_finalized_pcm_all_checks_pass(client):
    x = _signal()
    jid = _new_pcm_job(client)
    # 用奇怪块长上传（31）
    for a in range(0, len(x), 31):
        client.post(f"/jobs/{jid}/chunks",
                    content=x[a:min(a + 31, len(x))].tobytes())
    client.post(f"/jobs/{jid}/finalize")

    r = client.post(f"/jobs/{jid}/verify")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    checks = body["checks"]
    assert checks["chunking_invariance"]["ok"] is True
    assert checks["integrity"]["ok"] is True
    assert checks["sample_conservation"]["ok"] is True
    assert checks["reference_semantics"]["ok"] is True
    # 参考实现给出的具体区间与存储一致（短噪声被吞并）
    assert checks["reference_semantics"]["reference"] == \
        [[0, 220], [490, 600], [890, 1020]]
    assert checks["reference_semantics"]["stored"] == \
        checks["reference_semantics"]["kernel"]


def test_verify_open_job_checks_invariance_without_semantics(client):
    x = _signal()
    jid = _new_pcm_job(client)
    # 只传到 400：第二语音尚未出现，作业仍 open
    client.post(f"/jobs/{jid}/chunks", content=x[:400].tobytes())
    r = client.post(f"/jobs/{jid}/verify")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "reference_semantics" not in body["checks"]
    assert body["checks"]["chunking_invariance"]["ok"] is True


def test_verify_wav_job(client):
    x = np.zeros(1000)
    x[100:300] = 0.5
    r = client.post("/jobs", json={
        "config": CONFIG, "media": {"container": "wav"}})
    jid = r.json()["job_id"]
    rr = client.post(f"/jobs/{jid}/chunks", content=wav_bytes(x, 1000))
    assert rr.status_code == 200
    v = client.post(f"/jobs/{jid}/verify")
    assert v.status_code == 200
    assert v.json()["ok"] is True


def test_verify_replays_persisted_chunks_not_memory(client):
    """复核必须基于持久化块：新建服务实例打开同一 DB 也能重放。"""
    x = _signal()
    jid = _new_pcm_job(client)
    for a in range(0, len(x), 64):
        client.post(f"/jobs/{jid}/chunks",
                    content=x[a:min(a + 64, len(x))].tobytes())
    client.post(f"/jobs/{jid}/finalize")

    # 从同一 data dir 构建全新 app/service（模拟进程重启）
    data_dir = client.app.state.settings.data_dir
    settings = Settings(data_dir=data_dir, max_samples_per_job=20_000,
                        max_chunk_bytes=20_000, max_jobs=100,
                        event_ring=1000)
    app2 = create_app(settings)
    # 内存中没有该 job 的 SegmentConfigIn -> 需要从 DB 重建；
    # 当前设计把 cfg_in 缓存在内存，这里断言其降级行为：重建失败必须报
    # COMPUTATION_FAILED 而不是给出错误的 ok=True（错误类别可区分）。
    with TestClient(app2) as c2:
        r = c2.post(f"/jobs/{jid}/verify")
    # 见 service 设计：配置持久化在 jobs.config_json，重启后应能恢复 ——
    # 下面这一断言驱动该能力的实现（在提交前修正为 200）。
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_open_job_survives_restart_and_continues_chunks(client):
    """进程重启后向 open 作业续写块：状态从持久化块重放，结果不变。"""
    x = _signal()
    jid = _new_pcm_job(client)
    client.post(f"/jobs/{jid}/chunks", content=x[:400].tobytes())
    pre = client.get(f"/jobs/{jid}").json()
    assert [[i["start"], i["end"]]
            for i in pre["intervals_committed"]] == [[0, 220]]

    data_dir = client.app.state.settings.data_dir
    settings = Settings(data_dir=data_dir, max_samples_per_job=20_000,
                        max_chunk_bytes=20_000, max_jobs=100,
                        event_ring=1000)
    with TestClient(create_app(settings)) as c2:
        # 续写剩余 800 样本（状态必须从 DB 重放，而不是从 0 开始）
        for a in range(400, 1200, 29):
            rr = c2.post(f"/jobs/{jid}/chunks",
                         content=x[a:min(a + 29, 1200)].tobytes())
            assert rr.status_code == 200, rr.text
        fin = c2.post(f"/jobs/{jid}/finalize").json()
    assert [[i["start"], i["end"]]
            for i in fin["intervals_committed"]] == \
        [[0, 220], [490, 600], [890, 1020]]
