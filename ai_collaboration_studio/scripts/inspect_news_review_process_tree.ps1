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

function Get-InspectionCimRows {
    # Deliberately exclude command lines and process environments.
    return @(Get-CimInstance -Query 'SELECT ProcessId,ParentProcessId,CreationDate FROM Win32_Process' | ForEach-Object {
        [pscustomobject]@{pid=[int]$_.ProcessId;parent_pid=[int]$_.ParentProcessId;
            ticks=$(if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().Ticks } else { $null })}
    })
}

function Select-RegisteredCimRows([object[]]$rows, [object[]]$pins) {
    $related = @{}
    foreach ($pin in $pins) { $related[[string]$pin.pid] = $true }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($row in $rows) {
            # Include numeric descendants even before their generations have
            # been verified. Resolve-RegisteredTree still rejects reused
            # parents, unidentified children, and invalid ancestry below.
            if ($related.ContainsKey([string]$row.parent_pid) -and -not $related.ContainsKey([string]$row.pid)) {
                if ($related.Count -ge 128) { throw 'process_tree_size_limit' }
                $related[[string]$row.pid] = $true
                $changed = $true
            }
        }
    }
    return @($rows | Where-Object { $related.ContainsKey([string]$_.pid) })
}

function Test-RegisteredEnumeration([object[]]$first, [object[]]$last, [object[]]$native, [object[]]$pins) {
    $before = @(Select-RegisteredCimRows $first $pins)
    $after = @(Select-RegisteredCimRows $last $pins)
    $beforeById = @{}; $afterById = @{}; $relevant = @{}
    foreach ($pin in $pins) { $relevant[[string]$pin.pid] = $true }
    foreach ($row in $before) {
        $key = [string]$row.pid
        if ($beforeById.ContainsKey($key)) { return $false }
        $beforeById[$key] = $row; $relevant[$key] = $true
    }
    foreach ($row in $after) {
        $key = [string]$row.pid
        if ($afterById.ContainsKey($key)) { return $false }
        $afterById[$key] = $row; $relevant[$key] = $true
    }
    if ($beforeById.Count -ne $afterById.Count) { return $false }
    foreach ($key in $beforeById.Keys) {
        if (-not $afterById.ContainsKey($key) -or
            $beforeById[$key].parent_pid -ne $afterById[$key].parent_pid -or
            $beforeById[$key].ticks -ne $afterById[$key].ticks) { return $false }
    }
    $nativeById = @{}
    foreach ($row in $native) {
        $key = [string]$row.pid
        if (-not $relevant.ContainsKey($key)) { continue }
        if ($nativeById.ContainsKey($key)) { return $false }
        $nativeById[$key] = $row
    }
    # A pinned PID present natively but absent from CIM must stay unconfirmed;
    # excluding unrelated activity must not turn this into a dead target.
    if ($nativeById.Count -ne $afterById.Count) { return $false }
    foreach ($key in $afterById.Keys) {
        if (-not $nativeById.ContainsKey($key) -or $null -eq $afterById[$key].ticks -or
            $null -eq $nativeById[$key].ticks) { return $false }
        if (($afterById[$key].ticks - ($afterById[$key].ticks % 10)) -ne
            ($nativeById[$key].ticks - ($nativeById[$key].ticks % 10))) { return $false }
    }
    return $true
}

$inputFile = [IO.Path]::GetFullPath($InputPath)
if ((Get-Item -LiteralPath $inputFile).Length -gt 65536) { throw 'inspection_input_too_large' }
$inputValue = Get-Content -LiteralPath $inputFile -Raw -Encoding utf8 | ConvertFrom-Json
if ($inputValue.version -cne 'news_review_native_inspection_input_v1') { throw 'inspection_input_version' }
$waiting = $null -eq $inputValue.host_pin
if ($waiting) {
    if ($null -ne $inputValue.host_url -or $null -ne $inputValue.identity.policy_sha256) { throw 'waiting_identity_invalid' }
} else {
    $uri = [Uri]$inputValue.host_url
    if ($uri.Scheme -cne 'http' -or $uri.Host -cne '127.0.0.1' -or $uri.Port -lt 1024 -or
        $uri.Port -in @(8770,11111) -or $uri.AbsolutePath -cne '/' -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
        throw 'inspection_host_url_invalid'
    }
}
$pins = @($inputValue.pins)
if ($pins.Count -lt 1 -or $pins.Count -gt 128) { throw 'inspection_pins_invalid' }
foreach ($pin in $pins) {
    if ($pin.pid -le 0 -or (Get-NativeTicks $pin.start_utc) -le 0) { throw 'inspection_pin_invalid' }
}

# Bracket native generations with two CIM ancestry snapshots. Only changes to
# registered roots or any of their descendants block a bound status read.
$firstCimRows = @(Get-InspectionCimRows)
$nativeRows = @(Get-Process | ForEach-Object {
    $start = $null
    try { $start = $_.StartTime.ToUniversalTime() } catch { }
    [pscustomobject]@{pid=[int]$_.Id;ticks=$(if ($start) {$start.Ticks} else {$null});
        start_utc=$(if ($start) {$start.ToString('o')} else {$null})}
})
$cimRows = @(Get-InspectionCimRows)
$enumerationConsistent = Test-RegisteredEnumeration $firstCimRows $cimRows $nativeRows $pins
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
$owners = @(); $ownerAlive = $false; $listeners = @(); $portBound = $false; $hostPid = $null
if (-not $waiting) {
    $hostPid = $inputValue.host_pin.pid
    $owners = @($tree.processes | Where-Object { $_.pid -eq $hostPid -and
        (Get-NativeTicks $_.start_utc) -eq (Get-NativeTicks $inputValue.host_pin.start_utc) })
    $ownerAlive = $owners.Count -eq 1 -and $owners[0].alive
    $listeners = @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq $uri.Port |
        Select-Object LocalAddress,LocalPort,OwningProcess)
    $portBound = $listeners.Count -eq 1 -and $listeners[0].LocalAddress -ceq '127.0.0.1' -and
        $listeners[0].OwningProcess -eq $hostPid
}
$result = [ordered]@{
    version=$(if ($waiting) {'news_review_native_wait_inspection_v1'} else {'news_review_native_inspection_v1'});checked_at_utc=[DateTime]::UtcNow.ToString('o');
    candidate_sha=$inputValue.identity.candidate_sha;activation_sha256=$inputValue.identity.activation_sha256;
    policy_sha256=$inputValue.identity.policy_sha256;launcher_pid=$inputValue.launcher_pin.pid;
    pins=$tree.pins;processes=$tree.processes;enumeration_consistent=$enumerationConsistent;
    known_identity_inspection_incomplete=[bool]$incomplete;owner_pid=$hostPid;
    owner_identity_pinned=($owners.Count -eq 1);owner_alive=[bool]$ownerAlive;
    listeners=$listeners;host_url=$inputValue.host_url;
    safe_status_reads_allowed=[bool]($ownerAlive -and $portBound -and -not $incomplete);
    ownership_lock_verified=$false;terminal_status_inferred=$false;database_opened=$false;http_requests=0
}
$result | ConvertTo-Json -Depth 10 -Compress
