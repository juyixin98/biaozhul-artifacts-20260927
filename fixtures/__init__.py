"""可复用合成夹具：场景 JSON 加载。

场景定义（fixtures/scenarios/*.json）中的逐版本期望是人工编写的第三参考来源，
与被测内核 scanner、独立 oracle（app.kernel.oracle）彼此独立。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCENARIO_DIR = Path(__file__).parent / "scenarios"


@dataclass
class Scenario:
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def description(self) -> str:
        return self.raw.get("description", "")

    @property
    def schema(self) -> dict[str, Any]:
        return self.raw["schema"]

    @property
    def versions(self) -> list[dict[str, Any]]:
        return self.raw["versions"]

    @property
    def reads(self) -> list[dict[str, Any]]:
        return self.raw.get("reads", [])

    @property
    def explain_cases(self) -> list[dict[str, Any]]:
        return self.raw.get("explain_cases", [])

    @property
    def rows_cases(self) -> list[dict[str, Any]]:
        return self.raw.get("rows_cases", [])


def load_scenario(path: str | Path) -> Scenario:
    with open(path, "r", encoding="utf-8") as f:
        return Scenario(json.load(f))


def load_all() -> list[Scenario]:
    return [load_scenario(p) for p in sorted(SCENARIO_DIR.glob("*.json"))]
