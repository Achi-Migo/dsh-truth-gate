"""SQLite 账本：回执、回合基线、父子会话关联、裁决记录、钩子错误。

回执逐条 HMAC 签名；读出时验签，验签失败的回执视同不存在（只能削弱证据、
不能制造证据），并计入 ``tampered`` 计数供报告暴露。

v0.2+ 新增字段 ``summary`` / ``tree_fp`` / ``prev_sig``：值为 None 时不参与签名，
保证旧回执仍可验签；``prev_sig`` 构成会话内哈希链，断链计入 tampered。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from opc_truth import signing

HOME_ENV = "OPC_TRUTH_HOME"
_OPTIONAL_SIGNED = ("summary", "tree_fp", "prev_sig")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL, tool_use_id TEXT, kind TEXT NOT NULL,
  command TEXT, cwd TEXT, status TEXT, exit_code INTEGER,
  path TEXT, content_sha TEXT, output_sha TEXT, ts REAL NOT NULL, sig TEXT NOT NULL,
  summary TEXT, tree_fp TEXT, prev_sig TEXT);
CREATE INDEX IF NOT EXISTS ix_receipts_session ON receipts(session_id, ts);
CREATE TABLE IF NOT EXISTS baselines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL, ts REAL NOT NULL, repo TEXT, snapshot TEXT);
CREATE INDEX IF NOT EXISTS ix_baselines_session ON baselines(session_id, ts);
CREATE TABLE IF NOT EXISTS links(child TEXT PRIMARY KEY, parent TEXT NOT NULL, ts REAL);
CREATE TABLE IF NOT EXISTS responses(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, generation_id TEXT, ts REAL, text TEXT);
CREATE TABLE IF NOT EXISTS verdicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, generation_id TEXT, ts REAL, mode TEXT, decision TEXT,
  bounce INTEGER, latency_ms REAL, report TEXT);
CREATE INDEX IF NOT EXISTS ix_verdicts_session ON verdicts(session_id, ts);
CREATE TABLE IF NOT EXISTS errors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event TEXT, error TEXT);
"""


def default_home(repo: Path | None = None) -> Path:
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        return Path(override)
    root = repo if repo is not None else Path.cwd()
    return root / ".tools" / "truth"


