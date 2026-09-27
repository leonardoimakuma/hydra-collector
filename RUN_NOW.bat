@echo off
REM Triggers both collector workflows now and logs the result to run_now_log.txt
cd /d "%~dp0"
echo [%date% %time%] trigger > run_now_log.txt
gh workflow run collect.yml -R leonardoimakuma/hydra-collector >> run_now_log.txt 2>&1
gh workflow run daily.yml -R leonardoimakuma/hydra-collector >> run_now_log.txt 2>&1
timeout /t 20 /nobreak >nul
gh run list -R leonardoimakuma/hydra-collector --limit 6 >> run_now_log.txt 2>&1
echo [%date% %time%] done >> run_now_log.txt
timeout /t 10 >nul
