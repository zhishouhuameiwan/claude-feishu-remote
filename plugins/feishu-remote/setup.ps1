# 飞书远程回复 Claude Code：安装 / 更新 / 重新配置
# 用法（在 PowerShell 里运行，不需要管理员权限）：
#   powershell -ExecutionPolicy Bypass -File setup.ps1              首次安装，或更新后重新运行
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Reconfigure 重新输入 App ID / App Secret
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Rebind      换一个飞书账号接收卡片
# 详细步骤见 README.md。

param(
    [switch]$Reconfigure,
    [switch]$Rebind
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Feishu Remote Listener'
$PluginRoot = $PSScriptRoot
$Scripts = Join-Path $PluginRoot 'scripts'
$Data = if ($env:FEISHU_REMOTE_HOME) { $env:FEISHU_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'FeishuRemote' }
$VenvPython = Join-Path $Data 'venv\Scripts\python.exe'
$VenvPythonw = Join-Path $Data 'venv\Scripts\pythonw.exe'

function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

function Stop-Listener {
    # Every listener process is started as <data>\venv\Scripts\pythonw.exe ...; the venv's
    # pythonw is only a redirector whose child runs the real interpreter (a different
    # ExecutablePath) with the same command line, so match the command line.
    $venv = (Join-Path $Data 'venv').ToLower() + '\'
    $procs = @(Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($venv) })
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $procs | ForEach-Object { Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }
}

Step '检查 Python'
# The hooks run "python" from PATH. Windows ships a stub of that name that only opens the
# Store, so ask for the version instead of trusting Get-Command.
$python = Get-Command python -ErrorAction SilentlyContinue
$version = if ($python) { & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
if (-not $python -or $LASTEXITCODE -ne 0 -or -not $version) {
    Fail '没有找到可用的 Python。请从 https://www.python.org 安装 Python 3.10 或更新版本，安装时勾选 "Add python.exe to PATH"，然后重新打开 PowerShell 运行本脚本。'
}
if ([version]$version -lt [version]'3.10') { Fail "Python 版本是 $version，需要 3.10 或更新版本。" }
Write-Host "Python $version：$($python.Source)"

Step "准备数据目录 $Data"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
if (-not (Test-Path $VenvPython)) {
    Write-Host '创建 listener 用的 Python 环境 ...'
    & python -m venv (Join-Path $Data 'venv')
    if ($LASTEXITCODE -ne 0) { Fail '创建 Python 环境失败。' }
}
Write-Host '安装或更新 lark-oapi ...'
& $VenvPython -m pip install --disable-pip-version-check -q -r (Join-Path $PluginRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'lark-oapi 安装失败（检查网络或 pip 源）。' }
# The first import of lark-oapi's ~10,000 fresh files took 85 s on a test machine
# (antivirus scanning them); later ones take seconds. Pay it here, not in the listener.
Write-Host '预加载 lark-oapi（第一次可能要一两分钟）...'
& $VenvPython -c "import lark_oapi"
if ($LASTEXITCODE -ne 0) { Fail 'lark-oapi 导入失败。' }
Copy-Item (Join-Path $Scripts 'launch_listener.py') (Join-Path $Data 'launch_listener.py') -Force
& python (Join-Path $Scripts 'setup_helper.py') record-root  # spelled like the session-start hook writes it
if ($LASTEXITCODE -ne 0) { Fail "无法写入 $Data\plugin_root.txt。" }

Step '飞书应用凭据'
& python (Join-Path $Scripts 'setup_helper.py') has-credentials
if ($Reconfigure -or $LASTEXITCODE -ne 0) {
    Write-Host '在飞书开放平台 → 你的应用 → 凭证与基础信息 里复制。'
    $appId = Read-Host 'App ID'
    $secure = Read-Host 'App Secret（输入时不显示）' -AsSecureString
    $env:FEISHU_SETUP_APP_ID = $appId
    $env:FEISHU_SETUP_APP_SECRET = [System.Net.NetworkCredential]::new('', $secure).Password
    try {
        & python (Join-Path $Scripts 'setup_helper.py') save-credentials
        $saved = $LASTEXITCODE
    } finally {
        Remove-Item Env:FEISHU_SETUP_APP_ID, Env:FEISHU_SETUP_APP_SECRET -ErrorAction SilentlyContinue
    }
    if ($saved -ne 0) { Fail '凭据没有保存，请检查后重新运行。' }
} else {
    Write-Host '已配置（要重新输入请加 -Reconfigure）。'
}

Step '注册并启动 listener（开机登录时自动启动，之后每 5 分钟检查一次）'
$action = New-ScheduledTaskAction -Execute $VenvPythonw -Argument ('"' + (Join-Path $Data 'launch_listener.py') + '"') -WorkingDirectory $Data
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
Stop-Listener  # a running listener keeps the old code and credentials
Start-ScheduledTask -TaskName $TaskName
Write-Host '等待 listener 启动（最多 2 分钟）...'
& python (Join-Path $Scripts 'setup_helper.py') wait-listener 120
if ($LASTEXITCODE -ne 0) { Fail "listener 2 分钟内没有启动，请查看 $Data\listener.log 和 listener-supervisor.log。" }
Write-Host 'listener 已启动。'

Step '飞书后台：事件与回调'
Write-Host '飞书后台保存“长连接”订阅时要求本机 listener 在线，现在正是时候。若还没配置，请按 README「4. 运行安装脚本」第 5 条：'
Write-Host '  事件配置：使用长连接接收事件，添加 im.message.receive_v1'
Write-Host '  回调配置：使用长连接接收回调，添加 card.action.trigger'
Write-Host '  然后创建版本并发布（改过权限或订阅都要重新发布）。'
Read-Host '完成后按回车继续（已经配置过就直接按回车）' | Out-Null

Step '绑定你的飞书账号'
$bindArgs = @((Join-Path $Scripts 'feishu_bind.py'))
if ($Rebind) { $bindArgs += '--reset' }
& python @bindArgs
if ($LASTEXITCODE -ne 0) { Fail '绑定没有完成，可以稍后重新运行 setup.ps1。' }

Step '完成'
Write-Host '如果还没装插件，在终端运行（第一条二选一）：'
Write-Host '  claude plugin marketplace add zhishouhuameiwan/claude-feishu-remote'
Write-Host "  claude plugin marketplace add `"$(Split-Path (Split-Path $PluginRoot))`"     （用下载到本地的副本）"
Write-Host '  claude plugin install feishu-remote@claude-feishu-remote'
Write-Host '然后新开一个 Claude Code 会话即可生效（绑定前打开的会话也要重开）。'
