"""裁决引擎：抽取 → 核验 → 回合结论。

回合放行规则（固定）：
- 任一声明 CONTRADICTED 或 UNPROVEN → block；
- 正文宣称完成、本回合有实际动作（编辑或命令），却没有任何可检声明 → block（无证据的"完成"）；
- 由正文解析出的声明（source=lexicon）最多判 UNPROVEN：解析误差不能变成冤枉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opc_truth import ENGINE_VERSION, gitstate
from opc_truth.checkers import (
    CHECKERS,
    CONTRADICTED,
    STATUSES,
    SUPPORTED,
    UNPROVEN,
    UNVERIFIABLE,
    CheckEnv,
    ClaimVerdict,
)
from opc_truth.claims import CHECKABLE_TYPES, Claim, extract
from opc_truth.ledger import Ledger

FOLLOWUP_MARKER = "[TruthGate]"


@dataclass
class TurnReport:
    verdicts: list[ClaimVerdict]
    decision: str  # pass | block
    reasons: list[str]
    latency_ms: float
    protocol_present: bool
    completion_declared: bool
    tampered_receipts: int = 0
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for v in self.verdicts:
            out[v.status] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_version": ENGINE_VERSION,
            "decision": self.decision,
            "reasons": self.reasons,
            "counts": self.counts(),
            "latency_ms": round(self.latency_ms, 2),
            "protocol_present": self.protocol_present,
            "completion_declared": self.completion_declared,
            "tampered_receipts": self.tampered_receipts,
            "notes": self.notes,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }

    def render(self) -> str:
        c = self.counts()
        lines = [
            f"核验回执（{ENGINE_VERSION}）：已证实 {c[SUPPORTED]} · 已证伪 {c[CONTRADICTED]}"
            f" · 未证明 {c[UNPROVEN]} · 无法验证 {c[UNVERIFIABLE]} · 结论 {self.decision}",
        ]
        tag = {SUPPORTED: "[已证实]", CONTRADICTED: "[已证伪]", UNPROVEN: "[未证明]",
               UNVERIFIABLE: "[无法验证]"}
        for v in self.verdicts:
            subj = f" {v.claim.subject}" if v.claim.subject else ""
            ev = f"；证据：{'; '.join(v.evidence[:3])}" if v.evidence else ""
            lines.append(f"- {tag[v.status]} {v.claim.type}{subj}：{v.reason}{ev}")
        for r in self.reasons:
            if r.startswith("rule:"):
                lines.append(f"- [未证明] {r[5:]}")
        lines.append("注：已证实只表示与证据一致，不等于代码逻辑正确。")
        return "\n".join(lines)

    def followup(self, bounce: int, limit: int) -> str:
        bad = [v for v in self.verdicts if v.status in (CONTRADICTED, UNPROVEN)]
        lines = [
            f"{FOLLOWUP_MARKER} 你的汇报有 {len(bad) + self._rule_count()} 处未通过独立核验"
            f"（第 {bounce}/{limit} 次退回）。",
        ]
        for v in bad:
            label = "已证伪" if v.status == CONTRADICTED else "未证明"
            subj = f" `{v.claim.subject}`" if v.claim.subject else ""
            ev = f"（证据：{'; '.join(v.evidence[:2])}）" if v.evidence else ""
            lines.append(f"- {label}：{v.claim.type}{subj} —— {v.reason}{ev}")
        for r in self.reasons:
            if r.startswith("rule:"):
                lines.append(f"- 未证明：{r[5:]}")
        lines.append(
            "请二选一：实际补齐证据（真正运行命令、真正修改文件）后重新汇报；"
            "或把对应说法改为“未完成/无法确认”。回复末尾的 ```claims 块必须与事实一致。"
        )
        return "\n".join(lines)

    def _rule_count(self) -> int:
        return sum(1 for r in self.reasons if r.startswith("rule:"))


@dataclass
class TurnInputs:
    repo: Path | None
    ledger: Ledger
    session_id: str
    rerun: bool = False
    rerun_timeout_s: float = 300.0
    rerun_commands: list[str] = field(default_factory=list)
    require_protocol: bool = False  # Cursor 场景默认由钩子打开；D2 保持 False


def build_env(inputs: TurnInputs) -> CheckEnv:
    ledger = inputs.ledger
    base = ledger.baseline(inputs.session_id)
    baseline_ts = base.ts if base is not None else 0.0
    family = ledger.family(inputs.session_id)
    receipts = ledger.receipts(family, since=0.0)
    shell = [r for r in receipts if r.kind == "shell"]
    edits = [r for r in receipts if r.kind in ("edit", "delete") and r.ts >= baseline_ts]
    cur = gitstate.snapshot(inputs.repo) if inputs.repo is not None else None
    base_snap = base.snapshot if base is not None else None
    touched = None
    if inputs.repo is not None and base_snap is not None and cur is not None:
        touched = gitstate.touched(inputs.repo, base_snap, cur)
    return CheckEnv(
        repo=inputs.repo, base=base_snap, cur=cur, touched=touched, shell=shell, edits=edits,
        baseline_ts=baseline_ts, ledger=ledger, session_id=inputs.session_id,
        rerun=inputs.rerun, rerun_timeout_s=inputs.rerun_timeout_s,
        rerun_commands=list(inputs.rerun_commands),
    )


def verify_turn(text: str, inputs: TurnInputs, *, use_protocol: bool = True,
                use_lexicon: bool = True) -> TurnReport:
    started = time.perf_counter()
    ex = extract(text, use_protocol=use_protocol, use_lexicon=use_lexicon)
    env = build_env(inputs)
    verdicts: list[ClaimVerdict] = []
    for claim in ex.claims:
        verdict = CHECKERS[claim.type](claim, env)
        if claim.source == "lexicon" and verdict.status == CONTRADICTED:
            verdict = ClaimVerdict(
                claim, UNPROVEN,
                "正文说法与证据矛盾：" + verdict.reason
                + "（由正文解析，需在 claims 块中如实声明）",
                verdict.evidence,
            )
        verdicts.append(verdict)
    reasons: list[str] = []
    for err in ex.protocol_errors:
        reasons.append(f"rule:claims 块有误——{err}")
    active = bool(env.edits) or any(r.ts >= env.baseline_ts for r in env.shell) \
        or bool(env.touched)
    checkable = [v for v in verdicts if v.claim.type in CHECKABLE_TYPES]
    # 无仓库的离线轨迹（D2）不套“空喊完成”规则：第三方 Agent 不走声明协议，
    # 且文件类声明本就只能 UNVERIFIABLE，硬套该规则只会抬高已解决误拦率。
    if (ex.completion_declared and active and not checkable
            and (inputs.repo is not None or inputs.require_protocol)):
        reasons.append("rule:宣称完成，但没有给出任何可核验的声明（文件/测试/命令/提交）")
    if inputs.require_protocol and active and not ex.protocol_present:
        reasons.append("rule:本回合有实际动作，但回复末尾缺少 ```claims 声明块")
    blocking = any(v.status in (CONTRADICTED, UNPROVEN) for v in verdicts) or any(
        r.startswith("rule:") for r in reasons)
    for v in verdicts:
        if v.status in (CONTRADICTED, UNPROVEN):
            reasons.append(f"{v.status}:{v.claim.type}")
    report = TurnReport(
        verdicts=verdicts,
        decision="block" if blocking else "pass",
        reasons=reasons,
        latency_ms=(time.perf_counter() - started) * 1000,
        protocol_present=ex.protocol_present,
        completion_declared=ex.completion_declared,
        tampered_receipts=inputs.ledger.tampered,
    )
    if inputs.ledger.tampered:
        report.notes.append(f"{inputs.ledger.tampered} 条回执验签失败，已按不存在处理")
    return report


def claim_from_dict(d: dict[str, Any]) -> Claim:
    kwargs: dict[str, Any] = {k: d[k] for k in Claim.__dataclass_fields__ if k in d}
    return Claim(**kwargs)
