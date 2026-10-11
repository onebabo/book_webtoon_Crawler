@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "REPO=D:\workspaces\github\onebabo\book_webtoon_Crawler"
set "DEVWORK=%REPO%\Dev_work"
set "SECRETS=%TEMP%\webtoon_crawler_secrets_backup"
cd /d "%DEVWORK%"

echo === git config (user) ===
git config --global user.name "onebabo"
git config --global user.email "jhkang@baeoom.com"
git config --global core.quotepath false

echo === git status (before) ===
git -C "%REPO%" status

echo.
echo === git pull origin main ===
git -C "%REPO%" pull origin main
if errorlevel 1 goto :SERVER_WINS

echo.
echo === git status (after) ===
git -C "%REPO%" status
cd /d "%DEVWORK%"
echo.
echo 완료: git pull origin main
echo cwd: %CD%
exit /b 0

:SERVER_WINS
echo.
echo === pull 충돌/실패 → 서버(origin/main) 기준으로 강제 동기화 ===
echo 규칙: 서버 자료가 최신(정상). 로컬 미커밋 변경은 버리고 원격과 일치시킴.
echo 비밀파일(config.ini, settings.json, sftp.json)은 백업 후 복원.

if not exist "%SECRETS%" mkdir "%SECRETS%"
if exist "%REPO%\config.ini" copy /y "%REPO%\config.ini" "%SECRETS%\config.ini" >nul
if exist "%REPO%\.vscode\settings.json" copy /y "%REPO%\.vscode\settings.json" "%SECRETS%\settings.json" >nul
if exist "%REPO%\.vscode\sftp.json" copy /y "%REPO%\.vscode\sftp.json" "%SECRETS%\sftp.json" >nul

git -C "%REPO%" fetch origin
if errorlevel 1 (
  echo fetch 실패
  cd /d "%DEVWORK%"
  exit /b 1
)

git -C "%REPO%" checkout main
git -C "%REPO%" reset --hard origin/main
if errorlevel 1 (
  echo reset --hard 실패
  cd /d "%DEVWORK%"
  exit /b 1
)

git -C "%REPO%" clean -fd

if exist "%SECRETS%\config.ini" copy /y "%SECRETS%\config.ini" "%REPO%\config.ini" >nul
if exist "%SECRETS%\settings.json" copy /y "%SECRETS%\settings.json" "%REPO%\.vscode\settings.json" >nul
if exist "%SECRETS%\sftp.json" copy /y "%SECRETS%\sftp.json" "%REPO%\.vscode\sftp.json" >nul

echo.
echo === git status (after server-sync) ===
git -C "%REPO%" status

cd /d "%DEVWORK%"
echo.
echo 완료: 서버(origin/main) 기준 강제 동기화
echo cwd: %CD%
exit /b 0
