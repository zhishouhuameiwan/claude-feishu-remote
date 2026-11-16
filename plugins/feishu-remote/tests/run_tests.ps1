# Offline tests for the Feishu remote-reply plugin. Nothing is sent to Feishu and
# nothing real is touched: every suite works in its own temp data folder
# (see _testenv.py), so a running listener and real requests are left alone.
# ask / permission / reply import the listener and need lark-oapi: they use
# $env:FEISHU_TEST_PYTHON, or else the listener's venv that setup.ps1 created.
# content and sessions run with the system python, as the real hooks do.
# Usage: powershell -ExecutionPolicy Bypass -File run_tests.ps1   (about 6 minutes; exit code = failed suites)

$env:PYTHONIOENCODING = 'utf-8'
$here = $PSScriptRoot
$data = if ($env:FEISHU_REMOTE_HOME) { $env:FEISHU_REMOTE_HOME } else { Join-Path $env:LOCALAPPDATA 'FeishuRemote' }
$venv = if ($env:FEISHU_TEST_PYTHON) { $env:FEISHU_TEST_PYTHON } else { Join-Path $data 'venv\Scripts\python.exe' }
if (-not (Test-Path $venv)) { Write-Host "No python with lark-oapi at $venv; run setup.ps1 or set FEISHU_TEST_PYTHON."; exit 4 }
$suites = @(
    @($venv, 'test_feishu_ask.py'),
    @($venv, 'test_feishu_permission.py'),
    @($venv, 'test_feishu_reply.py'),
    @('python', 'test_feishu_content.py'),
    @('python', 'test_feishu_sessions.py')
)
$failed = 0
foreach ($suite in $suites) {
    $out = & $suite[0] (Join-Path $here $suite[1]) 2>&1 | Out-String
    $code = $LASTEXITCODE
    $lines = $out -split "`r?`n"
    $pass = @($lines | Where-Object { $_ -match '^PASS ' }).Count
    $fail = @($lines | Where-Object { $_ -match '^FAIL ' })
    '{0}: exit={1} pass={2} fail={3}' -f $suite[1], $code, $pass, $fail.Count
    $fail
    if ($code -ne 0) {
        $failed++
        if ($fail.Count -eq 0) { $lines | Select-Object -Last 15 }  # crashed: show the traceback
    }
}
exit $failed
