# pumpfade 실거래 봇 자동 시작 (윈도우 작업 스케줄러)
# - 컴퓨터가 켜지면 로그인하지 않아도 봇이 시작돼요. 봇이 꺼지면 30초 뒤 다시 켜요.
# - 로그는 data\pumpfade-live.log 에 쌓여요.
# - 저장소 폴더에서 "관리자 권한" PowerShell 로 실행:
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install_autostart.ps1
# - 봇 멈추기 (자동 시작은 남겨둠, 다시 켜기는 Start-ScheduledTask pumpfade-live):
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install_autostart.ps1 -Stop
# - 자동 시작 지우기:
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install_autostart.ps1 -Remove
param([switch]$Remove, [switch]$Stop)
$ErrorActionPreference = "Stop"
$TaskName = "pumpfade-live"

function Stop-Bot {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*pumpfade*live*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

if ($Stop) {
    Stop-Bot
    Write-Host "봇을 멈췄어요. 다시 켜기: Start-ScheduledTask $TaskName"
    exit 0
}
if ($Remove) {
    Stop-Bot
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "자동 시작을 지우고 봇을 멈췄어요."
    exit 0
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not (Test-Path (Join-Path $repo ".env"))) { throw ".env 가 없어요. $repo\.env 에 키를 먼저 넣으세요" }
$python = (& py -3 -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $python)) { throw "파이썬을 찾지 못했어요 (py -3)" }

$data = Join-Path $repo "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null
$runner = Join-Path $data "run_live.cmd"
$lines = @(
    "@echo off",
    "cd /d `"$repo`"",
    "set PYTHONUTF8=1",
    ":loop",
    "`"$python`" -m pumpfade --log-file data\pumpfade-live.log live --i-understand-live",
    "ping -n 31 127.0.0.1 >nul",
    "goto loop"
)
Set-Content -Path $runner -Value $lines -Encoding Oem

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "자동 시작을 등록하고 봇을 켰어요 (작업 이름: $TaskName)."
Write-Host "로그 보기:   Get-Content data\pumpfade-live.log -Tail 30 -Wait"
Write-Host "상태 보기:   py -m pumpfade status --live"
Write-Host "멈추기:      powershell -ExecutionPolicy Bypass -File deploy\windows\install_autostart.ps1 -Stop"
