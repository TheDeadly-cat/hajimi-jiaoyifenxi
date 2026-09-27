#requires -Version 7.0
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$InputPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-NativeTicks($value) {
    if ($value -is [DateTime]) { return $value.ToUniversalTime().Ticks }
    return ([DateTimeOffset]::Parse([string]$value)).UtcDateTime.Ticks
}

function Resolve-RegisteredTree([object[]]$snapshot, [object[]]$pins) {
    $known = @{}
    foreach ($entry in $pins) {
        $key = [string]$entry.pid + ':' + (Get-NativeTicks $entry.start_utc)
        if ($known.ContainsKey($key)) { throw 'duplicate_process_pin' }
        $known[$key] = $entry
    }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($entry in @($known.Values)) {
            $parent = @($snapshot | Where-Object { $_.pid -eq $entry.pid -and $_.ticks -eq (Get-NativeTicks $entry.start_utc) })
            if ($parent.Count -ne 1) { continue }
            foreach ($child in @($snapshot | Where-Object { $_.parent_pid -eq $entry.pid -and $_.ticks -ge $parent[0].ticks })) {
                $key = [string]$child.pid + ':' + $child.ticks
                if (-not $known.ContainsKey($key)) {
                    if ($known.Count -ge 128) { throw 'process_tree_size_limit' }
                    $known[$key] = [pscustomobject]@{pid=$child.pid;parent_pid=$child.parent_pid;start_utc=$child.start_utc}
                    $changed = $true
                }
            }
        }
    }
    # A newly observed orphan cannot be assigned to a dead/reused generation
    # merely from its numeric parent PID. Keep the check incomplete instead.
    $unboundDescendant = $false
    foreach ($entry in $snapshot) {
        if ($entry.parent_pid -in @($known.Values.pid)) {
            $key = [string]$entry.pid + ':' + $entry.ticks
            if (-not $known.ContainsKey($key)) { $unboundDescendant = $true }
        }
    }
    $states = foreach ($entry in @($known.Values)) {
        $same = @($snapshot | Where-Object pid -eq $entry.pid)
        $matching = @($same | Where-Object ticks -eq (Get-NativeTicks $entry.start_utc))
        [pscustomobject]@{pid=$entry.pid;parent_pid=$entry.parent_pid;start_utc=$entry.start_utc;
            alive=($matching.Count -eq 1);pid_reused=($same.Count -gt 0 -and $matching.Count -eq 0)}
    }
    return [pscustomobject]@{pins=@($known.Values | Sort-Object pid,start_utc);
        processes=@($states | Sort-Object pid,start_utc);unbound_descendant=$unboundDescendant}
}

$inputFile = [IO.Path]::GetFullPath($InputPath)
if ((Get-Item -LiteralPath $inputFile).Length -gt 65536) { throw 'inspection_input_too_large' }
$inputValue = Get-Content -LiteralPath $inputFile -Raw -Encoding utf8 | ConvertFrom-Json
if ($inputValue.version -cne 'news_review_native_inspection_input_v1') { throw 'inspection_input_version' }
$uri = [Uri]$inputValue.host_url
if ($uri.Scheme -cne 'http' -or $uri.Host -cne '127.0.0.1' -or $uri.Port -lt 1024 -or
    $uri.Port -in @(8770,11111) -or $uri.AbsolutePath -cne '/' -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
    throw 'inspection_host_url_invalid'
}
$pins = @($inputValue.pins)
if ($pins.Count -lt 1 -or $pins.Count -gt 128) { throw 'inspection_pins_invalid' }
foreach ($pin in $pins) {
    if ($pin.pid -le 0 -or (Get-NativeTicks $pin.start_utc) -le 0) { throw 'inspection_pin_invalid' }
}

