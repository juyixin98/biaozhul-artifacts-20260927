"""策略匹配引擎。

把 :class:`PolicyBundle` 编译成快速匹配器，回答：

1. **入口判定**：scheme、userinfo 是否允许（URL 层）；
2. **连接判定**：某个规范化 IP 候选 + 端口是否允许（IP 层）；
3. **混合地址集判定**：一个域名解析出多个候选时如何处置。

显式规则（无隐式魔法）
======================
* 规则按文件顺序求值，**第一条命中即决定**（allow 或 deny）；
* 无任何命中 → ``default_action``（本项目策略固定 deny）；
* 混合地址集仅支持 ``deny_if_any_forbidden``：候选中**只要有一个**被禁，
  整组拒绝且**不连接任何候选**（包括其中"看起来允许"的那个），全部候选
  的逐条判定写入决策链；
* host 规则做精确主机匹配（已规范化主机），不做后缀匹配，避免
  ``evil-allowed.local`` 之类的混淆。
"""

from __future__ import annotations

import ipaddress

from ..contracts import (
    IpCandidate,
    MatchedRule,
    PolicyBundle,
    Rule,
    Verdict,
)


class PolicyEngine:
    def __init__(self, bundle: PolicyBundle) -> None:
        self._bundle = bundle
        # 统一的有序匹配器，严格保留文件顺序（host 与 cidr 混排也正确）。
        # 每个匹配器: (kind, payload, rule)；payload 含义随 kind。
        self._matchers: list[tuple[str, object, Rule]] = []
        for rule in bundle.rules:
            if rule.kind == "cidr":
                self._matchers.append(
                    ("cidr", ipaddress.ip_network(rule.target, strict=False), rule)
                )
            else:
                t = rule.target.strip("[]").lower()
                try:
                    ip_int = int(ipaddress.ip_address(t))
                    self._matchers.append(("hostip", ip_int, rule))
                except ValueError:
                    self._matchers.append(("hostname", t, rule))

    @property
    def bundle(self) -> PolicyBundle:
        return self._bundle

    # ------------------------------------------------------------------
    # URL 入口层
    # ------------------------------------------------------------------
    def userinfo_allowed(self) -> bool:
        return not self._bundle.deny_userinfo

    # ------------------------------------------------------------------
    # 主机/端口层（字面量 IP 主机与域名候选共用）
    # ------------------------------------------------------------------
    def evaluate_candidate(self, candidate: IpCandidate, host: str, port: int) -> MatchedRule | None:
        """返回第一条命中的规则；无命中返回 None（调用方按 default_action 处理）。

        对每个候选（无论是字面量 IP 主机，还是域名解析结果）统一：

        * ``hostname`` 规则按规范化主机名精确匹配；
        * ``hostip``   规则按候选 IP 数值匹配（host 规则也能管解析结果）；
        * ``cidr``     规则按候选 IP 网段匹配。

        端口规则未指定端口时匹配任意端口，指定时必须相等。
        """

        try:
            addr = ipaddress.ip_address(candidate.literal)
        except ValueError:
            addr = None

        for kind, payload, rule in self._matchers:
            if rule.port is not None and rule.port != port:
                continue
            if kind == "hostname":
                if host.lower() == payload:
                    return _to_match(rule, f"{host}:{port}")
            elif kind == "hostip":
                if addr is not None and int(addr) == payload:
                    return _to_match(rule, f"{candidate.literal}:{port}")
            else:  # cidr
                if addr is not None and addr in payload:  # type: ignore[operator]
                    return _to_match(rule, str(payload))
        return None

    def default_match(self) -> MatchedRule:
        return MatchedRule(
            rule_id="default",
            action=self._bundle.default_action,
            target="*",
            detail="无任何规则命中，按 default_action 处置",
        )

    def verdict_for(self, candidate: IpCandidate, host: str, port: int) -> tuple[Verdict, MatchedRule]:
        match = self.evaluate_candidate(candidate, host, port)
        if match is None:
            match = self.default_match()
        return (Verdict.ALLOW if match.action == "allow" else Verdict.DENY), match

    # ------------------------------------------------------------------
    # 混合地址集：deny_if_any_forbidden
    # ------------------------------------------------------------------
    # 规则 id 前缀的敏感度：集合中含多个被禁候选时，报告优先级最高的禁因
    # （元数据/链路本地最敏感）。未列出的规则优先级为 0。
    _SEVERITY = (
        ("deny-metadata", 100),
        ("deny-loopback", 60),
        ("deny-private", 50),
        ("deny-mapped", 40),
        ("deny-unique-local", 30),
        ("deny-unspecified", 20),
        ("default", 10),
    )

    def _severity_of(self, rule_id: str) -> int:
        for prefix, score in self._SEVERITY:
            if rule_id.startswith(prefix):
                return score
        return 0

    def evaluate_set(
        self,
        candidates: tuple[IpCandidate, ...],
        host: str,
        port: int,
    ) -> tuple[Verdict, MatchedRule, list[tuple[IpCandidate, Verdict, MatchedRule]]]:
        """对整组候选做显式混合集判定。

        返回 ``(总判定, 决定性匹配, 每候选明细)``。
        任一候选 deny → 整体 deny（决定性匹配取**敏感度最高**的被禁候选，
        使报告反映集合中最危险的目标；同敏感度按候选顺序取先）。
        全部 allow → 整体 allow。空集由调用方按 DNS 错误处理。
        """

        per: list[tuple[IpCandidate, Verdict, MatchedRule]] = []
        decisive: MatchedRule | None = None
        best_score = -1
        for cand in candidates:
            verdict, match = self.verdict_for(cand, host, port)
            per.append((cand, verdict, match))
            if verdict is Verdict.DENY:
                score = self._severity_of(match.rule_id)
                if score > best_score:
                    best_score, decisive = score, match
        if decisive is not None:
            return Verdict.DENY, decisive, per
        # 全部允许：决定性匹配取第一个候选的 allow 规则（用于证据）
        return per[0][1], per[0][2], per


def _to_match(rule: Rule, target: str) -> MatchedRule:
    return MatchedRule(
        rule_id=rule.rule_id,
        action=rule.action,
        target=target,
        detail=rule.rationale or f"规则 {rule.rule_id} {rule.action}",
    )
