$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '请先按照 README 创建 .venv 并安装依赖。'
}
$taskRuntime = (& $taskPython -c "import json,os;from bank_agent.config import Settings;s=Settings.from_env();print(json.dumps({'local_chroma':s.memory_backend=='chroma' and not s.memory_chroma_url,'workers':int(os.getenv('BANK_WEB_WORKERS','1'))}))") | ConvertFrom-Json
$taskWorkers = $taskRuntime.workers
if ($taskWorkers -lt 1 -or $taskWorkers -gt 4) { throw '本地演示支持 1 至 4 个 Web worker。' }
$taskScheduler = $null
if ($taskRuntime.local_chroma) {
    if ($taskWorkers -ne 1) { throw '本地 Chroma 使用单 worker；多个 worker 请先配置 BANK_CHROMA_URL。' }
    $env:BANK_SCHEDULER_MODE = 'embedded'
} else {
    $env:BANK_SCHEDULER_MODE = 'external'
    $taskScheduler = Start-Process -FilePath $taskPython -ArgumentList @('-m','bank_agent.scheduler') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
}
try {
    & $taskPython -m uvicorn bank_agent.main:app --host 127.0.0.1 --port 8000 --workers $taskWorkers
} finally {
    if ($taskScheduler -and -not $taskScheduler.HasExited) { Stop-Process -Id $taskScheduler.Id -ErrorAction SilentlyContinue }
}
