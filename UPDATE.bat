@echo off
REM Pushes the latest collector changes (lineup capture) to the public hydra-collector repo. Log: update_log.txt
setlocal EnableExtensions
cd /d "%~dp0"
set "LOG=%~dp0update_log.txt"
set "GIT=git"
where git >nul 2>&1 || set "GIT=%ProgramFiles%\Git\cmd\git.exe"
echo [%date% %time%] START > "%LOG%"
"%GIT%" add -A >> "%LOG%" 2>&1
"%GIT%" -c user.name="Hydra collector" -c user.email="hydra-collector@users.noreply.github.com" commit -m "collector: lineup capture + robustness fixes" >> "%LOG%" 2>&1
"%GIT%" pull --rebase --autostash origin main >> "%LOG%" 2>&1
"%GIT%" push origin main >> "%LOG%" 2>&1
"%GIT%" log --oneline -3 >> "%LOG%" 2>&1
echo [%date% %time%] DONE >> "%LOG%"
timeout /t 15 >nul
endlocal
