"""Shell 回执富化：测试汇总解析、管道假绿识别、可选工作区指纹。

签名兼容：新字段在值为 None 时不参与 HMAC（见 ``Receipt.signed_fields``）。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from opc_truth import gitstate
from opc_truth.checkers import TEST_COMMAND_RE

_PASSED = re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE)
_FAILED = re.compile(r"\b(\d+)\s+failed\b", re.IGNORECASE)
_ERROR = re.compile(r"\b(\d+)\s+error(?:s)?\b", re.IGNORECASE)
_NO_TESTS = re.compile(
    r"\bno tests (?:ran|collected)\b|\bcollected 0 items\b|"
    r"ERROR:\s*file or directory not found",
    re.IGNORECASE,
)
_PIPE_UNTRUSTED = re.compile(
    r"\|\s*(?:Select-Object|head|tail|findstr|grep|Out-String)\b|"
    r"\|\|\s*(?:true|exit\s+0)|;\s*exit\s+0\b",
    re.IGNORECASE,
)


def parse_test_summary(output: str) -> dict[str, Any] | None:
    if not output or not output.strip():
        return None
    if _NO_TESTS.search(output):
        return {"passed": 0, "failed": 0, "errors": 0, "collected": 0, "ok": False}
    passed = _PASSED.search(output)
    failed = _FAILED.search(output)
    errors = _ERROR.search(output)
    if not (passed or failed or errors or "FAILED" in output or "Traceback" in output):
        return None
    p = int(passed.group(1)) if passed else 0
    f = int(failed.group(1)) if failed else (1 if "FAILED" in output else 0)
    e = int(errors.group(1)) if errors else 0
    return {"passed": p, "failed": f, "errors": e, "ok": f == 0 and e == 0 and p > 0}


def command_exit_untrusted(command: str) -> bool:
    return bool(_PIPE_UNTRUSTED.search(command or ""))


def tree_fingerprint(repo: Path | None) -> str | None:
    if repo is None:
        return None
    snap = gitstate.snapshot(repo)
    if snap is None:
        return None
    return hashlib.sha256(
        json.dumps(snap, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def enrich_shell(*, command: str, exit_code: int | None, output: str = "",
                 repo: Path | None = None, with_tree_fp: bool = True) -> dict[str, Any]:
    """返回可直接喂给 ``Receipt`` 的 shell 字段（不含 session/ts/tool_use_id）。"""
    summary = parse_test_summary(output) if TEST_COMMAND_RE.search(command or "") else None
    status: str
    code = exit_code
    if summary is not None:
        status = "ok" if summary.get("ok") else "fail"
        # 汇总可信时覆盖被管道掩盖的退出码观感
        code = 0 if status == "ok" else (exit_code if exit_code not in (None, 0) else 1)
    elif command_exit_untrusted(command):
        status = "unknown"
        code = exit_code
    elif exit_code == 0:
        status = "ok"
    elif exit_code is None:
        status = "unknown"
    else:
        status = "fail"
    out: dict[str, Any] = {
        "status": status,
        "exit_code": code,
        "output_sha": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
        if output else None,
        "summary": json.dumps(summary, ensure_ascii=False, sort_keys=True) if summary else None,
        "tree_fp": tree_fingerprint(repo) if with_tree_fp else None,
    }
    return out
