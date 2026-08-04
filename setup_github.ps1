[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding            = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$GH_USER = "jinhae8971"
$GH_REPO = "ai-credit-radar"

if ($env:GH_TOKEN) {
    $GH_TOKEN = $env:GH_TOKEN
} else {
    $sec = Read-Host "GitHub PAT (repo + workflow 스코프)" -AsSecureString
    $GH_TOKEN = [System.Net.NetworkCredential]::new("", $sec).Password
}
if (-not $GH_TOKEN) { Write-Host "토큰 필요" -ForegroundColor Red; exit 1 }

$REMOTE_URL = "https://$GH_TOKEN@github.com/$GH_USER/$GH_REPO.git"
$API_HDR    = @{
    "Authorization" = "token $GH_TOKEN"
    "Accept"        = "application/vnd.github+json"
    "User-Agent"    = "GitHubActionsDeploy"
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# [1] Git
git config --global --add safe.directory ($ScriptDir -replace '\\','/') 2>$null
if (-not (Test-Path ".git")) { git init | Out-Null }
$prev = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
git remote remove origin 2>$null | Out-Null
$ErrorActionPreference = $prev
git remote add origin $REMOTE_URL
git config user.name $GH_USER; git config user.email "jinhae8971@gmail.com"
Write-Host "[1] Git OK" -ForegroundColor Green

# [2] 레포 생성 — GitHub Pages를 쓰므로 public
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO" -Headers $API_HDR | Out-Null
    Write-Host "[2] Repo exists" -ForegroundColor Green
} catch {
    try {
        Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" -Headers $API_HDR `
            -Body (@{name=$GH_REPO;private=$false;auto_init=$false} | ConvertTo-Json) `
            -ContentType "application/json" | Out-Null
        Write-Host "[2] Repo created (public)" -ForegroundColor Green; Start-Sleep -Seconds 2
    } catch {
        Write-Host "[2] 수동 생성: https://github.com/new (name: $GH_REPO, Public)" -ForegroundColor Red
        Read-Host "생성 후 Enter"
    }
}

# [3] Commit & Push
$ErrorActionPreference = "SilentlyContinue"
git add .; git commit -m "feat: AI credit radar initial deploy" 2>$null
if ($LASTEXITCODE -ne 0) { git commit --allow-empty -m "chore: update" 2>$null }
git branch -M main; git push -u origin main --force 2>$null
$pushCode = $LASTEXITCODE; $ErrorActionPreference = "Stop"
if ($pushCode -ne 0) {
    Write-Host "PUSH FAILED. 토큰에 repo + workflow 스코프 필요" -ForegroundColor Red
    exit 1
}
Write-Host "[3] Push OK" -ForegroundColor Green

# [4] Secrets
$secretNames = @("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
if (Get-Command gh -ErrorAction SilentlyContinue) {
    $env:GH_TOKEN = $GH_TOKEN
    foreach ($n in $secretNames) {
        $sv = Read-Host "$n 값 입력" -AsSecureString
        $plain = [System.Net.NetworkCredential]::new("", $sv).Password
        if ($plain) { gh secret set $n --body $plain --repo "$GH_USER/$GH_REPO" 2>$null }
    }
    Write-Host "[4] Secrets set" -ForegroundColor Green
} else {
    Write-Host "[4] 아래에서 직접 등록하세요:" -ForegroundColor Yellow
    Write-Host "  https://github.com/$GH_USER/$GH_REPO/settings/secrets/actions" -ForegroundColor White
    foreach ($n in $secretNames) { Write-Host "  - $n" -ForegroundColor Cyan }
    Read-Host "등록 후 Enter"
}

# [5] Pages 활성화 (main / docs)
try {
    Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO/pages" `
        -Headers $API_HDR -ContentType "application/json" `
        -Body (@{source=@{branch="main";path="/docs"}} | ConvertTo-Json -Depth 3) | Out-Null
    Write-Host "[5] Pages 활성화됨" -ForegroundColor Green
} catch {
    Write-Host "[5] 수동 설정: Settings > Pages > main / (docs)" -ForegroundColor Yellow
}

# [6] 워크플로우 즉시 실행
try {
    Invoke-RestMethod -Method Post `
        -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO/actions/workflows/daily.yml/dispatches" `
        -Headers $API_HDR -Body '{"ref":"main"}' -ContentType "application/json" | Out-Null
    Write-Host "[6] Triggered — 3분 후 확인" -ForegroundColor Green
} catch {
    Write-Host "[6] 수동: https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White
}

# [7] 토큰 흔적 제거
git remote set-url origin "https://github.com/$GH_USER/$GH_REPO.git"
Remove-Variable GH_TOKEN -ErrorAction SilentlyContinue
$env:GH_TOKEN = $null
Write-Host "DONE — https://$GH_USER.github.io/$GH_REPO" -ForegroundColor Cyan
