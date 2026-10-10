param(
    [Parameter(Mandatory=$true)][string]$RunRoot,
    [Parameter(Mandatory=$true)][string]$AuthorizedWorkspace,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][ValidateSet('check','launch')][string]$Mode
)
$ErrorActionPreference = 'Stop'
$resolvedRoot = [System.IO.Path]::GetFullPath($RunRoot)
$authorizedRoot = [System.IO.Path]::GetFullPath($AuthorizedWorkspace).TrimEnd('\','/')
if (-not $resolvedRoot.StartsWith($authorizedRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'launcher root outside authorized workspace'
}
# Verify every existing ancestor, not just the final directory.
$ancestor = [System.IO.DirectoryInfo]::new($resolvedRoot)
while ($null -ne $ancestor) {
    if (-not $ancestor.Exists -or ($ancestor.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'launcher ancestor missing or a reparse point'
    }
    $ancestor = $ancestor.Parent
}
$capturePath = Join-Path $resolvedRoot 'capture_news_review_isolated.py'
$helperPath = Join-Path $resolvedRoot 'launch_news_review_isolated.py'
$bindingsPath = Join-Path $resolvedRoot 'isolated-launch-bindings.json'
foreach ($sealedPath in @($capturePath,$helperPath,$bindingsPath)) {
    if (-not (Test-Path -LiteralPath $sealedPath -PathType Leaf) -or ((Get-Item -LiteralPath $sealedPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'sealed launcher file missing or a reparse point'
    }
}
if ($Mode -eq 'launch' -and -not ([System.IO.Path]::GetFileName($resolvedRoot)).StartsWith('NewsReviewTrial', [System.StringComparison]::Ordinal)) {
    throw 'real launch requires a separately prepared NewsReviewTrial root'
}
$resolvedPython = [System.IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { throw 'python executable missing' }
if ($resolvedPython.Contains('"') -or $capturePath.Contains('"') -or $resolvedRoot.Contains('"') -or $resolvedRoot.EndsWith('\')) { throw 'invalid command path' }
# Arguments are absolute file paths plus an enum; no command interpreter executes them.
$commandLine = '"' + $resolvedPython + '" -I -S -X utf8 "' + $capturePath + '" --run-root "' + $resolvedRoot + '" --mode ' + $Mode
$environment = [System.Collections.Generic.List[string]]::new()
foreach ($name in @('SystemRoot','WINDIR','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','PATH','COMSPEC')) {
    $value = [System.Environment]::GetEnvironmentVariable($name, 'Process')
    if ($null -ne $value -and $value.Length -gt 0) { $environment.Add($name + '=' + $value) }
}
$environment.Add('PYTHONUTF8=1')
$environment.Add('PYTHONIOENCODING=utf-8')
$environment.Add('AI_STUDIO_SKIP_LOCAL_ENV=1')
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
    ShowWindow = [uint16]0
    CreateFlags = [uint32]16777216
    WinstationDesktop = 'WinSta0\Default'
    EnvironmentVariables = [string[]]$environment
}
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $commandLine
    CurrentDirectory = $resolvedRoot
    ProcessStartupInformation = $startup
}
[ordered]@{
    return_value = [uint32]$created.ReturnValue
    process_id = [uint32]$created.ProcessId
    mode = $Mode
    outside_all_jobs_inferred_from_creation = $false
    requires_native_boundary_receipt = $true
} | ConvertTo-Json -Compress
if ([uint32]$created.ReturnValue -ne 0) { exit 2 }
