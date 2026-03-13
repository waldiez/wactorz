@echo off
:: AgentFlow — Windows launcher (batch wrapper)
:: Double-click this file to start AgentFlow.

:: If start.ps1 is next to this file, use it (recommended)
if exist "%~dp0start.ps1" (
    powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
    goto :end
)

:: Fallback: start.ps1 not found — run PowerShell inline
powershell -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "function Is-Repo($p){ return ($p) -and (Test-Path \"$p\.git\") -and (Test-Path \"$p\pyproject.toml\") };" ^
  "$cwd=(Get-Location).Path; $default='C:\waldiez\agentflow';" ^
  "if(Is-Repo $cwd){ $r=$cwd; Set-Location $r; git fetch; git pull }" ^
  "elseif(Is-Repo $default){ $r=$default; Set-Location $r; git fetch; git pull }" ^
  "else{ if(-not(Test-Path 'C:\waldiez')){ New-Item -ItemType Directory 'C:\waldiez'|Out-Null }; Set-Location 'C:\waldiez'; git clone https://github.com/waldiez/agentflow; $r=$default; Set-Location $r };" ^
  "$env=$r+'\.env'; $ex=$r+'\.env.example';" ^
  "if(Test-Path $env){ $c=Get-Content $env -Raw; if($c -notmatch 'LLM_API_KEY\s*=\s*\S+'){ Write-Host 'WARNING: LLM_API_KEY missing in .env — opening file...'; Start-Process notepad $env -Wait } }" ^
  "elseif(Test-Path $ex){ Copy-Item $ex $env; Write-Host '.env created — opening for you to fill in your API key...'; Start-Process notepad $env -Wait };" ^
  "try{ $s=docker inspect --format '{{.State.Status}}' mosquitto 2>$null } catch { $s='missing' };" ^
  "if($LASTEXITCODE -ne 0){ $s='missing' };" ^
  "$conf=$r+'\infra\mosquitto\mosquitto.conf';" ^
  "if($s -eq 'running'){ Write-Host 'mosquitto already running' }" ^
  "elseif($s -eq 'missing'){ docker run -d --name mosquitto --restart unless-stopped -p 1883:1883 -p 9001:9001 -v \"${conf}:/mosquitto/config/mosquitto.conf\" eclipse-mosquitto:2.0 }" ^
  "else{ docker start mosquitto };" ^
  "pip install --force-reinstall -e '.[all]';" ^
  "agentflow"

:end
pause
