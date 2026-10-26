[CmdletBinding()]
param(
    [string]$DataRoot = '', [int]$Port = 8780, [switch]$Offline,
    [string]$PythonPath = '', [switch]$CheckOnly, [switch]$NoBrowser
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$script:Project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:Manifest = $null
try { $script:Manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'news_first_run_runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
$script:Attempt = [Guid]::NewGuid().ToString('N')
$script:Receipt = Join-Path $script:Project ("runtime\news-launcher\receipts\{0}-welcome.json" -f $script:Attempt)
$script:LastResult = $null
$script:Busy = $false
$script:PendingProcess = $null
$script:PendingOutput = $null
$script:PendingError = $null
$script:PendingIdentity = $null
$script:PreparationGate = $null
$script:Python = $null
$script:ManagedPython = Join-Path $script:Project 'runtime\bootstrap\python\Scripts\python.exe'
$script:PrivatePython = Join-Path $script:Project 'runtime\toolchains\python-3.14.6\python.exe'
$script:PrivateNode = Join-Path $script:Project 'runtime\toolchains\node-v24.16.0-win-x64'
if (-not $DataRoot) { $DataRoot = Join-Path $script:Project 'runtime\news-workspace' }

function Assert-OrdinaryPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $part = $full
    while ($part) {
        if (Test-Path -LiteralPath $part) {
            $item = Get-Item -LiteralPath $part -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'path_alias_refused' }
        }
        $parent = Split-Path -Parent $part
        if ($parent -eq $part) { break }; $part = $parent
    }
    return $full
}

function Enter-PreparationGate {
    if($script:PreparationGate){throw 'another_setup_in_progress'}
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$digest=-join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:Project.ToLowerInvariant())) | ForEach-Object{$_.ToString('x2')})}
    finally{$sha.Dispose()}
    $gate=New-Object Threading.Mutex($false,('Local\NewsWorkspacePreparation_'+$digest))
    $owned=$false
    try{$owned=$gate.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true}
    if(-not $owned){$gate.Dispose();throw 'another_setup_in_progress'}
    $script:PreparationGate=$gate
}

function Leave-PreparationGate {
    if($script:PreparationGate -and -not $script:PendingProcess){
        $script:PreparationGate.ReleaseMutex();$script:PreparationGate.Dispose();$script:PreparationGate=$null
    }
}

function Save-SafeReceipt([string]$Code, [string]$Detail) {
    $value = [ordered]@{ schema='news_welcome_v1'; at=[DateTimeOffset]::UtcNow.ToString('o'); code=$Code;
        detail=$Detail; paid_model_calls=0; stopped_existing_processes=0; implicit_data_recovery=$false;
        diagnostic=("消息工作台安全诊断`r`n状态：{0}`r`n未包含密钥、消息正文或本机完整路径。" -f $Code) }
    if($script:PendingIdentity){$value.pending_process=$script:PendingIdentity}
    try {
        $path = Assert-OrdinaryPath $script:Receipt
        if(Test-Path -LiteralPath $path){$path=Join-Path (Split-Path -Parent $path) ($script:Attempt+'-'+[Guid]::NewGuid().ToString('N')+'-welcome.json')}
        [IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
        if (-not (Test-Path -LiteralPath $path)) {
            $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
            try { $bytes=[Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 5)); $stream.Write($bytes,0,$bytes.Length) }
            finally { $stream.Dispose() }
        }
        $value.receipt = $path
    } catch { $value.receipt=$null; $value.diagnostic += "`r`n诊断文件未能保存，请复制以上内容。" }
    return [pscustomobject]$value
}

