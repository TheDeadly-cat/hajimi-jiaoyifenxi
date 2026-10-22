param(
    [Parameter(Mandatory=$true)][ValidatePattern('^NewsObserver-[a-zA-Z0-9-]+$')][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$ScriptPath,
    [Parameter(Mandatory=$true)][string]$ScriptSha256,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [Parameter(Mandatory=$true)][string]$ConfigSha256,
    [Parameter(Mandatory=$true)][string]$ReceiptDirectory,
    [Parameter(Mandatory=$true)][string]$PythonPath
)
$ErrorActionPreference='Stop'
$python=[IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PythonPath) 'pythonw.exe'))
if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw 'Windowless Python interpreter missing'}
$root=[System.IO.Path]::GetFullPath($PSScriptRoot)
foreach($entry in @($ScriptPath,$ConfigPath,$ReceiptDirectory)) {
    $resolved=[System.IO.Path]::GetFullPath($entry)
    if(-not $resolved.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Task artifact outside repair deployment'}
    if($resolved.Contains('"')){throw 'Invalid task argument'}
}
function Get-Sha256([string]$path){
    # Use the runtime directly: Get-FileHash may be unavailable to a child
    # Windows PowerShell process when optional modules cannot be discovered.
    $stream=[IO.File]::OpenRead($path)
    $hash=[Security.Cryptography.SHA256]::Create()
    try{return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
    finally{$hash.Dispose();$stream.Dispose()}
}
if((Get-Sha256 $ScriptPath) -ne $ScriptSha256){throw 'Supervisor identity mismatch'}
if((Get-Sha256 $ConfigPath) -ne $ConfigSha256){throw 'Config identity mismatch'}
if(Test-Path -LiteralPath $ReceiptDirectory){throw 'Task launch evidence already exists; inspect before any retry'}
[void][System.IO.Directory]::CreateDirectory($ReceiptDirectory)
function Write-NewJson([string]$name,$value){
    $path=Join-Path $ReceiptDirectory $name
    $stream=[IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$bytes=[Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 12));$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
}
$account=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$scheduler=New-Object -ComObject Schedule.Service
$scheduler.Connect()
$folder=$scheduler.GetFolder('\')
$definition=$scheduler.NewTask(0)
$definition.RegistrationInfo.Description='Windowless read-only guardian attachment; no observer launch, no restart, no automatic triggers.'
$definition.Principal.UserId=$account
$definition.Principal.LogonType=3
$definition.Principal.RunLevel=0
$definition.Settings.Enabled=$true
# Hidden only hides the scheduler entry. pythonw.exe removes console ownership.
$definition.Settings.Hidden=$true
$definition.Settings.AllowDemandStart=$true
$definition.Settings.MultipleInstances=2
$definition.Settings.ExecutionTimeLimit='PT0S'
$definition.Settings.RestartCount=0
$definition.Settings.DisallowStartIfOnBatteries=$false
$definition.Settings.StopIfGoingOnBatteries=$false
$definition.Settings.StartWhenAvailable=$false
$action=$definition.Actions.Create(0)
$action.Path=$python
$action.Arguments='-I -S -B -X utf8 "'+$ScriptPath+'" --config "'+$ConfigPath+'"'
$action.WorkingDirectory=$root
Write-NewJson 'intent.json' @{task_name=$TaskName;interpreter=$python;windowless_interpreter=$true;script=$ScriptPath;script_sha256=$ScriptSha256;config=$ConfigPath;config_sha256=$ConfigSha256;execution_time_limit='PT0S';restart_count=0;trigger_count=0;host_mutated=$false;utc=[DateTime]::UtcNow.ToString('o')}
# TASK_CREATE only: an existing task is never replaced or restarted.
$registered=$folder.RegisterTaskDefinition($TaskName,$definition,2,$account,$null,3)
Write-NewJson 'registration.json' @{task_name=$registered.Name;path=$registered.Path;enabled=$registered.Enabled;utc=[DateTime]::UtcNow.ToString('o')}
$xmlPath=Join-Path $ReceiptDirectory 'registered-task.xml'
$xmlStream=[IO.File]::Open($xmlPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
try{$xmlBytes=[Text.Encoding]::UTF8.GetBytes($registered.Xml);$xmlStream.Write($xmlBytes,0,$xmlBytes.Length);$xmlStream.Flush($true)}finally{$xmlStream.Dispose()}
$running=$registered.Run($null)
Write-NewJson 'dispatch.json' @{task_name=$TaskName;instance_guid=$running.InstanceGuid;state=$running.State;utc=[DateTime]::UtcNow.ToString('o');outcome='dispatched_not_yet_verified'}
@{task_name=$TaskName;instance_guid=$running.InstanceGuid;state=$running.State} | ConvertTo-Json
