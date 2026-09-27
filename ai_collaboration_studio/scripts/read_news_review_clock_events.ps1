#requires -Version 7.0
param(
    [Parameter(Mandatory)][DateTimeOffset]$FromUtc,
    [Parameter(Mandatory)][DateTimeOffset]$ToUtc,
    [Parameter(Mandatory)][string]$OutputPath
)
$ErrorActionPreference='Stop'
if($ToUtc -le $FromUtc -or ($ToUtc-$FromUtc).TotalHours -gt 25){throw 'Choose a positive window of at most 25 hours'}
if(-not [IO.Path]::IsPathFullyQualified($OutputPath) -or (Test-Path -LiteralPath $OutputPath)){throw 'An unused absolute output file is required'}
$queries=@(
    @{provider='Microsoft-Windows-Kernel-General';ids=@(1);purpose='system_clock_adjustment'},
    @{provider='Microsoft-Windows-Time-Service';ids=@(35,37);purpose='time_service_metadata'},
    @{provider='Microsoft-Windows-Kernel-Power';ids=@(42,107);purpose='sleep_resume_metadata'},
    @{provider='Microsoft-Windows-Power-Troubleshooter';ids=@(1);purpose='wake_metadata'},
    @{provider='Microsoft-Windows-Time-Service';log='Microsoft-Windows-Time-Service/Operational';ids=@(260,261,266);purpose='time_service_operational_metadata'}
)
$results=foreach($query in $queries){
    $records=@();$status='queried';$capped=$false
    try{
        $logName=if($query.log){$query.log}else{'System'}
        $events=@(Get-WinEvent -FilterHashtable @{LogName=$logName;ProviderName=$query.provider;Id=$query.ids;StartTime=$FromUtc.LocalDateTime;EndTime=$ToUtc.LocalDateTime} -MaxEvents 129 -ErrorAction Stop)
        $capped=$events.Count -gt 128
        $records=@($events|Where-Object {$_.TimeCreated.ToUniversalTime() -ge $FromUtc.UtcDateTime -and $_.TimeCreated.ToUniversalTime() -le $ToUtc.UtcDateTime}|Select-Object -First 128|ForEach-Object{
            $event=$_
            $timeFields=@{}
            $numericFields=@{}
            if($query.purpose -in @('system_clock_adjustment','time_service_operational_metadata')){
                $xml=[xml]$event.ToXml()
                foreach($node in $xml.Event.EventData.Data){
                    if($node.Name -in @('NewTime','OldTime')){
                        $parsed=[DateTimeOffset]::MinValue
                        if([DateTimeOffset]::TryParse([string]$node.'#text',[ref]$parsed)){$timeFields[$node.Name]=$parsed.UtcDateTime.ToString('o')}
                    }elseif($node.Name -in @('PhaseOffset','ClockRate','LastSyncError','TimeSinceLastGoodSync','TickCount','ReasonCode')){
                        $number=0.0
                        if([double]::TryParse([string]$node.'#text',[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$number) -and [double]::IsFinite($number)){
                            $numericFields[$node.Name]=$number
                        }
                    }
                }
            }
            [ordered]@{event_id=$event.Id;record_id=$event.RecordId;time_created_utc=$event.TimeCreated.ToUniversalTime().ToString('o');clock_fields=$timeFields;numeric_fields=$numericFields}
        })
    }catch{
        $status=if($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*'){'no_matching_events'}else{'query_unavailable'}
    }
    [ordered]@{provider=$query.provider;log_name=if($query.log){$query.log}else{'System'};purpose=$query.purpose;status=$status;truncated=$capped;records=$records}
}
$report=[ordered]@{version='news_review_system_clock_metadata_v2';checked_at_utc=[DateTime]::UtcNow.ToString('o');from_utc=$FromUtc.UtcDateTime.ToString('o');to_utc=$ToUtc.UtcDateTime.ToString('o');queries=@($results);numeric_field_units='native event values; not interpreted';raw_event_messages_saved=$false;configuration_or_time_source_names_saved=$false;process_environment_read=$false;system_configuration_changed=$false;root_cause_proven=$false}
$file=[IO.File]::Open($OutputPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
try{$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($report|ConvertTo-Json -Depth 10));$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
[pscustomobject]@{output=$OutputPath;queries=@($results|ForEach-Object{[pscustomobject]@{purpose=$_.purpose;status=$_.status;records=@($_.records).Count;truncated=$_.truncated}});root_cause_proven=$false}|ConvertTo-Json -Depth 5
