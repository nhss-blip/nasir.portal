@echo off
cd /d "%~dp0"
python -m waitress --listen=0.0.0.0:8080 app:app
pause
