# git_webtoon_push.ps1
# 로컬 변경분 전체 add → commit(날짜/시분초) → push
# 콘솔 작업 폴더는 Dev_work 유지

$ErrorActionPreference = "Stop"

$repoRoot = "D:\workspaces\github\onebabo\book_webtoon_Crawler"
$devWork = Join-Path $repoRoot "Dev_work"
Set-Location $devWork

Write-Host "=== git config (user) ===" -ForegroundColor Cyan
git config --global user.name "onebabo"
git config --global user.email "jhkang@baeoom.com"
git config --global core.quotepath false

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$commitMsg = "설명 추가 $stamp"

Write-Host "=== git status (before) ===" -ForegroundColor Cyan
git -C $repoRoot status

Write-Host "`n=== git add -A ===" -ForegroundColor Cyan
git -C $repoRoot add -A

Write-Host "`n=== git status (staged) ===" -ForegroundColor Cyan
git -C $repoRoot status

Write-Host "`n=== git commit ===" -ForegroundColor Cyan
Write-Host "message: $commitMsg"
git -C $repoRoot commit -m $commitMsg
if ($LASTEXITCODE -ne 0) {
    Write-Host "커밋할 변경이 없거나 커밋에 실패했습니다. push를 건너뜁니다." -ForegroundColor Yellow
    Set-Location $devWork
    exit $LASTEXITCODE
}

Write-Host "`n=== git push ===" -ForegroundColor Cyan
git -C $repoRoot push
if ($LASTEXITCODE -ne 0) {
    Write-Host "push 실패" -ForegroundColor Red
    Set-Location $devWork
    exit $LASTEXITCODE
}

Set-Location $devWork
Write-Host "`n완료: $commitMsg" -ForegroundColor Green
Write-Host "cwd: $(Get-Location)" -ForegroundColor DarkGray