# Explicit property selection avoids command lines and process environments.
$cimRows = @(Get-CimInstance -Query 'SELECT ProcessId,ParentProcessId,CreationDate FROM Win32_Process' | ForEach-Object {
    [pscustomobject]@{pid=[int]$_.ProcessId;parent_pid=[int]$_.ParentProcessId;
        ticks=$(if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().Ticks } else { $null })}
})
$nativeRows = @(Get-Process | ForEach-Object {
    $start = $null
    try { $start = $_.StartTime.ToUniversalTime() } catch { }
    [pscustomobject]@{pid=[int]$_.Id;ticks=$(if ($start) {$start.Ticks} else {$null});
        start_utc=$(if ($start) {$start.ToString('o')} else {$null})}
})
$enumerationConsistent = @(Compare-Object (@($cimRows.pid | Sort-Object)) (@($nativeRows.pid | Sort-Object))).Count -eq 0
$snapshot = @()
$unidentified = @()
foreach ($row in $cimRows) {
    $native = @($nativeRows | Where-Object pid -eq $row.pid)
    if ($null -eq $row.ticks -or $native.Count -ne 1 -or $null -eq $native[0].ticks) {
        $unidentified += $row.pid
        continue
    }
    # CIM truncates to microseconds; preserve the native 100 ns generation.
    if (($row.ticks - ($row.ticks % 10)) -ne ($native[0].ticks - ($native[0].ticks % 10))) {
        $unidentified += $row.pid
        continue
    }
    $snapshot += [pscustomobject]@{pid=$row.pid;parent_pid=$row.parent_pid;ticks=$native[0].ticks;start_utc=$native[0].start_utc}
}
$tree = Resolve-RegisteredTree $snapshot $pins
$incomplete = -not $enumerationConsistent -or $tree.unbound_descendant
foreach ($pin in $pins) {
    $match = @($snapshot | Where-Object { $_.pid -eq $pin.pid -and $_.ticks -eq (Get-NativeTicks $pin.start_utc) })
    if ($match.Count -eq 1 -and $null -ne $pin.parent_pid -and $match[0].parent_pid -ne $pin.parent_pid) {
        $incomplete = $true
    }
}
if (@($tree.pins | Where-Object pid -in $unidentified).Count -gt 0 -or
    @($cimRows | Where-Object { $_.pid -in $unidentified -and $_.parent_pid -in @($tree.pins.pid) }).Count -gt 0 -or
    @($tree.processes | Where-Object pid_reused).Count -gt 0) { $incomplete = $true }
$owners = @($tree.processes | Where-Object { $_.pid -eq $inputValue.host_pin.pid -and
    (Get-NativeTicks $_.start_utc) -eq (Get-NativeTicks $inputValue.host_pin.start_utc) })
$ownerAlive = $owners.Count -eq 1 -and $owners[0].alive
$listeners = @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq $uri.Port |
    Select-Object LocalAddress,LocalPort,OwningProcess)
$portBound = $listeners.Count -eq 1 -and $listeners[0].LocalAddress -ceq '127.0.0.1' -and
    $listeners[0].OwningProcess -eq $inputValue.host_pin.pid
$result = [ordered]@{
    version='news_review_native_inspection_v1';checked_at_utc=[DateTime]::UtcNow.ToString('o');
    candidate_sha=$inputValue.identity.candidate_sha;activation_sha256=$inputValue.identity.activation_sha256;
    policy_sha256=$inputValue.identity.policy_sha256;launcher_pid=$inputValue.launcher_pin.pid;
    pins=$tree.pins;processes=$tree.processes;enumeration_consistent=$enumerationConsistent;
    known_identity_inspection_incomplete=[bool]$incomplete;owner_pid=$inputValue.host_pin.pid;
    owner_identity_pinned=($owners.Count -eq 1);owner_alive=[bool]$ownerAlive;
    listeners=$listeners;host_url=$inputValue.host_url;
    safe_status_reads_allowed=[bool]($ownerAlive -and $portBound -and -not $incomplete);
    ownership_lock_verified=$false;terminal_status_inferred=$false;database_opened=$false;http_requests=0
}
$result | ConvertTo-Json -Depth 10 -Compress
