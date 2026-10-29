"""Truth Gate：编码 Agent 声明核验层。

钩子自动采集签名回执，对 Agent 回复里的声明做确定性对账，四态结论：
SUPPORTED / CONTRADICTED / UNPROVEN / UNVERIFIABLE。判定不经过任何概率模型；
只有二元硬事实（文件存在性、diff 触及集合、AST 符号、精确值、退出码）才能判证伪。
设计与取舍见 docs/decisions/ADR-009-可信回执层TruthGate.md。
"""

ENGINE_VERSION = "truth-gate-0.4.0"
