[CmdletBinding()]
param(
  [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
  [string[]]$TestFiles,
  [ValidateRange(15, 900)]
  [int]$TimeoutSeconds = 120,
  [ValidateRange(256, 4096)]
  [int]$MaxPrivateMemoryMB = 3072,
  [ValidateRange(1, 1024)]
  [int]$MaxOutputMB = 64
)

$ErrorActionPreference = 'Stop'
$frontendRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testsRoot = [IO.Path]::GetFullPath((Join-Path $frontendRoot 'tests'))
$node = (Get-Command node.exe -ErrorAction Stop).Source
$taskkill = (Get-Command taskkill.exe -ErrorAction Stop).Source

function Stop-TestProcessTree {
  param([Diagnostics.Process]$Process)

  if ($null -eq $Process) { return }
  try {
    if (-not $Process.HasExited) {
      & $taskkill /PID $Process.Id /T /F 2>$null | Out-Null
      [void]$Process.WaitForExit(5000)
    }
  } catch {
    Write-Warning "Could not fully stop test process tree $($Process.Id): $($_.Exception.Message)"
  }
}

function New-TestProcess {
  param([string]$TestPath)

  if ($TestPath.Contains('"')) {
    throw "Test path contains an unsupported quote: $TestPath"
  }
  $startInfo = [Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $node
  $startInfo.WorkingDirectory = $frontendRoot
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $startInfo.Arguments = '--max-old-space-size=2048 --test --test-concurrency=1 --test-isolation=none "{0}"' -f $TestPath

  $process = [Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  if (-not $process.Start()) {
    $process.Dispose()
    throw "Could not start Node for $TestPath"
  }
  return $process
}

if (-not $TestFiles -or $TestFiles.Count -eq 0) {
  $TestFiles = Get-ChildItem -LiteralPath $testsRoot -File -Filter '*.test.js' |
    Sort-Object Name |
    ForEach-Object FullName
}

$resolvedTests = foreach ($file in ($TestFiles | Where-Object { $_ -and $_ -ne '--' })) {
  $candidate = if ([IO.Path]::IsPathRooted($file)) {
    [IO.Path]::GetFullPath($file)
  } else {
    [IO.Path]::GetFullPath((Join-Path $frontendRoot $file))
  }
  if (-not $candidate.StartsWith($testsRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing test path outside frontend/tests: $file"
  }
  if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw "Test file not found: $candidate"
  }
  $candidate
}

if (-not $resolvedTests -or @($resolvedTests).Count -eq 0) {
  throw 'No frontend test files were selected.'
}

$failures = [Collections.Generic.List[string]]::new()
foreach ($testPath in @($resolvedTests)) {
  $displayName = [IO.Path]::GetFileName($testPath)
  Write-Host "[safe-test] $displayName"
  $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-studio-test-{0}.out" -f [guid]::NewGuid().ToString('N'))
  $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-studio-test-{0}.err" -f [guid]::NewGuid().ToString('N'))
  $process = $null
  $stdoutStream = $null
  $stderrStream = $null
  $stdoutCopy = $null
  $stderrCopy = $null
  $completedNormally = $false
  try {
    # One file per process plus isolation=none keeps the test body in the
    # process we monitor. This avoids an unobserved Node test-worker child
    # owning tens of GiB while the small runner parent appears healthy.
    $process = New-TestProcess -TestPath $testPath
    $stdoutStream = [IO.FileStream]::new($stdoutPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $stderrStream = [IO.FileStream]::new($stderrPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $stdoutCopy = $process.StandardOutput.BaseStream.CopyToAsync($stdoutStream)
    $stderrCopy = $process.StandardError.BaseStream.CopyToAsync($stderrStream)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $guardReason = $null

    while (-not $process.WaitForExit(250)) {
      $process.Refresh()
      $privateMemoryMB = [math]::Round($process.PrivateMemorySize64 / 1MB)
      if ($privateMemoryMB -gt $MaxPrivateMemoryMB) {
        $guardReason = "private memory reached ${privateMemoryMB} MB (limit ${MaxPrivateMemoryMB} MB)"
        break
      }
      $outputBytes = 0L
      if (Test-Path -LiteralPath $stdoutPath) { $outputBytes += (Get-Item -LiteralPath $stdoutPath).Length }
      if (Test-Path -LiteralPath $stderrPath) { $outputBytes += (Get-Item -LiteralPath $stderrPath).Length }
      if ($outputBytes -gt ($MaxOutputMB * 1MB)) {
        $outputMB = [math]::Round($outputBytes / 1MB)
        $guardReason = "captured output reached ${outputMB} MB (limit ${MaxOutputMB} MB)"
        break
      }
      if ((Get-Date) -ge $deadline) {
        $guardReason = "timeout after ${TimeoutSeconds}s"
        break
      }
    }

    if ($guardReason) {
      Stop-TestProcessTree -Process $process
      $failures.Add("$displayName ($guardReason)")
    } else {
      $process.WaitForExit()
      $process.Refresh()
      $exitCode = $process.ExitCode
      $completedNormally = $true
      if ($exitCode -ne 0) {
        $failures.Add("$displayName (exit $exitCode)")
      }
    }
  } catch {
    if ($null -ne $process) { Stop-TestProcessTree -Process $process }
    $failures.Add("$displayName (runner error: $($_.Exception.Message))")
  } finally {
    # Any launch, monitoring, or cleanup error must fail closed and leave no
    # test process behind.
    if (-not $completedNormally -and $null -ne $process) {
      Stop-TestProcessTree -Process $process
    }
    foreach ($copyTask in @($stdoutCopy, $stderrCopy)) {
      if ($null -ne $copyTask) {
        try { [void]$copyTask.GetAwaiter().GetResult() } catch { Write-Warning $_.Exception.Message }
      }
    }
    if ($null -ne $stdoutStream) { $stdoutStream.Dispose() }
    if ($null -ne $stderrStream) { $stderrStream.Dispose() }
    if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Encoding UTF8 }
    if (Test-Path -LiteralPath $stderrPath) {
      Get-Content -LiteralPath $stderrPath -Encoding UTF8 | ForEach-Object { [Console]::Error.WriteLine($_) }
    }
    if ($null -ne $process) { $process.Dispose() }
    Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
  }

  if ($failures.Count -gt 0) { break }
}

if ($failures.Count -gt 0) {
  throw "Safe frontend test failed: $($failures -join '; ')"
}
Write-Host "[safe-test] passed $(@($resolvedTests).Count) file(s)"
