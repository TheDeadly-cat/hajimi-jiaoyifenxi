[CmdletBinding()]
param([switch]$CheckOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$appUrl = "http://127.0.0.1:8770/"
$readinessUrl = "http://127.0.0.1:8770/api/readiness"
$frontendIndex = Join-Path $projectRoot "frontend\dist\index.html"

function Show-LauncherError {
    param([Parameter(Mandatory = $true)][string]$Message)

    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        "AI Collaboration Studio",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

function Test-StudioReady {
    try {
        $readiness = Invoke-RestMethod -Uri $readinessUrl -Method Get -TimeoutSec 2
        return `
            $readiness.ok -eq $true -and `
            $readiness.ready -eq $true -and `
            $readiness.schema_version -eq "host_readiness_v1" -and `
            $readiness.service.id -eq "ai_collaboration_studio"
    } catch {
        return $false
    }
}

function Test-LocalPort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync("127.0.0.1", 8770)
        return $connect.Wait(500) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Open-StudioWindow {
    $edgePath = @(
        ${env:ProgramFiles(x86)},
        $env:ProgramFiles
    ) | Where-Object { $_ } | ForEach-Object {
        Join-Path $_ "Microsoft\Edge\Application\msedge.exe"
    } | Where-Object {
        Test-Path -LiteralPath $_
    } | Select-Object -First 1

    if ($edgePath) {
        Start-Process -FilePath $edgePath -ArgumentList "--app=$appUrl"
        return
    }

    Start-Process $appUrl
}

if (Test-StudioReady) {
    if (-not $CheckOnly) {
        Open-StudioWindow
    }
    exit 0
}

if ($CheckOnly) {
    exit 2
}

if (Test-LocalPort) {
    Show-LauncherError "Local port 8770 is occupied by another service. The launcher did not stop or replace that process."
    exit 1
}

if (-not (Test-Path -LiteralPath $frontendIndex -PathType Leaf)) {
    Show-LauncherError "The frontend production build is missing. Run npm.cmd run build in the frontend directory first."
    exit 1
}

$managedPython = Join-Path $projectRoot "runtime\bootstrap\python\Scripts\python.exe"
$pythonPath = $null
$pythonArguments = @("server.py")
if (Test-Path -LiteralPath $managedPython -PathType Leaf) {
    $pythonPath = $managedPython
} else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $pythonPath = $pythonCommand.Source
    }
}
if (-not $pythonPath) {
    $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    $pythonArguments = @("-3", "server.py")
    if ($pythonCommand) {
        $pythonPath = $pythonCommand.Source
    }
}
if (-not $pythonPath) {
    Show-LauncherError "Python 3 was not found. Install it or add python.exe to PATH."
    exit 1
}

$logDirectory = Join-Path $projectRoot "runtime\launcher"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdoutPath = Join-Path $logDirectory "server-$stamp.stdout.log"
$stderrPath = Join-Path $logDirectory "server-$stamp.stderr.log"

$serverProcess = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList $pythonArguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
    Start-Sleep -Milliseconds 250
    if (Test-StudioReady) {
        $ready = $true
        break
    }
    if ($serverProcess.HasExited) {
        break
    }
}

if (-not $ready) {
    Show-LauncherError "AI Collaboration Studio did not start. Check the launcher logs in: $logDirectory"
    exit 1
}

Open-StudioWindow
