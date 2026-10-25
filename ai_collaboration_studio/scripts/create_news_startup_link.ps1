param([Parameter(Mandatory=$true)][string]$OutputPath,[Parameter(Mandatory=$true)][string]$LauncherPath)
$ErrorActionPreference='Stop'
$newsOutput=[IO.Path]::GetFullPath($OutputPath)
$newsLauncher=[IO.Path]::GetFullPath($LauncherPath)
if((Test-Path -LiteralPath $newsOutput) -or -not (Test-Path -LiteralPath $newsLauncher -PathType Leaf) -or $newsLauncher.Contains('"') -or $newsLauncher.Contains("`n")){throw 'invalid startup path'}
$newsShell=New-Object -ComObject WScript.Shell
$newsLink=$newsShell.CreateShortcut($newsOutput)
$newsLink.TargetPath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$newsLink.Arguments='-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$newsLauncher+'" -Background'
$newsLink.WorkingDirectory=Split-Path -Parent $newsLauncher
$newsLink.WindowStyle=7
$newsLink.Description='AI Studio daily news background startup'
$newsLink.Save()
if(-not (Test-Path -LiteralPath $newsOutput -PathType Leaf)){throw 'startup entry missing'}
