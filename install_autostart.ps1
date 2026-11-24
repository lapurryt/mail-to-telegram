# Регистрирует задачу в Планировщике Windows: бот стартует при входе в систему, без окна консоли.
# Запуск: powershell -ExecutionPolicy Bypass -File install_autostart.ps1
# Удалить: Unregister-ScheduledTask -TaskName "Mail to Telegram" -Confirm:$false

$ErrorActionPreference = "Stop"
$TaskName = "Mail to Telegram"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Dir "mail_to_telegram.py"

# pythonw.exe = тот же Python, но без чёрного окна консоли
$Python = (Get-Command python -ErrorAction Stop).Source
$PythonW = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
if (-not (Test-Path $PythonW)) { throw "Не найден $PythonW" }

$Action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Script`"" -WorkingDirectory $Dir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Задержка 30 с после входа, чтобы успела подняться сеть (скрипт и так переживает обрывы).
$Trigger.Delay = "PT30S"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal `
    -Description "Пересылает уведомления о новых письмах в Telegram ($Dir)" -Force | Out-Null

Write-Host "Задача '$TaskName' создана: $PythonW `"$Script`""
Write-Host "Запустить сейчас:  Start-ScheduledTask -TaskName '$TaskName'"
