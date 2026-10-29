# KEEP ASCII-ONLY (PowerShell 5.1).
param(
    [string]$ProfileName = "truthgate-dev",
    [string]$PluginRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $PluginRoot) {
    $PluginRoot = Split-Path -Parent $PSScriptRoot
}

function Require-Cmd([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "missing command: $Name"
    }
}

Require-Cmd node
Require-Cmd npm

$python = $env:TRUTHGATE_PYTHON
if (-not $python) {
    $cand = "E:\cursor_ws\0opc\.venv\Scripts\python.exe"
    if (Test-Path $cand) { $python = $cand }
    else { $python = "python" }
}
if (-not (Get-Command $python -ErrorAction SilentlyContinue) -and -not (Test-Path $python)) {
    throw "missing python interpreter: $python"
}

$pyVer = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $pyVer.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    throw "Python >= 3.11 required, got $pyVer ($python)"
}
Write-Host "using python=$python ($pyVer)"

Push-Location $PluginRoot
try {
    if (-not (Test-Path "node_modules\@deepseek-ai\schemastery")) {
        npm install --ignore-scripts 2>&1 | Out-Host
    }
    npm run build 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "build failed" }
    npm test 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "unit tests failed" }

    # Bridge smoke (no DSH required)
    $env:PYTHONPATH = (Join-Path $PluginRoot "python")
    $env:OPC_TRUTH_HOME = Join-Path $PluginRoot ".smoke-truth"
    $payload = '{"op":"ping"}'
    $ping = $payload | & $python -m opc_truth bridge
    if ($ping -notmatch '"ok"\s*:\s*true') { throw "bridge ping failed: $ping" }

    # Prefer installing into an isolated profile when dsh CLI is reachable.
    $dshBin = $env:DSH_BIN
    if (-not $dshBin) {
        $dshBin = "C:\Users\Administrator\AppData\Local\npm-cache\_npx\a3a60f53b42a237b\node_modules\@deepseek-ai\dsh\lib\bin.js"
    }
    if (Test-Path $dshBin) {
        if (-not $env:DSH_HOME) { $env:DSH_HOME = "E:\dsh-home" }
        Write-Host "dsh plugin add -> profile $ProfileName"
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & node $dshBin plugin --profile $ProfileName add $PluginRoot *> $PluginRoot\.smoke-dsh-add.log
        $addCode = $LASTEXITCODE
        Get-Content $PluginRoot\.smoke-dsh-add.log -ErrorAction SilentlyContinue | Out-Host
        if ($addCode -ne 0) {
            $ErrorActionPreference = $prevEap
            throw "dsh plugin add failed exit=$addCode"
        }
        & node $dshBin --profile $ProfileName --dump-config *> $PluginRoot\.smoke-dsh-dump.log
        Select-String -Path $PluginRoot\.smoke-dsh-dump.log -Pattern "truth-gate|dsh-truth-gate" | Out-Host
        $ErrorActionPreference = $prevEap
        if (-not (Select-String -Path $PluginRoot\.smoke-dsh-dump.log -Pattern "truth-gate|dsh-truth-gate" -Quiet)) {
            throw "dump-config missing truth-gate layer"
        }
    } else {
        Write-Host "dsh CLI not found; skipped profile install"
    }

    Write-Host "SMOKE_OK profile=$ProfileName bridge_ping=$ping"
}
finally {
    Pop-Location
}
