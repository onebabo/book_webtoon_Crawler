@echo off
chcp 65001 >nul
set "REPO=D:\workspaces\github\onebabo\book_webtoon_Crawler"
set "DEVWORK=%REPO%\Dev_work"
cd /d "%DEVWORK%"

echo === git config (user) ===
git config --global user.name "onebabo"
git config --global user.email "jhkang@baeoom.com"
git config --global core.quotepath false

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "STAMP=%%I"
set "MSG=설명 추가 %STAMP%"

echo === git status (before) ===
git -C "%REPO%" status

echo.
echo === git add -A ===
git -C "%REPO%" add -A

echo.
echo === git status (staged) ===
git -C "%REPO%" status

echo.
echo === git commit ===
echo message: %MSG%
git -C "%REPO%" commit -m "%MSG%"
if errorlevel 1 (
  echo 커밋할 변경이 없거나 커밋에 실패했습니다. push를 건너뜁니다.
  cd /d "%DEVWORK%"
  exit /b %errorlevel%
)

echo.
echo === git push ===
git -C "%REPO%" push
if errorlevel 1 (
  echo push 실패
  cd /d "%DEVWORK%"
  exit /b %errorlevel%
)

cd /d "%DEVWORK%"
echo.
echo 완료: %MSG%
echo cwd: %CD%
