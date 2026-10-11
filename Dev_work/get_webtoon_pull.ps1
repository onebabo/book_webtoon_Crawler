# get_webtoon_pull.ps1
# 원격(main) 변경분을 로컬로 일괄 pull
# pull 충돌/실패 시: 서버(origin/main)가 최신(정상) → 로컬 변경 버리고 강제 맞춤
# 콘솔 작업 폴더는 Dev_work 유지
# 비밀파일(config.ini, settings.json, sftp.json)은 백업 후 복원

$ErrorActionPreference = "Continue"

$repoRoot = "D:\workspaces\github\onebabo\book_webtoon_Crawler"
$devWork = Join-Path $repoRoot "Dev_work"
$secrets = Join-Path $env:TEMP "webtoon_crawler_secrets_backup"
Set-Location $devWork

function Backup-Secrets {
    if (-not (Test-Path $secrets)) {
        New-Item -ItemType Directory -Path $secrets | Out-Null
    }
    $pairs = @(
        @{ Src = (Join-Path $repoRoot "config.ini"); Dst = (Join-Path $secrets "config.ini") },
        @{ Src = (Join-Path $repoRoot ".vscode\settings.json"); Dst = (Join-Path $secrets "settings.json") },
        @{ Src = (Join-Path $repoRoot ".vscode\sftp.json"); Dst = (Join-Path $secrets "sftp.json") }
    )
    foreach ($p in $pairs) {
        if (Test-Path $p.Src) {
            Copy-Item -Force $p.Src $p.Dst
        }
    }
}

function Restore-Secrets {
    $pairs = @(
        @{ Src = (Join-Path $secrets "config.ini"); Dst = (Join-Path $repoRoot "config.ini") },
        @{ Src = (Join-Path $secrets "settings.json"); Dst = (Join-Path $repoRoot ".vscode\settings.json") },
        @{ Src = (Join-Path $secrets "sftp.json"); Dst = (Join-Path $repoRoot ".vscode\sftp.json") }
    )
    foreach ($p in $pairs) {
        if (Test-Path $p.Src) {
            $dir = Split-Path $p.Dst -Parent
            if (-not (Test-Path $dir)) {
                New-Item -ItemType Directory -Path $dir | Out-Null
            }
            Copy-Item -Force $p.Src $p.Dst
        }
    }
}

function Sync-FromServer {
    Write-Host ""
    Write-Host "=== pull 충돌/실패 → 서버(origin/main) 기준으로 강제 동기화 ===" -ForegroundColor Yellow
    Write-Host "규칙: 서버 자료가 최신(정상). 로컬 미커밋 변경은 버리고 원격과 일치시킴." -ForegroundColor Yellow
    Write-Host "비밀파일(config.ini, settings.json, sftp.json)은 백업 후 복원." -ForegroundColor DarkYellow

    Backup-Secrets

    git -C $repoRoot fetch origin
    if ($LASTEXITCODE -ne 0) {
        Write-Host "fetch 실패" -ForegroundColor Red
        Set-Location $devWork
        exit $LASTEXITCODE
    }

    git -C $repoRoot checkout main
    git -C $repoRoot reset --hard origin/main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "reset --hard 실패" -ForegroundColor Red
        Set-Location $devWork
        exit $LASTEXITCODE
    }

    git -C $repoRoot clean -fd
    Restore-Secrets

    Write-Host "`n=== git status (after server-sync) ===" -ForegroundColor Cyan
    git -C $repoRoot status

    Set-Location $devWork
    Write-Host "`n완료: 서버(origin/main) 기준 강제 동기화" -ForegroundColor Green
    Write-Host "cwd: $(Get-Location)" -ForegroundColor DarkGray
}

Write-Host "=== git config (user) ===" -ForegroundColor Cyan
git config --global user.name "onebabo"
git config --global user.email "jhkang@baeoom.com"
git config --global core.quotepath false

Write-Host "=== git status (before) ===" -ForegroundColor Cyan
git -C $repoRoot status

Write-Host "`n=== git pull origin main ===" -ForegroundColor Cyan
git -C $repoRoot pull origin main
if ($LASTEXITCODE -ne 0) {
    Sync-FromServer
    exit 0
}

Write-Host "`n=== git status (after) ===" -ForegroundColor Cyan
git -C $repoRoot status

Set-Location $devWork
Write-Host "`n완료: git pull origin main" -ForegroundColor Green
Write-Host "cwd: $(Get-Location)" -ForegroundColor DarkGray
