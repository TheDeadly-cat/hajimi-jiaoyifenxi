[CmdletBinding()]
param([switch]$CheckOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$appUrl = "http://127.0.0.1:8770/"
$readinessUrl = "http://127.0.0.1:8770/api/readiness"
$versionUrl = "http://127.0.0.1:8770/api/version"
$integrationManifestUrl = "http://127.0.0.1:8770/api/integration/manifest"
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

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($stream)
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    return -join ($digest | ForEach-Object { $_.ToString("x2") })
}

function Get-BackendSourceSha256 {
    $relativePaths = @("server.py")
    $relativePaths += Get-ChildItem `
        -LiteralPath (Join-Path $projectRoot "backend") `
        -Recurse `
        -File `
        -Filter "*.py" | ForEach-Object {
            $_.FullName.Substring($projectRoot.Length + 1).Replace("\", "/")
        }
    [string[]]$orderedPaths = $relativePaths
    [Array]::Sort($orderedPaths, [System.StringComparer]::Ordinal)

    $fingerprint = [System.Text.StringBuilder]::new()
    foreach ($relativePath in $orderedPaths) {
        $sourcePath = Join-Path $projectRoot $relativePath.Replace("/", "\")
        $fileSha256 = Get-FileSha256 -Path $sourcePath
        [void]$fingerprint.Append($relativePath)
        [void]$fingerprint.Append([char]0)
        [void]$fingerprint.Append($fileSha256)
        [void]$fingerprint.Append("`n")
    }

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($fingerprint.ToString())
        $digest = $sha256.ComputeHash($bytes)
    } finally {
        $sha256.Dispose()
    }
    return -join ($digest | ForEach-Object { $_.ToString("x2") })
}

function Test-StudioReady {
    try {
        $readiness = Invoke-RestMethod -Uri $readinessUrl -Method Get -TimeoutSec 2
        $version = Invoke-RestMethod -Uri $versionUrl -Method Get -TimeoutSec 2
        $manifest = Invoke-RestMethod -Uri $integrationManifestUrl -Method Get -TimeoutSec 2
        return `
            $readiness.ok -eq $true -and `
            $readiness.ready -eq $true -and `
            $readiness.schema_version -eq "host_readiness_v1" -and `
            $readiness.service.id -eq "ai_collaboration_studio" -and `
            $version.ok -eq $true -and `
            $version.schema_version -eq "host_version_v2" -and `
            $version.service.id -eq "ai_collaboration_studio" -and `
            $version.backend_build.available -eq $true -and `
            $version.backend_build.source_sha256 -eq $expectedBackendSourceSha256 -and `
            $manifest.ok -eq $true -and `
            $manifest.schema_version -eq "studio_integration_manifest_v2"
    } catch {
        return $false
    }
}

function Test-StudioHostIdentity {
    try {
        $version = Invoke-RestMethod -Uri $versionUrl -Method Get -TimeoutSec 2
        return `
            $version.ok -eq $true -and `
            $version.schema_version -in @("host_version_v1", "host_version_v2") -and `
            $version.service.id -eq "ai_collaboration_studio"
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

function Get-StartupFailureMessage {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][string]$LogDirectory
    )

    $diagnosticText = ""
    foreach ($path in @($StdoutPath, $StderrPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            continue
        }
        try {
            $diagnosticText += "`n" + ((Get-Content -LiteralPath $path -Tail 40 -ErrorAction Stop) -join "`n")
        } catch {
            # The server may still hold a redirected log handle. Classification
            # is best effort and never changes the fail-closed startup result.
        }
    }

    $logHint = "Logs: $LogDirectory"
    if ($diagnosticText -match "DatabaseMigrationRequired|Database migration is required") {
        return @"
The current source requires a newly prepared and reviewed database migration.
The previous authorization belongs to an earlier prepared SHA and cannot be reused.
No automatic migration was attempted. Generate a new preview/prepare manifest, review its exact SHA, then authorize that exact candidate separately.
$logHint
"@
    }
    if ($diagnosticText -match "DatabaseMigrationRecoveryRequired|DatabaseMigrationError") {
        return @"
Database migration recovery or consistency checks blocked startup.
The launcher did not repair, replace, or migrate the database. Review the migration recovery evidence before retrying.
$logHint
"@
    }
    if (-not $Process.HasExited) {
        return @"
Startup did not become ready within 15 seconds, and the process created by this launcher is still running.
The launcher did not stop or replace it. Review the logs before retrying so a second hidden process is not created.
$logHint
"@
    }

    return "AI Collaboration Studio exited before readiness (exit code $($Process.ExitCode)). $logHint"
}

try {
    $expectedBackendSourceSha256 = Get-BackendSourceSha256
} catch {
    $sourceIdentityError = $_.Exception.Message
    Show-LauncherError @"
The launcher could not compute the current backend source identity. No service was started.

Diagnostic: $sourceIdentityError
"@
    exit 1
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
    if (Test-StudioHostIdentity) {
        Show-LauncherError "An AI Collaboration Studio host is already using port 8770, but it does not satisfy the current readiness and integration-contract checks. The launcher did not stop or replace it. Close or migrate that exact instance deliberately before retrying."
    } else {
        Show-LauncherError "Local port 8770 is occupied by another service. The launcher did not stop or replace that process."
    }
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
    $failureMessage = Get-StartupFailureMessage `
        -Process $serverProcess `
        -StdoutPath $stdoutPath `
        -StderrPath $stderrPath `
        -LogDirectory $logDirectory
    Show-LauncherError $failureMessage
    exit 1
}

Open-StudioWindow
