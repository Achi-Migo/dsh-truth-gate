# @zakza/dsh-truth-gate

DeepSeek Harness 原生插件：用工具回执核对 Agent「测试通过 / 改了文件」等声明。

- **默认 `shadow`**（只记账，不弹回）
- 需要本机 **Python ≥ 3.11**（包内附带纯标准库 `opc_truth`）
- 兼容 DSH **0.2.1-rc.3 / 0.2.1-rc.4**

## 安装

```bash
dsh plugin --profile <profile> add @zakza/dsh-truth-gate
```

详见英文 README。冒烟：`scripts/smoke.ps1`。
