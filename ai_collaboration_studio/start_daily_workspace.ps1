[CmdletBinding()]
param([switch]$CheckOnly, [switch]$Background)
$ErrorActionPreference='Stop'
$studioWorkspace=Split-Path -Parent $PSScriptRoot
$env:AI_STUDIO_SKIP_LOCAL_ENV='1'
$env:AI_STUDIO_DATABASE_PATH=Join-Path $studioWorkspace 'ai_collaboration_studio\runtime\collaboration_studio.sqlite3'
$env:AI_STUDIO_RUNTIME_DIR=Join-Path $PSScriptRoot 'runtime'
$env:AI_STUDIO_HOST='127.0.0.1'
$env:AI_STUDIO_PORT='8770'
$env:AI_STUDIO_DISABLED_PROVIDERS='openai,doubao,deepseek,qwen,glm'
foreach($studioKeyName in @('OPENAI_API_KEY','ARK_API_KEY','DOUBAO_API_KEY','DEEPSEEK_API_KEY','DASHSCOPE_API_KEY','QWEN_API_KEY','GLM_API_KEY','ZHIPU_API_KEY','ZHIPUAI_API_KEY','BAILIAN_TOKEN_PLAN_API_KEY')){
    Remove-Item -LiteralPath ('Env:'+ $studioKeyName) -ErrorAction SilentlyContinue
}
& (Join-Path $PSScriptRoot 'scripts\start_ai_collaboration_studio.ps1') -CheckOnly:$CheckOnly -NoWindow:$Background
exit $LASTEXITCODE
