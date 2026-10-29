"""命令行：python -m opc_truth {verify,stats,mode,hook}。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from opc_truth import gitstate
from opc_truth.engine import TurnInputs, verify_turn
from opc_truth.hook import MODES, decode_payload, handle, load_config, save_config
from opc_truth.ledger import Ledger, default_home
from opc_truth.stats import compute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opc_truth")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="核验一段回复文本（读取当前会话账本）")
    v.add_argument("--text")
    v.add_argument("--file")
    v.add_argument("--session", default="manual")
    v.add_argument("--repo", default=".")
    v.add_argument("--json", action="store_true")
    s = sub.add_parser("stats", help="在线账本统计")
    s.add_argument("--days", type=float)
    m = sub.add_parser("mode", help="查看或切换模式 off/shadow/gate")
    m.add_argument("value", nargs="?", choices=MODES)
    h = sub.add_parser("hook", help="钩子入口（stdin 为 Cursor 载荷）")
    h.add_argument("event")
    sub.add_parser("bridge", help="中立 JSON 桥（stdin 一条消息 → stdout 一条结论）")
    args = parser.parse_args(argv)

    if args.cmd == "hook":
        raw = sys.stdin.buffer.read()
        sys.stdout.write(json.dumps(handle(args.event, decode_payload(raw), raw),
                                    ensure_ascii=False))
        return 0
    if args.cmd == "bridge":
        from opc_truth.bridge import handle_message
        out = handle_message(sys.stdin.buffer.read())
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if args.cmd == "mode":
        home = default_home(Path.cwd())
        cfg = load_config(home)
        if args.value:
            cfg["mode"] = args.value
            save_config(home, cfg)
        print(cfg["mode"])
        return 0
    if args.cmd == "stats":
        ledger = Ledger.open_default(Path.cwd())
        print(json.dumps(compute(ledger, args.days), ensure_ascii=False, indent=2))
        return 0
    text = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8")
    repo = gitstate.repo_root(Path(args.repo))
    ledger = Ledger.open_default(repo or Path.cwd())
    report = verify_turn(text, TurnInputs(repo=repo, ledger=ledger, session_id=args.session))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json
          else report.render())
    return 0 if report.decision == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