function Invoke-Captured([string]$Executable, [string[]]$Arguments, [int]$Timeout=30, [string]$Purpose='probe') {
    if($script:PendingProcess){throw 'setup_process_still_running_no_repeat'}
    # Only fixed probe/setup commands. Do not inherit provider credentials.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $quoted = foreach ($arg in $Arguments) { '"' + $arg.Replace('"','\"') + '"' }
    $info.Arguments = $quoted -join ' '
    $info.WorkingDirectory = $script:Project
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $info.StandardOutputEncoding=[Text.Encoding]::UTF8; $info.StandardErrorEncoding=[Text.Encoding]::UTF8
    foreach ($name in @('OPENAI_API_KEY','ARK_API_KEY','DOUBAO_API_KEY','DEEPSEEK_API_KEY','DASHSCOPE_API_KEY','QWEN_API_KEY','GLM_API_KEY','ZHIPU_API_KEY','ZHIPUAI_API_KEY','GOOGLE_API_KEY','GEMINI_API_KEY','ANTHROPIC_API_KEY','AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET','AI_STUDIO_MCP_TOKEN_SIGNING_SECRET')) { $info.EnvironmentVariables.Remove($name) }
    $info.EnvironmentVariables['PYTHONUTF8']='1'; $info.EnvironmentVariables['PYTHONIOENCODING']='utf-8'
    if (Test-Path -LiteralPath (Join-Path $script:PrivateNode 'node.exe')) {
        $info.EnvironmentVariables['PATH']=$script:PrivateNode + ';' + $info.EnvironmentVariables['PATH']
    }
    $process=New-Object Diagnostics.Process; $process.StartInfo=$info
    if (-not $process.Start()) { throw 'setup_process_failed' }
    $out=$process.StandardOutput.ReadToEndAsync(); $err=$process.StandardError.ReadToEndAsync()
    $deadline=[DateTime]::UtcNow.AddSeconds($Timeout)
    while (-not $process.WaitForExit(100)) {
        if (-not $CheckOnly) { [Windows.Forms.Application]::DoEvents() }
        if ([DateTime]::UtcNow -gt $deadline) {
            $script:PendingProcess=$process; $script:PendingOutput=$out; $script:PendingError=$err
            $script:PendingIdentity=[ordered]@{pid=$process.Id;creation_filetime=$process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString();purpose=$Purpose}
            $null=Save-SafeReceipt 'setup_process_still_running_no_repeat' '准备进程仍在运行，保留持有句柄，只允许继续核对，不重复启动。'
            throw 'setup_process_still_running_no_repeat'
        }
    }
    $stdout=$out.Result; $discarded=$err.Result
    $code=$process.ExitCode; $process.Dispose()
    return [pscustomobject]@{exit_code=$code;stdout=$stdout}
}

function Find-ExactPython {
    $candidates = @($PythonPath,$script:ManagedPython,$script:PrivatePython)
    $command=Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\*') { $candidates += $command.Source }
    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $probe=Invoke-Captured $candidate @('-I','-S','-B','-X','utf8','-c','import platform,struct;print(platform.python_version()+"/"+str(struct.calcsize("P")*8))')
            if ($probe.exit_code -eq 0 -and $probe.stdout.Trim() -eq '3.14.6/64') { return (Assert-OrdinaryPath $candidate) }
        } catch { if ($_.Exception.Message -eq 'setup_process_still_running_no_repeat') { throw } }
    }
    return $null
}

function Invoke-Coordinator([string]$Action, [switch]$Initialize, [switch]$Downloads) {
    $arguments=@('-I','-B','-X','utf8',(Join-Path $PSScriptRoot 'news_first_run.py'),$Action,'--data-root',$DataRoot,'--port',"$Port")
    $runtimePython=if ($PythonPath) {$PythonPath} else {$script:ManagedPython}
    $arguments += @('--python',$runtimePython)
    if ($Offline) { $arguments+='--offline' }; if ($Initialize) { $arguments+='--initialize-new' }
    if ($Downloads) { $arguments+='--allow-dependency-downloads' }
    $timeout=if($Action -eq 'bootstrap'){2700}else{70}
    $result=Invoke-Captured $script:Python $arguments $timeout $Action
    if ($result.exit_code -ne 0) { throw 'setup_step_failed' }
    return ($result.stdout | ConvertFrom-Json)
}

