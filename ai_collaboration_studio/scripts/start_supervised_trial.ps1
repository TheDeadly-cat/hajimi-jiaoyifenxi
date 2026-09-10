[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [ValidateSet('Offline', 'Online')]
    [string]$Mode = 'Offline',
    [string]$NetworkConfigPath,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Assert-ExactProperties {
    param($Value, [string[]]$Names, [string]$Label)
    if ($null -eq $Value -or $Value -isnot [pscustomobject]) {
        throw "$Label 必须是 JSON 对象。"
    }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if (@(Compare-Object -ReferenceObject $expected -DifferenceObject $actual).Count -ne 0) {
        throw "$Label 缺少必要字段或包含不支持的字段。"
    }
}

function Get-AbsolutePath {
    param($Value, [string]$Label)
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value) -or
        $Value -notmatch '^[a-zA-Z]:[\\/]' -or $Value.StartsWith("\\")) {
        throw "$Label 必须是本机绝对路径。"
    }
    $resolved = [System.IO.Path]::GetFullPath($Value)
    $cursor = $resolved
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label 不得包含链接或重解析点。"
            }
        }
        $parent = [System.IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $resolved
}

function Test-DescendantPath {
    param([string]$Path, [string]$Parent)
    $prefix = $Parent.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    return $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-BackendSourceSha256 {
    [string[]]$paths = @("server.py") + @(Get-ChildItem -LiteralPath (Join-Path $projectRoot "backend") -Recurse -File -Filter "*.py" | ForEach-Object {
        $_.FullName.Substring($projectRoot.Length + 1).Replace("\", "/")
    })
    [Array]::Sort($paths, [System.StringComparer]::Ordinal)
    $fingerprint = [System.Text.StringBuilder]::new()
    foreach ($relative in $paths) {
        $path = Join-Path $projectRoot $relative.Replace("/", "\")
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        [void]$fingerprint.Append($relative).Append([char]0).Append($hash).Append("`n")
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return -join ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($fingerprint.ToString())) | ForEach-Object { $_.ToString("x2") })
    } finally { $sha.Dispose() }
}

function Assert-FrontendBuild {
    param($Files)
    if ($Files -isnot [array] -or $Files.Count -eq 0) {
        throw "frontend_files 必须是已复核生产构建的非空文件清单。"
    }
    $dist = Get-AbsolutePath (Join-Path $projectRoot "frontend\dist") "Frontend directory"
    if (-not (Test-Path -LiteralPath $dist -PathType Container)) {
        throw "缺少已复核的前端生产构建。"
    }
    $expected = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $Files) {
        Assert-ExactProperties $entry @("path", "sha256") "Frontend file"
        if ($entry.path -isnot [string] -or $entry.path -notmatch '^[^\\:]+$' -or
            $entry.path.StartsWith('/') -or @($entry.path.Split('/') | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0 -or
            $entry.sha256 -isnot [string] -or $entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            -not $expected.Add($entry.path)) {
            throw "前端文件清单无效或包含重复路径。"
        }
        $file = Get-AbsolutePath (Join-Path $dist $entry.path.Replace('/', '\')) "Frontend file"
        if (-not (Test-DescendantPath $file $dist) -or -not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "已复核的前端文件缺失，或路径超出 frontend/dist。"
        }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.sha256) {
            throw "前端生产文件与已复核构建不一致，请重新确认版本。"
        }
    }
    $actual = @(Get-ChildItem -LiteralPath $dist -Recurse -File -Force | ForEach-Object {
        $_.FullName.Substring($dist.Length + 1).Replace('\', '/')
    })
    if (-not $expected.Contains('index.html') -or $actual.Count -ne $expected.Count -or
        @($actual | Where-Object { -not $expected.Contains($_) }).Count -gt 0) {
        throw "前端清单必须完整匹配生产构建的所有文件。"
    }
}

$savedEnvironment = @{}
$environmentApplied = $false
$serverInvoked = $false
$serverExitCode = 1
try {
    if ($Mode -eq 'Offline' -and $PSBoundParameters.ContainsKey('NetworkConfigPath')) {
        throw '离线模式不接受联网配置。请明确选择 -Mode Online，或移除 -NetworkConfigPath。'
    }
    if ($Mode -eq 'Online' -and [string]::IsNullOrWhiteSpace($NetworkConfigPath)) {
        throw '联网模式必须显式提供 -NetworkConfigPath；不会读取外层环境中的监控授权。'
    }
    $configFile = Get-AbsolutePath $ConfigPath "ConfigPath"
    if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) { throw "试用配置文件不存在。" }
    try { $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "试用配置不是有效 JSON。" }
    Assert-ExactProperties $config @('format_version', 'candidate_sha', 'data_directory', 'database_path', 'host', 'port', 'backend_source_sha256', 'frontend_files') "Trial configuration"
    if ($config.format_version -isnot [string] -or $config.format_version -cne 'studio_supervised_trial_v1' -or
        $config.candidate_sha -isnot [string] -or $config.candidate_sha -cnotmatch '^[0-9a-f]{40}$' -or
        $config.backend_source_sha256 -isnot [string] -or $config.backend_source_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw "试用配置版本或源码指纹无效。"
    }
    if ($config.host -isnot [string] -or $config.host -cne '127.0.0.1' -or ($config.port -isnot [int] -and $config.port -isnot [long]) -or
        $config.port -lt 1 -or $config.port -gt 65535) {
        throw "试用必须使用 127.0.0.1，端口必须是 1 至 65535 的整数。"
    }
    $dataDirectory = Get-AbsolutePath $config.data_directory "Data directory"
    $databasePath = Get-AbsolutePath $config.database_path "Database path"
    $localData = Get-AbsolutePath ([Environment]::GetFolderPath('LocalApplicationData')) "Local application data"
    $systemTemp = Get-AbsolutePath ([System.IO.Path]::GetTempPath()) "System temporary directory"
    if (-not (Test-DescendantPath $dataDirectory $localData) -or
        $dataDirectory.TrimEnd('\') -ieq $systemTemp.TrimEnd('\') -or (Test-DescendantPath $dataDirectory $systemTemp) -or
        (Test-DescendantPath $dataDirectory $projectRoot) -or -not (Test-DescendantPath $databasePath $dataDirectory)) {
        throw "试用数据必须位于 LOCALAPPDATA 下独立持久目录，不能位于 TEMP 或源码目录。"
    }
    if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) {
        throw "独立试用数据库不存在。本入口不会新建、恢复或迁移数据库。"
    }
    $secUserAgent = $null
    $secDirect = $false
    if ($Mode -eq 'Online') {
        $networkFile = Get-AbsolutePath $NetworkConfigPath 'NetworkConfigPath'
        try { $network = Get-Content -LiteralPath $networkFile -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw '联网配置不存在或不是有效 JSON。' }
        Assert-ExactProperties $network @('format_version', 'data_directory', 'sec_user_agent_file', 'sec_route') 'Network configuration'
        if ($network.format_version -isnot [string] -or
            $network.format_version -cne 'studio_source_collection_v1' -or
            (Get-AbsolutePath $network.data_directory 'Online data directory') -ine $dataDirectory -or
            $network.sec_route -isnot [string] -or
            $network.sec_route -cnotin @('inherit', 'sec_direct_only')) {
            throw '联网配置必须绑定当前独立数据目录，且只允许继承路由或 SEC 域名直连。'
        }
        $contactFile = Get-AbsolutePath $network.sec_user_agent_file 'SEC contact file'
        if ((Test-DescendantPath $contactFile $projectRoot) -or
            -not (Test-Path -LiteralPath $contactFile -PathType Leaf) -or
            (Get-Item -LiteralPath $contactFile).Length -gt 1024) {
            throw 'SEC 联系标识文件必须保存在源码目录之外，且不超过 1024 字节。'
        }
        $secUserAgent = [System.IO.File]::ReadAllText($contactFile, [System.Text.Encoding]::UTF8).Trim()
        if ($secUserAgent.Length -lt 10 -or $secUserAgent.Length -gt 300 -or
            $secUserAgent -match '[^\x20-\x7E]' -or
            $secUserAgent -notmatch '^.+\s+[^\s@]+@[^\s@]+\.[^\s@]+$') {
            throw 'SEC 联系标识需要单行应用／组织名称及联系邮箱；内容不会打印。'
        }
        $secDirect = $network.sec_route -ceq 'sec_direct_only'
    }
    $runtimeDirectory = Join-Path $dataDirectory 'runtime'
    [void](Get-AbsolutePath $runtimeDirectory "Runtime directory")
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "需要 Git 才能核验试用候选版本。" }
    $head = & git -C $projectRoot rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or ([string]$head).Trim() -cne $config.candidate_sha) {
        throw "当前 Git 候选与试用配置不一致。"
    }
    & git -C $projectRoot diff --quiet HEAD --
    if ($LASTEXITCODE -ne 0) { throw "已跟踪源码存在修改。请复核并绑定新候选后再启动。" }
    if ((Get-BackendSourceSha256) -cne $config.backend_source_sha256) {
        throw "后端源码与已复核候选指纹不一致。"
    }
    Assert-FrontendBuild $config.frontend_files
    $python = Join-Path $projectRoot 'runtime\bootstrap\python\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "缺少受管理的 Python 环境。请先按现有 bootstrap 流程准备依赖。"
    }
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    if (@($listeners | Where-Object { $_.Port -eq $config.port }).Count -gt 0) {
        throw "端口 $($config.port) 已被占用。本入口没有停止或替换任何已有服务。"
    }
    $url = "http://127.0.0.1:$($config.port)/"
    Write-Host ("候选版本：" + $config.candidate_sha)
    Write-Host ("试用数据库：" + $databasePath)
    Write-Host ("网页地址：" + $url)
    if ($Mode -eq 'Online') {
        Write-Host '运行模式：联网自动采集；固定 sec_micron_trial_v1，NVDA 8-K / Micron recent-30，每 300 秒检查。'
        Write-Host '启用全局监控与后台调度，关闭 dry-run；逐来源开关沿用本数据目录的状态。首次须在网页逐来源预览并确认启用。'
        Write-Host '首次 seed_only 只建立历史基线。来源失败分别显示；本入口不执行 AI 分析，全部模型 Provider 仍禁用。'
        Write-Host $(if ($secDirect) { '网络：仅为当前进程追加 SEC 两个域名的代理例外；Micron 沿用现有网络。' } else { '网络：继承现有路由，不修改代理配置。' })
    } else {
        Write-Host '运行模式：离线人工研究；来源监控关闭，全部模型 Provider 禁用。'
    }
    if ($CheckOnly) {
        Write-Host '配置与构建检查通过。未打开数据库，未启动服务；宿主 readiness 尚未验证。'
        exit 0
    }
    $trialEnvironment = @{
        AI_STUDIO_SKIP_LOCAL_ENV = '1'
        AI_STUDIO_RUNTIME_DIR = $runtimeDirectory
        AI_STUDIO_DATABASE_PATH = $databasePath
        AI_STUDIO_HOST = '127.0.0.1'
        AI_STUDIO_PORT = [string]$config.port
        AI_STUDIO_SOURCE_MONITOR_PROFILE = 'sec_micron_trial_v1'
        AI_STUDIO_SOURCE_MONITOR_ENABLED = '0'
        AI_STUDIO_SOURCE_MONITOR_AUTO_START = '0'
        AI_STUDIO_SOURCE_MONITOR_OFFICIAL_ONLY = '1'
        AI_STUDIO_SOURCE_MONITOR_ALLOW_READONLY_MARKET = '0'
        AI_STUDIO_SOURCE_MONITOR_DRY_RUN = '1'
        AI_STUDIO_SOURCE_MONITOR_INITIAL_MODE = 'seed_only'
        AI_STUDIO_SOURCE_MONITOR_MAX_ITEMS_PER_RUN = '50'
        AI_STUDIO_SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED = '0'
        AI_STUDIO_DISABLED_PROVIDERS = 'openai,deepseek,doubao,glm'
        FUTU_HOST = '127.0.0.1'
        FUTU_PORT = '1'
        PYTHONUTF8 = '1'
        PYTHONIOENCODING = 'utf-8'
    }
    if ($Mode -eq 'Online') {
        $trialEnvironment.AI_STUDIO_SOURCE_MONITOR_ENABLED = '1'
        $trialEnvironment.AI_STUDIO_SOURCE_MONITOR_AUTO_START = '1'
        $trialEnvironment.AI_STUDIO_SOURCE_MONITOR_DRY_RUN = '0'
        $trialEnvironment.SEC_USER_AGENT = $secUserAgent
        if ($secDirect) {
            $bypass = @(([Environment]::GetEnvironmentVariable('NO_PROXY', 'Process') -split ',') |
                ForEach-Object { $_.Trim() } | Where-Object { $_ })
            $trialEnvironment.NO_PROXY = (@($bypass + @('www.sec.gov', 'data.sec.gov')) | Select-Object -Unique) -join ','
        }
    }
    $clearNames = @('AI_STUDIO_SOURCE_MONITOR_CATCH_UP_MAX_ITEMS', 'AI_STUDIO_SOURCE_MONITOR_INITIAL_PREVIEW_SHA256',
        'AI_STUDIO_SOURCE_MONITOR_FROM_TIME', 'AI_STUDIO_SOURCE_MONITOR_CONTINUOUS_EVENT_CUTOFF', 'SEC_USER_AGENT',
        'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'ARK_API_KEY', 'DOUBAO_API_KEY', 'GLM_API_KEY', 'ZHIPU_API_KEY', 'ZHIPUAI_API_KEY',
        'AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET')
    if ($Mode -eq 'Online') { $clearNames = @($clearNames | Where-Object { $_ -ne 'SEC_USER_AGENT' }) }
    foreach ($name in @($trialEnvironment.Keys) + $clearNames) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    $environmentApplied = $true
    foreach ($name in $trialEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $trialEnvironment[$name], 'Process') }
    foreach ($name in $clearNames) { Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue }
    Write-Host '请保留此控制台。看到 server_started 后打开上方网址。停止时在这里按 Ctrl+C，等待 server_stopped 和命令提示符返回。'
    Push-Location -LiteralPath $projectRoot
    try {
        $serverInvoked = $true
        & $python -X utf8 -B server.py
        $serverExitCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($serverExitCode -ne 0) {
        Write-Host "前台进程返回码：$serverExitCode。若刚按 Ctrl+C，请核对 server_stopped 及端口、owner 释放；仅凭此返回码不能判断收尾成功。" -ForegroundColor Yellow
    }
} catch {
    if ($serverInvoked) {
        Write-Host '前台运行已结束或被中断。请核对 server_stopped 及端口、owner 释放，不能仅凭窗口关闭判断已停止。' -ForegroundColor Yellow
    } else {
        Write-Host ("启动已拒绝：" + $_.Exception.Message) -ForegroundColor Red
    }
    $serverExitCode = 1
} finally {
    if ($environmentApplied) {
        foreach ($name in $savedEnvironment.Keys) {
            if ($null -eq $savedEnvironment[$name]) { Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process') }
        }
    }
}
exit $serverExitCode
