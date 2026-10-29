"""Cursor 钩子分发：stdin 载荷 → 回执/基线/裁决 → stdout JSON。

契约：任何异常都 fail-open（返回该事件的放行形态）并记入账本 errors 表；
stdout 只输出一个 JSON 对象。载荷形态以 docs/evidence/TRUTH-E-P0-HOOK-PROBE-20261028.md 实测为准。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any

from opc_truth import gitstate, signing
from opc_truth.engine import FOLLOWUP_MARKER, TurnInputs, verify_turn
from opc_truth.ledger import Ledger, Receipt, default_home

MODES = ("off", "shadow", "gate")
DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "shadow",
    "loop_limit": 3,
    "rerun": False,
    "rerun_timeout_s": 300,
    "rerun_commands": [],
    "probe_keep": 20,
    "require_protocol": True,
    "tree_fp_on_shell": True,
}
_EDIT_TOOLS = {"Write", "StrReplace", "EditNotebook", "MultiEdit", "Edit"}
_PROBE_EVENTS = {"stop", "afterAgentResponse", "beforeSubmitPrompt"}


def decode_payload(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8-sig", errors="replace").lstrip()
    if not text:
        return {}
    obj, _ = json.JSONDecoder().raw_decode(text)
    return obj if isinstance(obj, dict) else {}


def load_config(home: Path) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    path = home / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError):
            pass
    env_mode = os.environ.get("OPC_TRUTH_MODE", "").strip()
    if env_mode in MODES:
        cfg["mode"] = env_mode
    if cfg.get("mode") not in MODES:
        cfg["mode"] = "shadow"
    return cfg


def save_config(home: Path, cfg: dict[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                      encoding="utf-8")


def _workspace(payload: dict[str, Any]) -> Path:
    roots = payload.get("workspace_roots") or []
    if roots:
        r = str(roots[0])
        if re.match(r"^/[A-Za-z]:/", r):
            r = r[1:]
        return Path(r)
    return Path(payload.get("cwd") or os.getcwd())


def _session(payload: dict[str, Any]) -> str:
    return str(payload.get("conversation_id") or payload.get("session_id") or "unknown")


def _allow_shape(event: str) -> dict[str, Any]:
    if event in ("beforeShellExecution", "beforeReadFile", "beforeMCPExecution", "preToolUse"):
        return {"permission": "allow"}
    if event == "beforeSubmitPrompt":
        return {"continue": True}
    return {}


def _probe(home: Path, event: str, raw: bytes, keep: int) -> None:
    if event not in _PROBE_EVENTS:
        return
    d = home / "probe"
    d.mkdir(parents=True, exist_ok=True)
    if len(list(d.glob(f"{event}-*.json"))) >= keep:
        return
    stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{time.perf_counter_ns() % 1_000_000:06d}"
    (d / f"{event}-{stamp}.json").write_bytes(raw)


def _key_guard(payload: dict[str, Any], event: str) -> dict[str, Any] | None:
    key_dir = str(signing.key_path().parent).lower().replace("/", "\\")
    probe = ""
    if event == "beforeReadFile":
        probe = str(payload.get("file_path") or "")
    elif event == "beforeShellExecution":
        probe = str(payload.get("command") or "")
    normalized = probe.lower().replace("/", "\\")
    if key_dir and (key_dir in normalized or "opc_truth\\hmac.key" in normalized):
        return {
            "permission": "deny",
            "user_message": "Truth Gate：已拒绝访问回执签名密钥。",
            "agent_message": "该路径存放核验层的签名密钥，Agent 不得读取。",
        }
    return None


def _extract_text(payload: dict[str, Any]) -> str | None:
    for key in ("text", "response", "last_assistant_message", "message", "content"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def _transcript_last_text(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    last: str | None = None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("role") == "user":
            last = None
            continue
        if row.get("role") != "assistant":
            continue
        for part in (row.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                last = str(part["text"])
    return last


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def handle(event: str, payload: dict[str, Any], raw: bytes = b"") -> dict[str, Any]:
    repo_guess = _workspace(payload)
    home = default_home(repo_guess)
    cfg = load_config(home)
    guard = _key_guard(payload, event) if event in ("beforeReadFile",
                                                     "beforeShellExecution") else None
    if guard is not None:
        return guard
    if cfg["mode"] == "off":
        return _allow_shape(event)
    try:
        _probe(home, event, raw, int(cfg.get("probe_keep", 20)))
    except OSError:
        pass
    ledger = Ledger(home / "ledger.sqlite")
    try:
        return _dispatch(event, payload, ledger, repo_guess, cfg, home)
    except Exception:  # noqa: BLE001 - fail-open 是契约
        ledger.log_error(event, traceback.format_exc())
        return _allow_shape(event)
    finally:
        ledger.close()


def _dispatch(event: str, payload: dict[str, Any], ledger: Ledger, workspace: Path,
              cfg: dict[str, Any], home: Path) -> dict[str, Any]:
    session = _session(payload)
    now = time.time()
    if event == "beforeSubmitPrompt":
        prompt = str(payload.get("prompt") or payload.get("text") or "")
        if not prompt.lstrip().startswith(FOLLOWUP_MARKER):
            repo = gitstate.repo_root(workspace)
            snap = gitstate.snapshot(repo) if repo is not None else None
            ledger.set_baseline(session, str(repo) if repo else None, snap, ts=now)
        return {"continue": True}
    if event in ("postToolUse", "postToolUseFailure"):
        _record_tool(event, payload, ledger, session, now, workspace,
                     tree_fp_on_shell=bool(cfg.get("tree_fp_on_shell", True)))
        return {}
    if event == "afterFileEdit":
        path = str(payload.get("file_path") or "")
        if path:
            ledger.add_receipt(Receipt(
                session_id=session, kind="edit", ts=now, path=path,
                content_sha=gitstate.file_sha(Path(path)),
            ))
        return {}
    if event == "afterAgentResponse":
        text = _extract_text(payload)
        if text:
            ledger.cache_response(session, payload.get("generation_id"), text)
        return {}
    if event == "subagentStop":
        child = payload.get("child_conversation_id")
        parent = payload.get("parent_conversation_id") or payload.get("conversation_id")
        if child and parent:
            ledger.link(str(parent), str(child))
        return {}
    if event == "stop":
        return _on_stop(payload, ledger, session, workspace, cfg, home)
    return _allow_shape(event)


def _record_tool(event: str, payload: dict[str, Any], ledger: Ledger, session: str,
                 now: float, workspace: Path | None = None,
                 tree_fp_on_shell: bool = True) -> None:
    from opc_truth.receipt_parse import enrich_shell

    tool = str(payload.get("tool_name") or "")
    tinput = payload.get("tool_input") or {}
    if tool == "Shell":
        exit_code: int | None = None
        output = ""
        if event == "postToolUseFailure":
            output = str(payload.get("error_message") or "")
            exit_code = 1
        else:
            try:
                out = json.loads(payload.get("tool_output") or "{}")
            except (TypeError, json.JSONDecodeError):
                out = {}
            if isinstance(out, dict):
                output = str(out.get("output") or "")
                if isinstance(out.get("exitCode"), int):
                    exit_code = out["exitCode"]
        repo = gitstate.repo_root(workspace) if workspace is not None else None
        fields = enrich_shell(
            command=str(tinput.get("command") or ""), exit_code=exit_code, output=output,
            repo=repo, with_tree_fp=tree_fp_on_shell,
        )
        ledger.add_receipt(Receipt(
            session_id=session, kind="shell", ts=now, tool_use_id=payload.get("tool_use_id"),
            command=str(tinput.get("command") or ""), cwd=str(tinput.get("cwd") or ""),
            **fields,
        ))
    elif tool == "Delete" and event == "postToolUse":
        path = str(tinput.get("path") or tinput.get("file_path") or "")
        if path:
            ledger.add_receipt(Receipt(session_id=session, kind="delete", ts=now, path=path,
                                       content_sha=gitstate.ABSENT))
    elif tool in _EDIT_TOOLS and event == "postToolUse":
        path = str(tinput.get("path") or tinput.get("file_path") or "")
        if path:
            ledger.add_receipt(Receipt(session_id=session, kind="edit", ts=now, path=path,
                                       content_sha=gitstate.file_sha(Path(path))))


def _on_stop(payload: dict[str, Any], ledger: Ledger, session: str, workspace: Path,
             cfg: dict[str, Any], home: Path) -> dict[str, Any]:
    base = ledger.baseline(session)
    since = base.ts if base is not None else 0.0
    text = ledger.cached_response(session, since) or _extract_text(payload) \
        or _transcript_last_text(payload.get("transcript_path"))
    if not text:
        ledger.log_error("stop", "no_response_text: 无法取得最终回复文本，本回合未核验")
        return {}
    repo = gitstate.repo_root(workspace)
    inputs = TurnInputs(
        repo=repo, ledger=ledger, session_id=session, rerun=bool(cfg.get("rerun")),
        rerun_timeout_s=float(cfg.get("rerun_timeout_s", 300)),
        rerun_commands=list(cfg.get("rerun_commands") or []),
        require_protocol=bool(cfg.get("require_protocol", True)),
    )
    report = verify_turn(text, inputs)
    mode = str(cfg["mode"])
    limit = int(cfg.get("loop_limit", 3))
    prior = ledger.consecutive_blocks(session, since) if mode == "gate" else 0
    decision = report.decision
    out: dict[str, Any] = {}
    bounce = 0
    if mode == "gate" and decision == "block":
        if prior < limit:
            bounce = prior + 1
            out = {"followup_message": report.followup(bounce, limit)}
        else:
            decision = "blocked_to_human"
    ledger.add_verdict(session, payload.get("generation_id"), mode, decision, bounce,
                       report.latency_ms, report.to_dict())
    health = _health_banner(ledger, session, since)
    receipt_md = home / "last_receipt.md"
    receipt_md.write_text(
        f"# Truth Gate 回执（{time.strftime('%Y-%m-%d %H:%M:%S')}，模式 {mode}）\n\n"
        + (health + "\n\n" if health else "")
        + ("**已转人工（BLOCKED）：连续退回达到上限，下列问题仍未解决。**\n\n"
           if decision == "blocked_to_human" else "")
        + report.render() + "\n",
        encoding="utf-8",
    )
    return out


def _health_banner(ledger: Ledger, session: str, since: float) -> str:
    errors = ledger.conn.execute(
        "SELECT COUNT(*) FROM errors WHERE ts>=?", (since,)
    ).fetchone()[0]
    receipts = ledger.conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE session_id=? AND ts>=?", (session, since)
    ).fetchone()[0]
    verdicts = ledger.conn.execute(
        "SELECT COUNT(*) FROM verdicts WHERE session_id=? AND ts>=?", (session, since)
    ).fetchone()[0]
    if errors or (receipts and not verdicts and since > 0):
        return (f"> **健康告警**：本回合钩子报错 {errors}，回执 {receipts}，裁决 {verdicts}。"
                " 若长期有回执无裁决，请查 `errors` 表。")
    return ""
