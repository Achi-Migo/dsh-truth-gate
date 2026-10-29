"""中立 JSON 桥：供 DSH 原生插件（或其它宿主）经 stdin/stdout 调用。

协议（单条 JSON 对象）：
- ``{"op":"ping"}``
- ``{"op":"baseline","session":"...","repo":"..."}``
- ``{"op":"receipt","session":"...","kind":"shell|edit|delete", ...}``
- ``{"op":"verify","session":"...","repo":"...","text":"...","mode":"shadow|gate|off",
     "require_protocol":true}``

任何异常返回 ``{"ok":false,"decision":"pass","error":"..."}``（fail-open）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from opc_truth import gitstate
from opc_truth.engine import TurnInputs, verify_turn
from opc_truth.ledger import Ledger, Receipt, default_home
from opc_truth.receipt_parse import enrich_shell


def _decode(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ValueError("empty input")
    obj, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(obj, dict):
        raise ValueError("payload must be a JSON object")
    return obj


def _fail(err: str) -> dict[str, Any]:
    return {"ok": False, "decision": "pass", "error": err}


def _ledger(home: Path) -> Ledger:
    return Ledger(home / "ledger.sqlite")


def _op_baseline(msg: dict[str, Any], home: Path) -> dict[str, Any]:
    session = str(msg.get("session") or "unknown")
    repo_s = msg.get("repo")
    repo = Path(str(repo_s)) if repo_s else None
    if repo is not None:
        root = gitstate.repo_root(repo)
        repo = root if root is not None else repo
    snap = gitstate.snapshot(repo) if repo is not None else None
    ledger = _ledger(home)
    try:
        ledger.set_baseline(session, str(repo) if repo else None, snap,
                            ts=float(msg.get("ts") or time.time()))
    finally:
        ledger.close()
    return {"ok": True, "op": "baseline", "session": session}


def _op_receipt(msg: dict[str, Any], home: Path) -> dict[str, Any]:
    session = str(msg.get("session") or "unknown")
    kind = str(msg.get("kind") or "")
    if kind not in ("shell", "edit", "delete"):
        raise ValueError(f"unsupported receipt kind: {kind}")
    ts = float(msg.get("ts") or time.time())
    repo_s = msg.get("repo")
    repo = Path(str(repo_s)) if repo_s else None
    if repo is not None:
        root = gitstate.repo_root(repo)
        repo = root if root is not None else repo

    fields: dict[str, Any] = {
        "session_id": session,
        "kind": kind,
        "ts": ts,
        "tool_use_id": msg.get("tool_use_id"),
        "path": msg.get("path"),
        "command": msg.get("command"),
        "cwd": msg.get("cwd"),
        "exit_code": msg.get("exit_code"),
        "status": msg.get("status"),
        "content_sha": msg.get("content_sha"),
        "output_sha": msg.get("output_sha"),
    }
    if kind == "edit" and fields["path"] and repo is not None:
        p = repo / str(fields["path"])
        if p.is_file() and not fields.get("content_sha"):
            fields["content_sha"] = gitstate.file_sha(p)
    if kind == "shell":
        enriched = enrich_shell(
            command=str(fields.get("command") or ""),
            exit_code=fields.get("exit_code") if fields.get("exit_code") is not None else None,
            output=str(msg.get("output") or ""),
            repo=repo,
            with_tree_fp=bool(msg.get("tree_fp", True)),
        )
        fields.update({k: v for k, v in enriched.items() if v is not None or k == "exit_code"})
        if fields.get("status") is None:
            fields["status"] = enriched.get("status")

    ledger = _ledger(home)
    try:
        # drop None optional noise before Receipt
        keep = ("session_id", "kind", "ts")
        clean = {k: v for k, v in fields.items() if v is not None or k in keep}
        ledger.add_receipt(Receipt(**clean))
    finally:
        ledger.close()
    return {"ok": True, "op": "receipt", "session": session, "kind": kind}


def _op_verify(msg: dict[str, Any], home: Path) -> dict[str, Any]:
    session = str(msg.get("session") or "unknown")
    text = str(msg.get("text") or "")
    mode = str(msg.get("mode") or "shadow")
    repo_s = msg.get("repo")
    repo = Path(str(repo_s)) if repo_s else None
    if repo is not None:
        root = gitstate.repo_root(repo)
        repo = root if root is not None else repo

    ledger = _ledger(home)
    try:
        report = verify_turn(
            text,
            TurnInputs(
                repo=repo,
                ledger=ledger,
                session_id=session,
                require_protocol=bool(msg.get("require_protocol", True)),
                rerun=bool(msg.get("rerun", False)),
            ),
        )
        bounce = int(msg.get("bounce") or 0)
        limit = int(msg.get("loop_limit") or 3)
        followup = None
        if mode == "gate" and report.decision == "block":
            followup = report.followup(max(1, bounce + 1), limit)
        ledger.add_verdict(
            session_id=session,
            generation_id=str(msg.get("generation_id") or ""),
            mode=mode,
            decision=report.decision if mode != "off" else "pass",
            bounce=bounce,
            latency_ms=report.latency_ms,
            report=report.to_dict(),
        )
        # decision = 宿主是否应拦截；engine_decision = 引擎四态汇总后的 pass/block
        if mode == "gate":
            decision = report.decision
        else:
            decision = "pass"
        out: dict[str, Any] = {
            "ok": True,
            "op": "verify",
            "session": session,
            "mode": mode,
            "decision": decision,
            "engine_decision": report.decision,
            "report": report.to_dict(),
        }
        if followup:
            out["followup"] = followup
        return out
    finally:
        ledger.close()


def handle_message(raw: bytes, *, home: Path | None = None) -> dict[str, Any]:
    try:
        msg = _decode(raw)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"decode: {exc}")
    try:
        op = str(msg.get("op") or "")
        base = home if home is not None else default_home(
            Path(str(msg["repo"])) if msg.get("repo") else Path.cwd())
        if op == "ping":
            return {"ok": True, "op": "ping", "engine": "opc_truth"}
        if op == "baseline":
            return _op_baseline(msg, base)
        if op == "receipt":
            return _op_receipt(msg, base)
        if op == "verify":
            return _op_verify(msg, base)
        return _fail(f"unknown op: {op}")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"{type(exc).__name__}: {exc}")


def main_stdio() -> int:
    raw = sys_stdin()
    out = handle_message(raw)
    import sys
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def sys_stdin() -> bytes:
    import sys
    return sys.stdin.buffer.read()
