"""在线账本统计（D4 影子运行的指标来源）+ 健康告警。"""

from __future__ import annotations

import json
import time
from typing import Any

from opc_truth.ledger import Ledger


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def compute(ledger: Ledger, window_days: float | None = None) -> dict[str, Any]:
    since = time.time() - window_days * 86400 if window_days else 0.0
    rows = ledger.conn.execute(
        "SELECT session_id, ts, mode, decision, bounce, latency_ms, report FROM verdicts"
        " WHERE ts>=? ORDER BY session_id, ts, id", (since,)
    ).fetchall()
    status_counts: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    decisions: dict[str, int] = {}
    latencies: list[float] = []
    bounced = recovered = escalated = 0
    prev: dict[str, str] = {}
    turns_with_unverifiable = 0
    for session, _ts, _mode, decision, _bounce, latency, report_json in rows:
        decisions[decision] = decisions.get(decision, 0) + 1
        latencies.append(float(latency or 0.0))
        report = json.loads(report_json or "{}")
        has_unv = False
        for v in report.get("verdicts", []):
            st = v["status"]
            status_counts[st] = status_counts.get(st, 0) + 1
            t = v["claim"]["type"]
            by_type.setdefault(t, {}).setdefault(st, 0)
            by_type[t][st] += 1
            has_unv = has_unv or st == "UNVERIFIABLE"
        turns_with_unverifiable += int(has_unv)
        last = prev.get(session)
        if last == "block":
            bounced += 1
            if decision == "pass":
                recovered += 1
            elif decision == "blocked_to_human":
                escalated += 1
        prev[session] = decision
    receipts = ledger.conn.execute(
        "SELECT kind, status, COUNT(*) FROM receipts WHERE ts>=? GROUP BY kind, status", (since,)
    ).fetchall()
    errors = ledger.conn.execute("SELECT COUNT(*) FROM errors WHERE ts>=?", (since,)).fetchone()[0]
    receipt_n = ledger.conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE ts>=?", (since,)
    ).fetchone()[0]
    # 有回执的会话却没有任何裁决 → 影子空转信号
    orphan_sessions = ledger.conn.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT r.session_id FROM receipts r WHERE r.ts>=?"
        " AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.session_id=r.session_id AND v.ts>=?)"
        " GROUP BY r.session_id)",
        (since, since),
    ).fetchone()[0]
    total_claims = sum(status_counts.values())
    decidable = status_counts.get("SUPPORTED", 0) + status_counts.get("CONTRADICTED", 0)
    alert = bool(errors) or (receipt_n > 0 and len(rows) == 0)
    return {
        "turns_checked": len(rows),
        "decisions": decisions,
        "claims": status_counts,
        "claims_by_type": by_type,
        "decidable_coverage": round(decidable / total_claims, 4) if total_claims else None,
        "turns_needing_human": turns_with_unverifiable + decisions.get("blocked_to_human", 0),
        "bounce_followups": bounced,
        "bounce_recovered": recovered,
        "bounce_escalated": escalated,
        "bounce_recovery_rate": round(recovered / bounced, 4) if bounced else None,
        "latency_ms_p50": round(_pct(latencies, 0.5), 1),
        "latency_ms_p95": round(_pct(latencies, 0.95), 1),
        "receipts": {f"{k}:{s}": n for k, s, n in receipts},
        "hook_errors": errors,
        "health": {
            "hook_errors": errors,
            "receipts": receipt_n,
            "verdicts": len(rows),
            "sessions_with_receipts_no_verdict": orphan_sessions,
            "alert": alert,
            "reason": ("hook_errors>0 or receipts_without_verdicts"
                       if alert else "ok"),
        },
    }
