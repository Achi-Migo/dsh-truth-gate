# @zakza/dsh-truth-gate

Native DeepSeek Harness plugin that verifies coding-agent claims (tests passed, file edited, …) against tool receipts.

- **Default mode: `shadow`** (record only; does not steer).
- Requires **Python ≥ 3.11** on PATH (ships `opc_truth` pure-stdlib engine under `python/`).
- Compatible with DSH **0.2.1-rc.3** and **0.2.1-rc.4**.

## Install

```bash
# from npm (preferred)
dsh plugin --profile <profile> add @zakza/dsh-truth-gate

# or local tarball / checkout
dsh plugin --profile <profile> add ./zakza-dsh-truth-gate-0.1.0.tgz
dsh plugin --profile <profile> add ./dsh-truth-gate
```

## Config

```yaml
# cordis.patch.yml / plugin config
mode: shadow          # off | shadow | gate
python: python        # interpreter
requireProtocol: true
loopLimit: 3
timeoutMs: 15000
```

## Smoke

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke.ps1 -ProfileName truthgate-dev
```
