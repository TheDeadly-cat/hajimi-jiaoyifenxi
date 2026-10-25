param(
    [string]$PythonExecutable,
    [string]$Plan,
    [string]$PlanSha256,
    [string]$Authorization,
    [string]$AuthorizationSha256
)
$ErrorActionPreference = 'Stop'
# No Read-Host or other plaintext completion prompt. Never accept a key parameter.
if (-not $PythonExecutable -or -not $Plan -or -not $PlanSha256 -or -not $Authorization -or -not $AuthorizationSha256) {
    Write-Host '需要新计划及单独批准的授权文件和各自 SHA256；未执行任何请求。'
    exit 2
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    Write-Host '指定的 Python 不存在；未执行任何请求。'
    exit 2
}
& $PythonExecutable -I -S -B -X utf8 (Join-Path $PSScriptRoot 'runner.py') execute --plan $Plan --plan-sha256 $PlanSha256 --authorization $Authorization --authorization-sha256 $AuthorizationSha256 --allow-real-provider
$taskExit = $LASTEXITCODE
Write-Host '本次已结束。此窗口将自动退出；不要在这里粘贴密钥。'
exit $taskExit