function Download-FixedPackage($Item, [string]$Name) {
    if ($Offline) { throw 'offline_download_refused' }
    $uri=[Uri]$Item.url
    if ($uri.Scheme -ne 'https' -or $uri.Host -notin @('www.python.org','nodejs.org') -or $Item.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'runtime_manifest_invalid' }
    $folder=Assert-OrdinaryPath (Join-Path $script:Project 'runtime\toolchains\downloads')
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $target=Join-Path $folder $Name
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Item.sha256) { throw 'existing_package_hash_mismatch' }
        return $target
    }
    $part=Join-Path $folder ($Name+'.'+[Guid]::NewGuid().ToString('N')+'.partial')
    $request=[Net.HttpWebRequest]::Create($uri); $request.AllowAutoRedirect=$false
    $request.Timeout=45000; $request.ReadWriteTimeout=45000; $request.Proxy=$null
    $pending=$request.BeginGetResponse($null,$null); $deadline=[DateTime]::UtcNow.AddSeconds(45)
    while (-not $pending.AsyncWaitHandle.WaitOne(100)) {
        [Windows.Forms.Application]::DoEvents()
        if([DateTime]::UtcNow -gt $deadline){$request.Abort();throw 'fixed_download_timeout'}
    }
    $response=$request.EndGetResponse($pending)
    try {
        if ([int]$response.StatusCode -ne 200 -or $response.ResponseUri.AbsoluteUri -ne $uri.AbsoluteUri -or $response.ContentLength -gt $Item.maximum_bytes) { throw 'fixed_download_response_refused' }
        $downloadStream=$response.GetResponseStream(); $output=[IO.File]::Open($part,[IO.FileMode]::CreateNew)
        try {
            $buffer=New-Object byte[] 65536; $total=0L
            while ($true) {
                $reading=$downloadStream.ReadAsync($buffer,0,$buffer.Length); $deadline=[DateTime]::UtcNow.AddSeconds(45)
                while (-not $reading.Wait(100)) {
                    [Windows.Forms.Application]::DoEvents()
                    if([DateTime]::UtcNow -gt $deadline){$request.Abort();throw 'fixed_download_timeout'}
                }
                $read=$reading.Result; if($read -eq 0){break}
                $total += $read; if ($total -gt $Item.maximum_bytes) { throw 'fixed_download_too_large' }
                $output.Write($buffer,0,$read); [Windows.Forms.Application]::DoEvents()
            }
        } finally { $downloadStream.Dispose(); $output.Dispose() }
    } finally { $response.Dispose() }
    if ($Item.PSObject.Properties.Name -contains 'bytes') { if ((Get-Item -LiteralPath $part).Length -ne $Item.bytes) { throw 'fixed_download_size_mismatch' } }
    if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Item.sha256) { throw 'fixed_download_hash_mismatch' }
    [IO.File]::Move($part,$target)
    return $target
}

