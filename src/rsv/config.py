# 独立配置：默认值集中在此，可用环境变量 RSV_<UPPER_FIELD> 覆盖。
# 本系统只用于本地测试交易，任何资源限制默认值都偏小，便于触发并测试边界。

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    # 单个栈元素最大字节数
    max_element_size: int = 520
    # 栈中元素最大个数
    max_stack_items: int = 64
    # 一次脚本执行的操作步数预算（解析后的指令，IF/ENDIF 也算步）
    max_op_steps: int = 128
    # 脚本嵌套分支最大深度
    max_script_depth: int = 8
    # 锁定/解锁脚本最大原始字节数
    max_script_bytes: int = 2048
    # CHECKMULTISIG 支持的最大公钥数
    max_multisig_pubkeys: int = 16
    # 执行轨迹最多保留多少条（防日志内存膨胀，不影响判定）
    max_trace_items: int = 200


@dataclass(frozen=True)
class ChainConfig:
    # 网络标签，参与域标签计算，防止跨链/跨环境重放
    network: str = "rsv-local"
    # 默认域标签（创世纪 UTXO 未显式给 domain 时使用）
    default_domain: str = "rsv-test-domain-v1"
    # 是否要求脚本执行后栈上恰好只剩一个为真的元素（clean stack）
    require_clean_stack: bool = True
    # 单笔交易输入/输出个数上限
    max_inputs: int = 8
    max_outputs: int = 8
    # 单个金额上限（防整数溢出推算）
    max_value: int = 21_000_000_000


@dataclass(frozen=True)
class Settings:
    limits: Limits = Limits()
    chain: ChainConfig = ChainConfig()
    sqlite_path: str = "data/rsv.sqlite3"
    runs_dir: str = "runs"
    fixture_dir: str = "fixtures"
    bootstrap_file: str = "fixtures/genesis.json"


def _apply_env_overrides(limits: Limits, chain: ChainConfig) -> tuple[Limits, Limits]:
    int_env = {
        "RSV_MAX_ELEMENT_SIZE": "max_element_size",
        "RSV_MAX_STACK_ITEMS": "max_stack_items",
        "RSV_MAX_OP_STEPS": "max_op_steps",
        "RSV_MAX_SCRIPT_DEPTH": "max_script_depth",
        "RSV_MAX_SCRIPT_BYTES": "max_script_bytes",
        "RSV_MAX_MULTISIG_PUBKEYS": "max_multisig_pubkeys",
        "RSV_MAX_TRACE_ITEMS": "max_trace_items",
    }
    kw_l: dict = {}
    for env, field in int_env.items():
        if env in os.environ:
            kw_l[field] = int(os.environ[env])
    limits = replace(limits, **kw_l) if kw_l else limits

    str_env = {
        "RSV_NETWORK": "network",
        "RSV_DEFAULT_DOMAIN": "default_domain",
    }
    kw_c: dict = {}
    for env, field in str_env.items():
        if env in os.environ:
            kw_c[field] = os.environ[env]
    for env, field in {
        "RSV_REQUIRE_CLEAN_STACK": "require_clean_stack",
    }.items():
        if env in os.environ:
            kw_c[field] = os.environ[env].lower() in ("1", "true", "yes")
    for env, field in {
        "RSV_MAX_INPUTS": "max_inputs",
        "RSV_MAX_OUTPUTS": "max_outputs",
        "RSV_MAX_VALUE": "max_value",
    }.items():
        if env in os.environ:
            kw_c[field] = int(os.environ[env])
    chain = replace(chain, **kw_c) if kw_c else chain
    return limits, chain


def load_settings(
    sqlite_path: str | None = None,
    runs_dir: str | None = None,
    bootstrap_file: str | None = None,
) -> Settings:
    limits, chain = _apply_env_overrides(Limits(), ChainConfig())
    here = Path(__file__).resolve().parents[2]
    return Settings(
        limits=limits,
        chain=chain,
        sqlite_path=sqlite_path or os.environ.get("RSV_SQLITE_PATH", str(here / "data/rsv.sqlite3")),
        runs_dir=runs_dir or os.environ.get("RSV_RUNS_DIR", str(here / "runs")),
        bootstrap_file=bootstrap_file
        or os.environ.get("RSV_BOOTSTRAP_FILE", str(here / "fixtures/genesis.json")),
    )
