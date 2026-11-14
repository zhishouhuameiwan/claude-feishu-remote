# 飞书远程回复 Claude Code：卸载
# 用法：powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-KeepData]
#   默认会删除数据目录（飞书凭据、绑定信息、日志、listener 的 Python 环境）；
#   加 -KeepData 则保留，以后重新安装不用再填凭据和绑定。
# 插件本身用 Claude Code 卸载：
#   claude plugin uninstall feishu-remote@claude-feishu-remote
#   claude plugin marketplace remove claude-feishu-remote

param([switch]$KeepData)

$ErrorActionPreference = 'Stop'
$TaskName = 'Feishu Remote Listener'
$Data = if ($env:FEISHU_REMOTE_HOME) { $env:FEISHU_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'FeishuRemote' }

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务 $TaskName"
}

# Match the command line: the venv's pythonw is a redirector whose child (the real
# interpreter, another ExecutablePath) carries the same command line.
$venv = (Join-Path $Data 'venv').ToLower() + '\'
$procs = @(Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($venv) })
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "已结束 listener 进程 $($p.ProcessId)"
}
foreach ($p in $procs) { Wait-Process -Id $p.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }

if ($KeepData) {
    Write-Host "数据目录已保留：$Data"
} elseif (Test-Path $Data) {
    try {
        Remove-Item -Recurse -Force $Data
        Write-Host "已删除数据目录：$Data"
    } catch {
        Write-Host "数据目录有文件仍被占用，没删干净：$Data。稍后手动删除即可。" -ForegroundColor Yellow
    }
}
Write-Host '最后在终端运行下面两条命令卸载插件本身：'
Write-Host '  claude plugin uninstall feishu-remote@claude-feishu-remote'
Write-Host '  claude plugin marketplace remove claude-feishu-remote'
