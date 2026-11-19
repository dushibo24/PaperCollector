# PaperCollector installer for Windows (PowerShell 5.1+).
# Usage: .\install.ps1 [setup options], e.g. .\install.ps1 --no-translation --notify none
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $answer = Read-Host "未找到 uv（Python 环境管理器）。现在从 https://astral.sh/uv 安装？[Y/n]"
    if ($answer -match '^[nN]') {
        Write-Host "请先安装 uv 后重试：https://docs.astral.sh/uv/"
        exit 1
    }
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
# --inexact keeps optional extras (e.g. notes) that setup installed on a previous run.
uv sync --inexact
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run paper-collector setup @args
exit $LASTEXITCODE