function Expand-FixedRuntime([string]$Archive, [string]$Destination, [string]$Prefix='') {
    $destination=Assert-OrdinaryPath $Destination
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $expanded=0L
        if($zip.Entries.Count -gt 50000){throw 'runtime_archive_too_many_files'}
        foreach ($entry in $zip.Entries) {
            $expanded += $entry.Length; $name=$entry.FullName.Replace('\','/')
            $final=[IO.Path]::GetFullPath((Join-Path $destination $name))
            if ($expanded -gt 1073741824 -or ($Prefix -and -not $name.StartsWith($Prefix)) -or $name.Contains('../') -or $name.Contains(':') -or -not $final.StartsWith($destination.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase) -or (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'runtime_archive_path_refused' }
        }
        [IO.Directory]::CreateDirectory($destination) | Out-Null
        foreach($entry in $zip.Entries){
            $final=Assert-OrdinaryPath (Join-Path $destination $entry.FullName)
            if($entry.FullName.EndsWith('/')){[IO.Directory]::CreateDirectory($final)|Out-Null;continue}
            [IO.Directory]::CreateDirectory((Split-Path -Parent $final)) | Out-Null
            $reader=$entry.Open();$writer=[IO.File]::Open($final,[IO.FileMode]::CreateNew)
            try{
                $buffer=New-Object byte[] 65536
                while(($count=$reader.Read($buffer,0,$buffer.Length)) -gt 0){$writer.Write($buffer,0,$count);[Windows.Forms.Application]::DoEvents()}
            }finally{$reader.Dispose();$writer.Dispose()}
        }
    } finally { $zip.Dispose() }
}

function Install-Foundation {
    # This function is reached only after the welcome screen's explicit consent.
    if ($Offline) { throw 'offline_download_refused' }
    if (-not $script:Python) {
        $archive=Download-FixedPackage $script:Manifest.python 'python-3.14.6-amd64.zip'
        $target=Assert-OrdinaryPath (Split-Path -Parent $script:PrivatePython)
        if (Test-Path -LiteralPath $target) { throw 'existing_python_target_preserved' }
        Expand-FixedRuntime $archive $target
        foreach($required in @('python.exe','Lib\venv\__init__.py','Lib\ensurepip\__init__.py')) {
            if(-not (Test-Path -LiteralPath (Join-Path $target $required) -PathType Leaf)){throw 'python_full_runtime_required'}
        }
        $script:Python=Find-ExactPython
        if (-not $script:Python) { throw 'python_version_not_confirmed' }
    }
    $node=Join-Path $script:PrivateNode 'node.exe'; $npm=Join-Path $script:PrivateNode 'npm.cmd'
    $nodeCommand=Get-Command node.exe -ErrorAction SilentlyContinue
    $npmCommand=Get-Command npm.cmd -ErrorAction SilentlyContinue
    $nodeOK=$false
    if ($nodeCommand -and $npmCommand) {
        try {
            $nodeProbe=Invoke-Captured $nodeCommand.Source @('--version')
            $npmCli=Join-Path (Split-Path -Parent $npmCommand.Source) 'node_modules\npm\bin\npm-cli.js'
            $npmProbe=Invoke-Captured $nodeCommand.Source @($npmCli,'--version')
            $nodeOK=($nodeProbe.stdout.Trim() -eq 'v24.16.0' -and $npmProbe.stdout.Trim() -eq '11.13.0')
        } catch { if ($_.Exception.Message -eq 'setup_process_still_running_no_repeat') { throw } }
    }
    if (-not $nodeOK -and -not (Test-Path -LiteralPath $node)) {
        $archive=Download-FixedPackage $script:Manifest.node 'node-v24.16.0-win-x64.zip'
        $destination=Assert-OrdinaryPath (Join-Path $script:Project 'runtime\toolchains')
        if (Test-Path -LiteralPath $script:PrivateNode) { throw 'existing_node_target_preserved' }
        Expand-FixedRuntime $archive $destination 'node-v24.16.0-win-x64/'
    }
}

function Inspect-Welcome {
    if($script:PendingProcess){return (Pending-Result)}
    if($Port -lt 1024 -or $Port -gt 65535 -or $Port -in @(8770,11111,18787)){
        return [pscustomobject]@{code='protected_or_invalid_port';title='此工作台地址不能使用';detail='受保护服务保持原状。请使用独立消息工作台的地址；不会停止或复用其他服务。';action='diagnose';diagnostic='消息工作台安全诊断：protected_or_invalid_port。未启动或下载。'}
    }
    if (-not $script:Manifest) {
        return [pscustomobject]@{code='runtime_manifest_invalid';title='运行组件清单无法读取';detail='未下载或执行任何安装。请复制安全诊断并保留本软件文件。';action='diagnose';diagnostic='消息工作台安全诊断：runtime_manifest_invalid。未包含密钥或本机路径。'}
    }
    $script:Python=Find-ExactPython
    if (-not $script:Python) {
        return [pscustomobject]@{code='runtime_missing';title='欢迎使用消息工作台';detail='这台电脑还没有所需的运行组件。确认后可下载固定版本的 Python、Node 和锁定依赖，再创建独立消息空间。';action='install';diagnostic='消息工作台安全诊断：runtime_missing。未包含密钥或本机路径。'}
    }
    return (Invoke-Coordinator 'inspect')
}

if ($CheckOnly) {
    try { $value=Inspect-Welcome; $null=Save-SafeReceipt $value.code $value.detail; $value | ConvertTo-Json -Depth 6 }
    catch { Save-SafeReceipt 'welcome_check_failed' '环境检查未完成，未启动或下载。' | ConvertTo-Json -Depth 6 }
    exit
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form=New-Object Windows.Forms.Form
$form.Text='消息工作台'; $form.StartPosition='CenterScreen'; $form.Size=New-Object Drawing.Size(640,440)
$form.MinimumSize=New-Object Drawing.Size(640,440); $form.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$title=New-Object Windows.Forms.Label; $title.Location=New-Object Drawing.Point(28,28); $title.Size=New-Object Drawing.Size(560,45); $title.Font=New-Object Drawing.Font('Microsoft YaHei UI',16)
$detail=New-Object Windows.Forms.Label; $detail.Location=New-Object Drawing.Point(28,88); $detail.Size=New-Object Drawing.Size(560,145)
$mode=New-Object Windows.Forms.Label; $mode.Location=New-Object Drawing.Point(28,238); $mode.Size=New-Object Drawing.Size(560,45); $mode.Text='当前方式：独立消息工作台。关闭网页不会停止后台；登录自动启动暂不适用于此方式。'
$primary=New-Object Windows.Forms.Button; $primary.Location=New-Object Drawing.Point(28,302); $primary.Size=New-Object Drawing.Size(250,42)
$refresh=New-Object Windows.Forms.Button; $refresh.Text='重新检查'; $refresh.Location=New-Object Drawing.Point(294,302); $refresh.Size=New-Object Drawing.Size(120,42)
$copy=New-Object Windows.Forms.Button; $copy.Text='复制安全诊断'; $copy.Location=New-Object Drawing.Point(430,302); $copy.Size=New-Object Drawing.Size(150,42)
$feedback=New-Object Windows.Forms.Label; $feedback.Location=New-Object Drawing.Point(28,354); $feedback.Size=New-Object Drawing.Size(560,30)
$form.Controls.AddRange(@($title,$detail,$mode,$primary,$refresh,$copy,$feedback))

function Render-Result($value) {
    $script:LastResult=$value; $title.Text=$value.title; $detail.Text=$value.detail
    $primary.Enabled=($value.action -in @('install','initialize','launch','open','check_pending')) -and -not ($Offline -and $value.action -eq 'install')
    $primary.Text=switch($value.action){'install'{'确认安装运行组件'}'initialize'{'创建独立消息空间并打开'}'launch'{'打开已有消息'}'open'{'进入消息列表'}'check_pending'{'继续核对准备结果'}default{'请保留数据并复制诊断'}}
}
function Pending-Result {
    return [pscustomobject]@{code='setup_process_still_running_no_repeat';title='准备结果尚未确认';detail='已保留本次准备进程。继续核对只检查它是否完成，不会启动另一份安装或后台；请暂时保持此窗口打开。';action='check_pending';diagnostic='消息工作台安全诊断：setup_process_still_running_no_repeat。未重复执行。'}
}
function Show-Problem($Problem=$null) {
    $code=if($Problem){$Problem.Exception.Message}else{'welcome_action_failed'}
    $known=@{
        'fixed_download_timeout'=@('下载等待超时','本次下载未完成，未执行安装。请检查网络后重新检查；已保存的消息不受影响。');
        'fixed_download_hash_mismatch'=@('运行组件校验未通过','下载文件与固定官方摘要不一致，未执行或解压。请保留诊断，不要手动运行该文件。');
        'existing_package_hash_mismatch'=@('已保存组件需要核查','已有组件摘要不符，未覆盖或执行。请保留文件并复制诊断。');
        'fixed_download_size_mismatch'=@('运行组件不完整','收到的文件大小不符，未安装。请检查网络并保留诊断。');
        'fixed_download_too_large'=@('下载内容超出预期','下载已停止，未安装。请保留诊断后核查下载来源。');
        'fixed_download_response_refused'=@('官方组件响应未通过核对','未跟随跳转或执行下载内容，请检查网络后重试检查。');
        'path_alias_refused'=@('此位置需要安全检查','文件夹是链接或重定向位置，未写入。请使用独立普通文件夹并保留已有文件。');
        'existing_python_target_preserved'=@('已有运行组件保持原状','目标目录已有内容，未覆盖。请复制诊断以核对是否为不完整安装。');
        'existing_node_target_preserved'=@('已有运行组件保持原状','目标目录已有内容，未覆盖。请复制诊断以核对是否为不完整安装。');
        'runtime_archive_path_refused'=@('组件压缩包结构异常','未解压，已有数据保持。请保留诊断，不要手动覆盖软件文件。');
        'python_full_runtime_required'=@('Python运行组件不完整','未创建消息空间。请保留诊断和已解压目录，不要覆盖已有数据。');
        'python_version_not_confirmed'=@('Python版本尚未确认','未准备依赖或创建消息。请保留诊断后核查完整运行组件。');
        'offline_download_refused'=@('离线验证不会下载','请使用已准备好的独立运行环境。');
        'setup_step_failed'=@('准备步骤未完成','未自动重试。请复制安全诊断并保留已有组件与数据。')
        'another_setup_in_progress'=@('另一个欢迎窗口正在准备','请回到先前的欢迎窗口继续核对。这里未重复下载、解压或安装；准备完成后可重新检查。')
    }
    if($script:PendingProcess -or $code -eq 'setup_process_still_running_no_repeat'){Render-Result (Pending-Result);return}
    if(-not $known.ContainsKey($code)){
        $inner=if($Problem){$Problem.Exception}else{$null}; $networkFailure=$false
        while($inner){if($inner -is [Net.WebException]){$networkFailure=$true};if(-not $inner.InnerException){break};$inner=$inner.InnerException}
        if($networkFailure){$code='official_download_unavailable';$known[$code]=@('无法连接官方下载位置','未安装组件。请检查网络后重新检查，或复制安全诊断。')}
        elseif($inner -is [UnauthorizedAccessException]){$code='location_not_writable';$known[$code]=@('保存位置不可写入','请检查文件夹权限或可用空间；已有消息未删除。')}
        elseif($inner -is [IO.IOException]){$code='file_operation_unconfirmed';$known[$code]=@('文件操作未完成','请检查剩余空间或文件是否被占用；已保留现有文件，不要手动删除数据。')}
        else{$code='welcome_action_failed';$known[$code]=@('暂时无法继续','本次操作未确认完成；原数据和已有进程保持。请复制安全诊断。')}
    }
    $saved=Save-SafeReceipt $code $known[$code][1]
    Render-Result ([pscustomobject]@{code=$code;title=$known[$code][0];detail=$known[$code][1];action='diagnose';diagnostic=$saved.diagnostic})
}
function Complete-Pending {
    if(-not $script:PendingProcess){return}
    if(-not $script:PendingProcess.HasExited){Render-Result (Pending-Result);return}
    $code=$script:PendingProcess.ExitCode; $stdout=$script:PendingOutput.Result; $discarded=$script:PendingError.Result
    $purpose=$script:PendingIdentity.purpose
    $script:PendingIdentity.exit_code=$code
    $null=Save-SafeReceipt 'owned_setup_process_exited' '仅核对原持有进程真实退出；未重复执行。'
    $script:PendingProcess.Dispose();$script:PendingProcess=$null;$script:PendingOutput=$null;$script:PendingError=$null;$script:PendingIdentity=$null
    Leave-PreparationGate
    if($code -ne 0){throw 'setup_step_failed'}
    if($purpose -eq 'probe'){Render-Result (Inspect-Welcome)}else{Render-Result ($stdout | ConvertFrom-Json)}
}
function Refresh-Welcome {
    if($script:Busy){return}
    $script:Busy=$true; $primary.Enabled=$false; $refresh.Enabled=$false
    try { Render-Result (Inspect-Welcome) } catch { Show-Problem $_ }
    finally {$script:Busy=$false;$refresh.Enabled=$true;if($script:LastResult){Render-Result $script:LastResult}}
}
function Open-ConfirmedWorkspace {
    # A welcome window may outlive its host. Never trust an earlier UI status
    # when the user follows the link; inspect only, without starting a process.
    $fresh=Invoke-Coordinator 'inspect'
    Render-Result $fresh
    if($fresh.code -eq 'already_running' -and $fresh.action -eq 'open' -and -not $NoBrowser){
        Start-Process ("http://127.0.0.1:{0}/" -f $Port)
    }
}
$copy.Add_Click({
    if($script:LastResult){
        try{[Windows.Forms.Clipboard]::SetText($script:LastResult.diagnostic);$feedback.Text='已复制安全诊断，不包含密钥或消息正文。'}
        catch{$feedback.Text='暂时未能复制，请重试。未读取原剪贴板内容。'}
    }
})
$refresh.Add_Click({ if(-not $script:Busy){ Refresh-Welcome } })
$primary.Add_Click({
    if($script:Busy -or -not $script:LastResult){return}
    $script:Busy=$true; $primary.Enabled=$false; $refresh.Enabled=$false
    try {
        $action=$script:LastResult.action
        if($action -eq 'check_pending'){Complete-Pending}
        elseif($action -eq 'install') {
            $question="将按需从官方固定地址下载、核对 SHA256 并解压完整 Python 3.14.6 运行组件及 Node 24.16.0（npm 11.13.0），再安装锁定依赖。组件保存在本软件 runtime 目录；不运行系统 Python 安装器，不覆盖其他 Python 或已有消息，不修改系统 PATH。需要网络和磁盘空间，但不调用 AI。是否继续？"
            if([Windows.Forms.MessageBox]::Show($question,'确认安装',[Windows.Forms.MessageBoxButtons]::YesNo,[Windows.Forms.MessageBoxIcon]::Question) -ne [Windows.Forms.DialogResult]::Yes){return}
            Enter-PreparationGate
            # Another window may have finished while this consent dialog was
            # visible. Recheck inside the shared preparation gate before work.
            Render-Result (Inspect-Welcome)
            if($script:LastResult.action -ne 'install'){return}
            $detail.Text='正在准备固定版本组件，请稍候；不会创建或修改已有消息。'; [Windows.Forms.Application]::DoEvents()
            Install-Foundation
            Render-Result (Invoke-Coordinator 'bootstrap' -Downloads)
        } elseif($action -eq 'initialize') {
            if([Windows.Forms.MessageBox]::Show('将创建新的独立消息空间，不配置 AI、不覆盖已有文件。是否继续？','首次设置',[Windows.Forms.MessageBoxButtons]::YesNo) -ne [Windows.Forms.DialogResult]::Yes){return}
            Render-Result (Invoke-Coordinator 'launch' -Initialize)
        } elseif($action -eq 'launch') { Render-Result (Invoke-Coordinator 'launch') }
        if($script:LastResult.action -eq 'open') { Open-ConfirmedWorkspace }
        $null=Save-SafeReceipt $script:LastResult.code $script:LastResult.detail
    } catch { Show-Problem $_ }
    finally { Leave-PreparationGate; $script:Busy=$false; $refresh.Enabled=$true; if($script:LastResult){Render-Result $script:LastResult} }
})
$form.Add_FormClosing({param($sender,$eventArgs) if($script:Busy -or $script:PendingProcess){$eventArgs.Cancel=$true}})
$form.Add_Shown({
    Refresh-Welcome
    if($script:LastResult -and $script:LastResult.action -in @('launch','open') -and -not $script:PendingProcess){
        $script:Busy=$true;$primary.Enabled=$false;$refresh.Enabled=$false
        try{
            if($script:LastResult.action -eq 'launch'){Render-Result (Invoke-Coordinator 'launch')}
            if($script:LastResult.action -eq 'open'){Open-ConfirmedWorkspace}
            $null=Save-SafeReceipt $script:LastResult.code $script:LastResult.detail
        }catch{Show-Problem $_}
        finally{$script:Busy=$false;$refresh.Enabled=$true;if($script:LastResult){Render-Result $script:LastResult}}
    }
})
[void]$form.ShowDialog()
