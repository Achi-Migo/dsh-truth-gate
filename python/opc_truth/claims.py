"""声明抽取：协议通道（回复末尾的 ```claims JSON 块）+ 兜底通道（正文中英词典）。

抽取只负责"Agent 说了什么"，不负责"说得对不对"。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from opc_truth.gitstate import norm

CHECKABLE_TYPES = frozenset({
    "file_created", "file_modified", "file_deleted", "only_changed",
    "command_succeeded", "tests_pass", "committed", "branch_is", "pushed",
    "symbol_exists", "config_value",
})
CLAIM_TYPES = CHECKABLE_TYPES | {"judgment"}

_BLOCK_RE = re.compile(r"```claims[ \t]*\r?\n(.*?)```", re.DOTALL)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_CLAUSE_SPLIT = re.compile(r"[。！？!?;；，,\n]|(?<=[a-z0-9`)])\.\s")
_PATHLIKE = re.compile(r"^[\w.\-/\\]+(?:\.\w{1,10}|/)$")


@dataclass
class Claim:
    type: str
    subject: str | None = None
    expected: str | None = None
    evidence_ref: str | None = None
    text: str = ""
    source: str = "protocol"  # protocol | lexicon | rule
    scope_all: bool = False

    def key(self) -> tuple[str, str | None]:
        return (self.type, norm(self.subject) if self.subject else None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Extraction:
    claims: list[Claim]
    protocol_present: bool
    protocol_errors: list[str]
    completion_declared: bool
    body: str


@lru_cache(maxsize=1)
def _lexicon() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (Path(__file__).parent / "data" / "lexicon.json").read_text(encoding="utf-8")
    )
    return data


@lru_cache(maxsize=1)
def _compiled() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str],
                         list[tuple[dict[str, Any], re.Pattern[str]]]]:
    lex = _lexicon()
    neg = re.compile("|".join(lex["negation"]), re.IGNORECASE)
    scope = re.compile("|".join(lex["scope_all"]), re.IGNORECASE)
    done = re.compile("|".join(lex["completion"]), re.IGNORECASE)
    pats = [(entry, re.compile(entry["pattern"], re.IGNORECASE)) for entry in lex["claims"]]
    return neg, scope, done, pats


def parse_protocol(text: str) -> tuple[list[Claim], bool, list[str], str]:
    claims: list[Claim] = []
    errors: list[str] = []
    blocks = list(_BLOCK_RE.finditer(text))
    body = _BLOCK_RE.sub("", text)
    for m in blocks:
        try:
            raw = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            errors.append(f"claims 块不是合法 JSON：{exc.msg}")
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if not isinstance(item, dict) or "type" not in item:
                errors.append(f"claims 条目缺少 type：{str(item)[:80]}")
                continue
            ctype = str(item["type"]).strip()
            if ctype not in CLAIM_TYPES:
                errors.append(f"未知声明类型：{ctype}")
                ctype = "judgment"
            claims.append(Claim(
                type=ctype,
                subject=_opt_str(item.get("subject")),
                expected=_opt_str(item.get("expected")),
                evidence_ref=_opt_str(item.get("evidence_ref")),
                text=str(item.get("text") or item.get("subject") or ctype),
                source="protocol",
                scope_all=bool(item.get("scope_all", False)),
            ))
    return claims, bool(blocks), errors, body


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _scan_text(body: str) -> str:
    text = _FENCE_RE.sub(" ", body)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def scan_lexicon(body: str) -> tuple[list[Claim], bool]:
    neg, scope, done, pats = _compiled()
    text = _scan_text(body)
    found: dict[tuple[str, str | None], Claim] = {}
    completion = False
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if not clause or neg.search(clause):
            continue
        if done.search(clause):
            completion = True
        for entry, pat in pats:
            for m in pat.finditer(clause):
                subject = entry.get("subject")
                if m.groups() and m.group(1):
                    subject = m.group(1).strip()
                    if entry["type"].startswith("file_") or entry["type"] == "only_changed":
                        if not _PATHLIKE.match(subject):
                            continue
                claim = Claim(
                    type=entry["type"], subject=subject, text=clause[:200], source="lexicon",
                    scope_all=bool(entry["type"] == "tests_pass" and scope.search(clause)),
                )
                found.setdefault(claim.key(), claim)
    return list(found.values()), completion


def extract(text: str, *, use_protocol: bool = True, use_lexicon: bool = True) -> Extraction:
    if use_protocol:
        proto, present, errors, body = parse_protocol(text)
    else:
        proto, present, errors, body = [], False, [], _BLOCK_RE.sub("", text)
    lex: list[Claim] = []
    completion = False
    if use_lexicon:
        lex, completion = scan_lexicon(body)
    covered_types = {c.type for c in proto}
    covered_keys = {c.key() for c in proto}
    merged = list(proto)
    for c in lex:
        if c.key() in covered_keys:
            continue
        if c.subject is None and c.type in covered_types:
            continue
        if c.type in ("tests_pass", "committed", "pushed") and c.type in covered_types:
            continue
        merged.append(c)
    return Extraction(merged, present, errors, completion, body)
