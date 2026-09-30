@echo off
cd /d %~dp0
:loop
python agent.py
echo Agent stopped. Restarting in 5 seconds... (close this window to stop)
timeout /t 5 >nul
goto loop
