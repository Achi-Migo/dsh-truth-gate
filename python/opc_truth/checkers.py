"""核验器：每类声明一个确定性函数。

精度纪律：CONTRADICTED 只能建立在二元硬事实上——路径存在与否、触及集合、AST 定义、
精确值相等、退出码。缺基线、缺回执、找不到键等"证据缺位"情形一律不判证伪。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opc_truth.claims import Claim
from opc_truth.gitstate import ABSENT, git, norm
from opc_truth.ledger import Ledger, Receipt

SUPPORTED = "SUPPORTED"
CONTRADICTED = "CONTRADICTED"
UNPROVEN = "UNPROVEN"
UNVERIFIABLE = "UNVERIFIABLE"
STATUSES = (SUPPORTED, CONTRADICTED, UNPROVEN, UNVERIFIABLE)

TEST_COMMAND_RE = re.compile(
    r"(?:\bpytest\b|-m\s+pytest|\bnpm\s+(?:run\s+)?test\b|\bpnpm\s+(?:run\s+)?test\b|"
    r"\byarn\s+test\b|\bcargo\s+test\b|\bgo\s+test\b|\bvitest\b|\bjest\b|\bmvn\s+test\b|"
    r"\bgradle\w*\s+test\b|run_all\.ps1|\bunittest\b|\btox\b|\bnox\b)",
    re.IGNORECASE,
)
_SUBSET_ARG_RE = re.compile(r"(?:\s|^)(?:[\w./\\-]+\.py|tests?[/\\][\w./\\-]*|-k\s+\S+|::\w+)")
_SUBJECT_ALIASES = {
    "build": re.compile(r"\b(?:build|compile|tsc|make)\b|npm\s+run\s+build|cargo\s+build",
                        re.IGNORECASE),
    "ruff": re.compile(r"\bruff\b|\blint\b|eslint|flake8", re.IGNORECASE),
    "mypy": re.compile(r"\bmypy\b|\bpyright\b|tsc\s+--noEmit", re.IGNORECASE),
}


@dataclass
class ClaimVerdict:
    claim: Claim
    status: str
    reason: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"claim": self.claim.to_dict(), "status": self.status, "reason": self.reason,
                "evidence": self.evidence}


@dataclass
class CheckEnv:
    repo: Path | None
    base: dict[str, Any] | None
    cur: dict[str, Any] | None
    touched: set[str] | None
    shell: list[Receipt]
    edits: list[Receipt]
    baseline_ts: float
    ledger: Ledger | None = None
    session_id: str = ""
    rerun: bool = False
    rerun_timeout_s: float = 300.0
    rerun_commands: list[str] = field(default_factory=list)

    @property
    def last_edit_ts(self) -> float:
        return max((r.ts for r in self.edits), default=0.0)

    def edited_paths(self) -> set[str]:
        out: set[str] = set()
        for r in self.edits:
            if r.path:
                out.add(self.rel(r.path))
        return out

    def rel(self, path: str) -> str:
        p = norm(path)
        if self.repo is not None and Path(p).is_absolute():
            try:
                return Path(p).resolve().relative_to(self.repo.resolve()).as_posix()
            except ValueError:
                return p
        return p

    def abs(self, path: str) -> Path | None:
        p = Path(norm(path))
        if p.is_absolute():
            return p
        return self.repo / p if self.repo is not None else None


def _v(claim: Claim, status: str, reason: str, *evidence: str) -> ClaimVerdict:
    return ClaimVerdict(claim, status, reason, list(evidence))


def _base_state(env: CheckEnv, rel: str) -> str | None:
    """基线时该路径的状态：ABSENT / 内容哈希 / 'clean'（被 HEAD 代表）；无基线返回 None。"""
    if env.base is None or env.repo is None:
        return None
    dirty: dict[str, str] = env.base.get("dirty", {})
    if rel in dirty:
        return dirty[rel]
    head = env.base.get("head")
    if head:
        proc = git(env.repo, "cat-file", "-e", f"{head}:{rel}")
        if proc is not None and proc.returncode == 0:
            return "clean"
    return ABSENT


def _touched(env: CheckEnv) -> set[str] | None:
    if env.touched is None:
        return None
    return env.touched | env.edited_paths()


def _resolve(env: CheckEnv, subject: str) -> str:
    """把 Agent 口中的路径落到仓库相对路径；裸文件名按触及集合、再按仓库文件唯一匹配。"""
    rel = env.rel(subject)
    if "/" in rel or env.repo is None:
        return rel
    if (env.repo / rel).exists():
        return rel
    pool = (_touched(env) or set()) | env.edited_paths()
    hits = sorted(p for p in pool if p.rsplit("/", 1)[-1] == rel)
    if len(hits) == 1:
        return hits[0]
    proc = git(env.repo, "ls-files", "--cached", "--others", "--exclude-standard")
    if proc is not None and proc.returncode == 0:
        hits = [p for p in proc.stdout.splitlines() if p.rsplit("/", 1)[-1] == rel]
        if len(hits) == 1:
            return hits[0]
    return rel


def check_file_created(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if not claim.subject:
        return _v(claim, UNVERIFIABLE, "未给出文件路径")
    rel = _resolve(env, claim.subject)
    path = env.abs(rel)
    if path is None:
        return _v(claim, UNVERIFIABLE, "无仓库上下文，无法定位路径")
    if not path.exists():
        return _v(claim, CONTRADICTED, f"文件不存在：{rel}", f"fs:{rel}=absent")
    base = _base_state(env, rel)
    if base is None:
        if rel in env.edited_paths():
            return _v(claim, SUPPORTED, "文件存在且有本回合编辑回执", f"edit:{rel}")
        return _v(claim, UNVERIFIABLE, "文件存在，但缺回合基线，无法判断是否本回合新建")
    if base == ABSENT:
        return _v(claim, SUPPORTED, "基线时不存在、现在存在", f"baseline:{rel}=absent", f"fs:{rel}")
    touched = _touched(env) or set()
    if rel in touched:
        return _v(claim, SUPPORTED, "文件本回合被写入（基线时已存在，按触及判定）", f"diff:{rel}")
    return _v(claim, CONTRADICTED, f"文件在基线时已存在且本回合未被触及：{rel}",
              f"baseline:{rel}=present", "diff:untouched")


def check_file_modified(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if not claim.subject:
        return _v(claim, UNVERIFIABLE, "未给出文件路径")
    rel = _resolve(env, claim.subject)
    touched = _touched(env)
    if rel in env.edited_paths() or (touched is not None and rel in touched):
        return _v(claim, SUPPORTED, "本回合触及该文件", f"diff:{rel}")
    if touched is None:
        return _v(claim, UNVERIFIABLE, "缺回合基线，无法判断是否触及")
    path = env.abs(rel)
    if path is None or (not path.exists() and _base_state(env, rel) == ABSENT):
        return _v(claim, UNVERIFIABLE, f"无法把 {claim.subject} 定位到仓库中的文件")
    return _v(claim, CONTRADICTED, f"本回合未触及该文件：{rel}",
              "diff:" + (",".join(sorted(touched)[:10]) or "(空)"))


def check_file_deleted(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if not claim.subject:
        return _v(claim, UNVERIFIABLE, "未给出文件路径")
    rel = _resolve(env, claim.subject)
    path = env.abs(rel)
    if path is None:
        return _v(claim, UNVERIFIABLE, "无仓库上下文，无法定位路径")
    if path.exists():
        return _v(claim, CONTRADICTED, f"文件仍然存在：{rel}", f"fs:{rel}=present")
    base = _base_state(env, rel)
    if base == ABSENT:
        return _v(claim, UNVERIFIABLE, "文件当前不存在，但基线时也不存在（谈不上删除）",
                  f"baseline:{rel}=absent")
    return _v(claim, SUPPORTED, "文件当前不存在", f"fs:{rel}=absent")


def _glob_match(path: str, pattern: str) -> bool:
    pat = norm(pattern)
    if pat.endswith("/"):
        return path.startswith(pat)
    if any(ch in pat for ch in "*?["):
        return Path(path).match(pat)
    return path == pat or path.startswith(pat.rstrip("/") + "/")


def check_only_changed(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if not claim.subject:
        return _v(claim, UNVERIFIABLE, "未给出范围")
    touched = _touched(env)
    if touched is None:
        return _v(claim, UNVERIFIABLE, "缺回合基线，无法计算触及集合")
    allowed = [s.strip() for s in re.split(r"[,，;；\s]+", claim.subject) if s.strip()]
    extras = sorted(p for p in touched if not any(_glob_match(p, a) for a in allowed))
    if extras:
        return _v(claim, CONTRADICTED, "触及了声明范围之外的文件：" + ", ".join(extras[:8]),
                  "diff:" + ",".join(sorted(touched)[:20]))
    if not touched:
        return _v(claim, CONTRADICTED, "本回合没有触及任何文件", "diff:(空)")
    return _v(claim, SUPPORTED, "触及集合全部落在声明范围内", "diff:" + ",".join(sorted(touched)))


def _match_subject(receipt: Receipt, subject: str | None) -> bool:
    if not subject:
        return True
    cmd = receipt.command or ""
    alias = _SUBJECT_ALIASES.get(subject.lower())
    if alias is not None:
        return bool(alias.search(cmd))
    return subject.lower() in cmd.lower()


def _receipt_ref(r: Receipt) -> str:
    code = r.exit_code if r.exit_code is not None else ("nonzero" if r.status == "fail" else "?")
    return f"receipt:{r.tool_use_id or '-'} `{(r.command or '')[:80]}` exit={code}"


def _evidence_ref_check(claim: Claim, env: CheckEnv) -> ClaimVerdict | None:
    if not claim.evidence_ref:
        return None
    ref = claim.evidence_ref
    for r in env.shell:
        if r.tool_use_id == ref or (ref.startswith("cmd:") and ref[4:].strip().lower()
                                    in (r.command or "").lower()):
            return None
    return _v(claim, CONTRADICTED, f"引用的回执不存在：{ref}", "receipts:no-match")


def check_command_succeeded(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    bad_ref = _evidence_ref_check(claim, env)
    if bad_ref is not None:
        return bad_ref
    matches = [r for r in env.shell if _match_subject(r, claim.subject)]
    if not matches:
        return _v(claim, UNPROVEN, "没有与该声明匹配的命令运行回执；请实际运行该命令")
    latest = matches[-1]
    if latest.status == "ok" and latest.exit_code in (0, None):
        return _v(claim, SUPPORTED, "最近一次匹配命令退出码为 0", _receipt_ref(latest))
    if latest.status == "fail" or (latest.exit_code not in (None, 0)):
        return _v(claim, CONTRADICTED, "最近一次匹配命令以非零状态退出", _receipt_ref(latest))
    return _v(claim, UNPROVEN, "匹配命令的退出状态未知（可能在后台运行）", _receipt_ref(latest))


def _is_subset_run(command: str) -> bool:
    return bool(_SUBSET_ARG_RE.search(command))


def _current_tree_fp(env: CheckEnv) -> str | None:
    if env.repo is None:
        return None
    from opc_truth.receipt_parse import tree_fingerprint
    return tree_fingerprint(env.repo)


def check_tests_pass(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    bad_ref = _evidence_ref_check(claim, env)
    if bad_ref is not None:
        return bad_ref
    runs = [r for r in env.shell if r.command and TEST_COMMAND_RE.search(r.command)
            and _match_subject(r, claim.subject)]
    cur_fp = _current_tree_fp(env)
    if cur_fp and any(r.tree_fp for r in runs):
        fresh = [r for r in runs if r.tree_fp and r.tree_fp == cur_fp]
        stale_fp = [r for r in runs if r.tree_fp and r.tree_fp != cur_fp]
    else:
        # 无指纹的旧回执退回按编辑时间判断（兼容 v0.1 账本）
        fresh = [r for r in runs if r.ts >= env.last_edit_ts]
        stale_fp = []
    if not fresh:
        rerun = _rerun_tests(claim, env)
        if rerun is not None:
            return rerun
        if stale_fp or runs:
            why = "测试时的工作区指纹与当前不一致" if stale_fp else "测试回执早于最后一次编辑"
            return _v(claim, UNPROVEN, f"{why}，已过期；请在改动后重新运行",
                      _receipt_ref((stale_fp or runs)[-1]))
        return _v(claim, UNPROVEN, "没有测试运行回执；请实际运行测试")
    latest = fresh[-1]
    if latest.status == "fail" or latest.exit_code not in (0, None):
        ev = [_receipt_ref(latest)]
        if latest.summary:
            ev.append(f"summary:{latest.summary[:120]}")
        return _v(claim, CONTRADICTED, "最近一次测试运行失败", *ev)
    if latest.status != "ok":
        return _v(claim, UNPROVEN, "测试运行退出状态未知（可能被管道掩盖）", _receipt_ref(latest))
    if claim.scope_all and _is_subset_run(latest.command or ""):
        return _v(claim, UNPROVEN, "声明为全部测试通过，但回执只覆盖子集", _receipt_ref(latest))
    note = "指纹一致且" if latest.tree_fp else ""
    return _v(claim, SUPPORTED, f"{note}最近一次测试运行退出码为 0", _receipt_ref(latest))


def _detect_test_command(repo: Path) -> list[str] | None:
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists():
        venv = repo / ".venv" / "Scripts" / "python.exe"
        py = str(venv) if venv.exists() else sys.executable
        return [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            return None
        if "test" in scripts:
            return ["npm", "test", "--silent"]
    return None


def _rerun_tests(claim: Claim, env: CheckEnv) -> ClaimVerdict | None:
    """白名单重跑：只运行配置或项目清单里的命令，绝不运行 Agent 文本里的命令。超时不算失败。"""
    if not env.rerun or env.repo is None or claim.subject:
        return None
    argv: list[str] | None
    if env.rerun_commands:
        argv = env.rerun_commands[0].split()
    else:
        argv = _detect_test_command(env.repo)
    if not argv:
        return None
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=env.repo, capture_output=True, timeout=env.rerun_timeout_s)
    except subprocess.TimeoutExpired:
        return _v(claim, UNPROVEN, f"白名单重跑超时（{env.rerun_timeout_s:.0f}s），超时不算失败")
    except OSError as exc:
        return _v(claim, UNPROVEN, f"白名单重跑无法启动：{exc}")
    receipt = Receipt(
        session_id=env.session_id or "truth-rerun", kind="shell", ts=started,
        tool_use_id="truth-rerun", command="[truth-rerun] " + " ".join(argv),
        cwd=str(env.repo), status="ok" if proc.returncode == 0 else "fail",
        exit_code=proc.returncode,
    )
    if env.ledger is not None:
        env.ledger.add_receipt(receipt)
    if proc.returncode == 0:
        return _v(claim, SUPPORTED, "核验层独立重跑白名单测试命令，退出码 0", _receipt_ref(receipt))
    return _v(claim, CONTRADICTED, "核验层独立重跑白名单测试命令失败", _receipt_ref(receipt))


def check_committed(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if env.repo is None:
        return _v(claim, UNVERIFIABLE, "非 git 仓库")
    sha = claim.subject if claim.subject and re.fullmatch(r"[0-9a-f]{7,40}", claim.subject) \
        else None
    if sha:
        kind = git(env.repo, "cat-file", "-t", sha)
        if kind is None or kind.returncode != 0 or kind.stdout.strip() != "commit":
            return _v(claim, CONTRADICTED, f"提交 {sha} 不存在", f"git:cat-file {sha}=missing")
        anc = git(env.repo, "merge-base", "--is-ancestor", sha, "HEAD")
        if anc is not None and anc.returncode == 1:
            return _v(claim, CONTRADICTED, f"提交 {sha} 不在当前分支历史中", "git:not-ancestor")
        return _v(claim, SUPPORTED, f"提交 {sha} 存在且在当前分支上", f"git:{sha}")
    if env.base is None or env.cur is None:
        return _v(claim, UNVERIFIABLE, "缺回合基线，无法判断本回合是否提交")
    if env.base.get("head") == env.cur.get("head"):
        return _v(claim, CONTRADICTED, "本回合 HEAD 未变化，没有新提交",
                  f"git:HEAD={str(env.cur.get('head'))[:12]}")
    leftover = sorted(p for p in (env.edited_paths()) if p in env.cur.get("dirty", {}))
    note = f"（仍有未提交改动：{', '.join(leftover[:5])}）" if leftover else ""
    return _v(claim, SUPPORTED, "本回合 HEAD 已前进" + note, f"git:HEAD={env.cur['head'][:12]}")


def check_branch_is(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if env.repo is None or not claim.subject:
        return _v(claim, UNVERIFIABLE, "缺仓库或分支名")
    proc = git(env.repo, "rev-parse", "--abbrev-ref", "HEAD")
    if proc is None or proc.returncode != 0:
        return _v(claim, UNVERIFIABLE, "无法读取当前分支")
    branch = proc.stdout.strip()
    if branch == claim.subject.strip():
        return _v(claim, SUPPORTED, f"当前分支为 {branch}", f"git:branch={branch}")
    return _v(claim, CONTRADICTED, f"当前分支为 {branch}，不是 {claim.subject}",
              f"git:branch={branch}")


def check_pushed(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    if env.repo is None:
        return _v(claim, UNVERIFIABLE, "非 git 仓库")
    remotes = git(env.repo, "branch", "-r")
    if remotes is None or remotes.returncode != 0 or not remotes.stdout.strip():
        return _v(claim, UNVERIFIABLE, "没有远端跟踪分支，无法离线核对推送")
    contains = git(env.repo, "branch", "-r", "--contains", "HEAD")
    if contains is not None and contains.returncode == 0 and contains.stdout.strip():
        return _v(claim, SUPPORTED, "HEAD 已包含在远端跟踪分支中",
                  "git:" + contains.stdout.strip().splitlines()[0].strip())
    return _v(claim, CONTRADICTED, "HEAD 不在任何远端跟踪分支中（本地视角未推送）",
              "git:branch -r --contains HEAD=(空)")


def _split_subject(subject: str | None) -> tuple[str, str] | None:
    if not subject or "::" not in subject:
        return None
    path, name = subject.split("::", 1)
    return path.strip(), name.strip()


def _python_defs(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                full = f"{prefix}{child.name}"
                names.add(full)
                walk(child, full + ".")
            elif isinstance(child, ast.Assign) and not prefix:
                for t in child.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(child, ast.AnnAssign) and not prefix and isinstance(child.target,
                                                                                 ast.Name):
                names.add(child.target.id)

    walk(tree, "")
    return names


def check_symbol_exists(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    parts = _split_subject(claim.subject)
    if parts is None:
        return _v(claim, UNVERIFIABLE, "subject 须为 `路径::符号` 形式")
    rel, name = parts
    path = env.abs(rel)
    if path is None:
        return _v(claim, UNVERIFIABLE, "无仓库上下文")
    if not path.exists():
        return _v(claim, CONTRADICTED, f"文件不存在：{rel}", f"fs:{rel}=absent")
    if path.suffix != ".py":
        return _v(claim, UNVERIFIABLE, "目前只对 Python 文件做 AST 核验")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return _v(claim, UNVERIFIABLE, f"文件无法解析：{exc.__class__.__name__}")
    if name in _python_defs(tree):
        return _v(claim, SUPPORTED, f"AST 中存在定义 {name}", f"ast:{rel}::{name}")
    return _v(claim, CONTRADICTED, f"AST 中找不到定义 {name}", f"ast:{rel}::{name}=absent")


def _lookup_dotted(data: Any, key: str) -> tuple[bool, Any]:
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _read_config_value(path: Path, key: str) -> tuple[bool, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return _lookup_dotted(json.loads(text), key)
    if suffix == ".toml":
        return _lookup_dotted(tomllib.loads(text), key)
    if suffix == ".py":
        tree = ast.parse(text)
        for node in tree.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for t in targets:
                if isinstance(t, ast.Name) and t.id == key and value is not None:
                    try:
                        return True, ast.literal_eval(value)
                    except ValueError:
                        return True, ast.unparse(value)
        return False, None
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([\w.\-]+)\s*[=:]\s*(.*?)\s*$", line)
        if m and m.group(1) == key:
            return True, m.group(2).split(" #")[0].strip().strip("'\"")
    return False, None


def _same_value(actual: Any, expected: str) -> bool:
    a = str(actual).strip().strip("'\"")
    e = expected.strip().strip("'\"")
    if a == e:
        return True
    if isinstance(actual, bool) or a.lower() in ("true", "false"):
        return a.lower() == e.lower()
    try:
        return float(a) == float(e)
    except ValueError:
        return False


def check_config_value(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    parts = _split_subject(claim.subject)
    if parts is None or claim.expected is None:
        return _v(claim, UNVERIFIABLE, "subject 须为 `路径::键` 且给出 expected")
    rel, key = parts
    path = env.abs(rel)
    if path is None or not path.exists():
        return _v(claim, UNVERIFIABLE, f"配置文件不存在或无法定位：{rel}")
    try:
        found, actual = _read_config_value(path, key)
    except (ValueError, SyntaxError, UnicodeDecodeError) as exc:
        return _v(claim, UNVERIFIABLE, f"配置文件无法解析：{exc.__class__.__name__}")
    if not found:
        return _v(claim, UNVERIFIABLE, f"在 {rel} 中找不到键 {key}（可能定义在别处）")
    if _same_value(actual, claim.expected):
        return _v(claim, SUPPORTED, f"{key} = {actual!r}", f"config:{rel}::{key}")
    return _v(claim, CONTRADICTED, f"{key} 实际为 {actual!r}，不是 {claim.expected!r}",
              f"config:{rel}::{key}={actual!r}")


def check_judgment(claim: Claim, env: CheckEnv) -> ClaimVerdict:
    return _v(claim, UNVERIFIABLE, "判断性陈述，没有可对照的客观证据")


CHECKERS: dict[str, Callable[[Claim, CheckEnv], ClaimVerdict]] = {
    "file_created": check_file_created,
    "file_modified": check_file_modified,
    "file_deleted": check_file_deleted,
    "only_changed": check_only_changed,
    "command_succeeded": check_command_succeeded,
    "tests_pass": check_tests_pass,
    "committed": check_committed,
    "branch_is": check_branch_is,
    "pushed": check_pushed,
    "symbol_exists": check_symbol_exists,
    "config_value": check_config_value,
    "judgment": check_judgment,
}