@dataclass
class Receipt:
    session_id: str
    kind: str  # shell | edit | delete
    ts: float
    tool_use_id: str | None = None
    command: str | None = None
    cwd: str | None = None
    status: str | None = None  # ok | fail | unknown（仅 shell）
    exit_code: int | None = None
    path: str | None = None
    content_sha: str | None = None
    output_sha: str | None = None
    summary: str | None = None  # JSON 字符串：测试汇总
    tree_fp: str | None = None
    prev_sig: str | None = None
    sig: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def signed_fields(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("sig")
        data.pop("extra")
        for key in _OPTIONAL_SIGNED:
            if data.get(key) is None:
                data.pop(key, None)
        return data


@dataclass
class Baseline:
    session_id: str
    ts: float
    repo: str | None
    snapshot: dict[str, Any] | None


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=10)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.tampered = 0

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(receipts)").fetchall()}
        for name, decl in (("summary", "TEXT"), ("tree_fp", "TEXT"), ("prev_sig", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE receipts ADD COLUMN {name} {decl}")
        self.conn.commit()

    @classmethod
    def open_default(cls, repo: Path | None = None) -> Ledger:
        return cls(default_home(repo) / "ledger.sqlite")

    def close(self) -> None:
        self.conn.close()

    def _last_sig(self, session_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT sig FROM receipts WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row[0] if row else None

    # --- 回执 ---
    def add_receipt(self, receipt: Receipt) -> Receipt:
        if receipt.prev_sig is None:
            receipt.prev_sig = self._last_sig(receipt.session_id)
        receipt.sig = signing.sign(receipt.signed_fields())
        f = receipt.signed_fields()
        self.conn.execute(
            "INSERT INTO receipts(session_id, tool_use_id, kind, command, cwd, status, exit_code,"
            " path, content_sha, output_sha, ts, sig, summary, tree_fp, prev_sig)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f["session_id"], f.get("tool_use_id"), f["kind"], f.get("command"), f.get("cwd"),
                f.get("status"), f.get("exit_code"), f.get("path"), f.get("content_sha"),
                f.get("output_sha"), f["ts"], receipt.sig,
                f.get("summary"), f.get("tree_fp"), f.get("prev_sig"),
            ),
        )
        self.conn.commit()
        return receipt

    def receipts(self, session_ids: list[str], since: float = 0.0) -> list[Receipt]:
        if not session_ids:
            return []
        marks = ",".join("?" for _ in session_ids)
        rows = self.conn.execute(
            "SELECT session_id, kind, ts, tool_use_id, command, cwd, status, exit_code, path,"
            " content_sha, output_sha, sig, summary, tree_fp, prev_sig FROM receipts"
            f" WHERE session_id IN ({marks}) AND ts >= ? ORDER BY ts, id",
            (*session_ids, since),
        ).fetchall()
        out: list[Receipt] = []
        prev_by_session: dict[str, str] = {}
        for row in rows:
            r = Receipt(
                session_id=row[0], kind=row[1], ts=row[2], tool_use_id=row[3], command=row[4],
                cwd=row[5], status=row[6], exit_code=row[7], path=row[8], content_sha=row[9],
                output_sha=row[10], sig=row[11], summary=row[12], tree_fp=row[13],
                prev_sig=row[14],
            )
            ok = signing.verify(r.signed_fields(), r.sig)
            expected_prev = prev_by_session.get(r.session_id)
            if expected_prev is not None and r.prev_sig is not None and r.prev_sig != expected_prev:
                ok = False
            if ok:
                out.append(r)
                prev_by_session[r.session_id] = r.sig
            else:
                self.tampered += 1
        return out

    # --- 基线 ---
    def set_baseline(self, session_id: str, repo: str | None, snapshot: dict[str, Any] | None,
                     ts: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO baselines(session_id, ts, repo, snapshot) VALUES(?,?,?,?)",
            (session_id, ts if ts is not None else time.time(), repo,
             json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None),
        )
        self.conn.commit()

    def baseline(self, session_id: str) -> Baseline | None:
        row = self.conn.execute(
            "SELECT session_id, ts, repo, snapshot FROM baselines WHERE session_id=?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return Baseline(row[0], row[1], row[2], json.loads(row[3]) if row[3] else None)

    # --- 父子会话 ---
    def link(self, parent: str, child: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO links(child, parent, ts) VALUES(?,?,?)",
            (child, parent, time.time()),
        )
        self.conn.commit()

    def family(self, session_id: str) -> list[str]:
        seen = [session_id]
        frontier = [session_id]
        while frontier:
            marks = ",".join("?" for _ in frontier)
            rows = self.conn.execute(
                f"SELECT child FROM links WHERE parent IN ({marks})", frontier
            ).fetchall()
            frontier = [r[0] for r in rows if r[0] not in seen]
            seen.extend(frontier)
        return seen

    # --- 回复缓存 ---
    def cache_response(self, session_id: str, generation_id: str | None, text: str) -> None:
        self.conn.execute(
            "INSERT INTO responses(session_id, generation_id, ts, text) VALUES(?,?,?,?)",
            (session_id, generation_id, time.time(), text),
        )
        self.conn.commit()

    def cached_response(self, session_id: str, since: float) -> str | None:
        row = self.conn.execute(
            "SELECT text FROM responses WHERE session_id=? AND ts>=? ORDER BY ts DESC, id DESC"
            " LIMIT 1",
            (session_id, since),
        ).fetchone()
        return row[0] if row else None

    # --- 裁决 ---
    def add_verdict(self, session_id: str, generation_id: str | None, mode: str, decision: str,
                    bounce: int, latency_ms: float, report: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO verdicts(session_id, generation_id, ts, mode, decision, bounce,"
            " latency_ms, report) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, generation_id, time.time(), mode, decision, bounce, latency_ms,
             json.dumps(report, ensure_ascii=False)),
        )
        self.conn.commit()

    def consecutive_blocks(self, session_id: str, since: float) -> int:
        rows = self.conn.execute(
            "SELECT decision FROM verdicts WHERE session_id=? AND ts>=? AND mode='gate'"
            " ORDER BY ts DESC, id DESC",
            (session_id, since),
        ).fetchall()
        n = 0
        for (decision,) in rows:
            if decision != "block":
                break
            n += 1
        return n

    def log_error(self, event: str, error: str) -> None:
        self.conn.execute(
            "INSERT INTO errors(ts, event, error) VALUES(?,?,?)", (time.time(), event, error[:4000])
        )
        self.conn.commit()
