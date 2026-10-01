@echo off
chcp 936 >nul
REM Fetch the current UTF-8 PowerShell installer. No printer credentials in this file.
powershell.exe -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $r = Invoke-WebRequest -UseBasicParsing 'https://www.bnbscheduler.top/print-setup/uic-print.ps1'; Invoke-Expression $r.Content } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
  echo 配置未完成，请打开 https://www.bnbscheduler.top/print/
  pause
  exit /b 1
)
pause
