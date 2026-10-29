"""回执签名：HMAC-SHA256，密钥文件放在工作区之外（默认 %LOCALAPPDATA%/opc_truth/hmac.key）。

与 ``opc_control.signer`` 同算法；不复用其实现，因为它从 env 取密钥且缺省 fail closed，
而钩子进程拿不到该 env。已知边界（写入威胁模型）：密钥与 Agent 同机，
钩子只能拦截 Agent 经 Read/Shell 工具读取密钥路径，拦不住绕开工具面的读取。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

KEY_FILE_ENV = "OPC_TRUTH_KEY_FILE"


def _candidates() -> list[Path]:
    override = os.environ.get(KEY_FILE_ENV, "").strip()
    if override:
        return [Path(override)]
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    # 本机实测：Python 进程对 C:\Users 下新建文件被拒（PowerShell 可写），故备选放工作区所在盘根。
    return [Path(base) / "opc_truth" / "hmac.key",
            Path(Path.cwd().anchor) / ".opc_truth" / "hmac.key"]


def key_path() -> Path:
    cands = _candidates()
    for path in cands:
        if path.exists():
            return path
    return cands[0]


def _load_key() -> bytes:
    for path in _candidates():
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
    last_error: OSError | None = None
    for path in _candidates():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = secrets.token_hex(32).encode("ascii")
            path.write_bytes(data)
            return data
        except OSError as exc:
            last_error = exc
    raise PermissionError(f"无法创建回执签名密钥：{last_error}")


def canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def sign(fields: dict[str, Any]) -> str:
    return hmac.new(_load_key(), canonical(fields), hashlib.sha256).hexdigest()


def verify(fields: dict[str, Any], signature: str) -> bool:
    try:
        given = bytes.fromhex(signature)
    except ValueError:
        return False
    expected = hmac.new(_load_key(), canonical(fields), hashlib.sha256).digest()
    return hmac.compare_digest(given, expected)
