"""工作区快照：HEAD + 脏文件内容哈希；两次快照之差即"本回合触及的文件"。

快照只哈希 ``git status`` 报出的路径（含未跟踪文件），干净文件由 HEAD 代表，
因此开销与改动量成正比而非与仓库大小成正比。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

ABSENT = "<absent>"
_HASH_CAP = 20 * 1024 * 1024


def git(repo: Path, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def repo_root(path: Path) -> Path | None:
    proc = git(path, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def file_sha(path: Path) -> str:
    # Windows 上无读权限的未跟踪路径（如 pip 临时目录）会让 is_file/stat 抛 PermissionError；
    # 一个不可读文件不能拖垮整个回合基线。
    try:
        if not path.is_file():
            return ABSENT
        st = path.stat()
        if st.st_size > _HASH_CAP:
            return f"size:{st.st_size}:mtime:{st.st_mtime_ns}"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"unreadable:{exc.__class__.__name__}"


def _dirty_paths(repo: Path) -> list[str] | None:
    proc = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if proc is None or proc.returncode != 0:
        return None
    parts = proc.stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in code or "C" in code:
            if i < len(parts) and parts[i]:
                paths.append(parts[i])
            i += 1
    return paths


def snapshot(repo: Path) -> dict[str, Any] | None:
    head_proc = git(repo, "rev-parse", "HEAD")
    head = head_proc.stdout.strip() if head_proc is not None and head_proc.returncode == 0 else None
    paths = _dirty_paths(repo)
    if paths is None:
        return None
    return {"head": head, "dirty": {p: file_sha(repo / p) for p in paths}}


def touched(repo: Path, base: dict[str, Any], cur: dict[str, Any]) -> set[str]:
    """基线到当前之间内容发生变化的路径集合（含回合内提交进 HEAD 的路径）。"""
    base_dirty: dict[str, str] = base.get("dirty", {})
    cur_dirty: dict[str, str] = cur.get("dirty", {})
    out: set[str] = set()
    for path in set(base_dirty) | set(cur_dirty):
        if base_dirty.get(path) != cur_dirty.get(path):
            out.add(path)
    if base.get("head") and cur.get("head") and base["head"] != cur["head"]:
        proc = git(repo, "diff", "--name-only", base["head"], cur["head"])
        if proc is not None and proc.returncode == 0:
            out.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return out


def norm(path: str) -> str:
    p = path.strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def relativize(path: str, repo: Path) -> str:
    """把绝对路径转成相对仓库根的 posix 路径；仓库外路径原样（规范化后）返回。"""
    p = norm(path)
    try:
        return Path(p).resolve().relative_to(repo.resolve()).as_posix() if Path(p).is_absolute() \
            else p
    except ValueError:
        return p
