# Hydra collector - unattended setup. Launched by START.bat. Log: start_log.txt (same folder).
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot "start_log.txt"
function Log($m) { $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m; Write-Host $line; Add-Content -Path $log -Value $line }
"" | Set-Content -Path $log
Log "START"

# 1. git
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
  foreach ($p in @("$env:ProgramFiles\Git\cmd\git.exe", "${env:ProgramFiles(x86)}\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")) { if (Test-Path $p) { $git = $p; break } }
}
if (-not $git) { Log "ERROR: git not found"; exit 1 }
Log "git: $git"

# 2. gh (portable, no installer / no admin prompt)
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) {
  $toolDir = Join-Path $PSScriptRoot ".tools\gh"
  $gh = Get-ChildItem -Path $toolDir -Recurse -Filter gh.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
  if (-not $gh) {
    Log "Downloading portable GitHub CLI..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/cli/cli/releases/latest" -Headers @{ "User-Agent" = "hydra-collector" }
    $asset = $rel.assets | Where-Object { $_.name -like "*windows_amd64.zip" } | Select-Object -First 1
    $zip = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
    New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $toolDir -Force
    $gh = Get-ChildItem -Path $toolDir -Recurse -Filter gh.exe | Select-Object -First 1 -ExpandProperty FullName
  }
}
if (-not $gh) { Log "ERROR: could not get gh"; exit 1 }
Log "gh: $gh"

# 3. login (device code; approve at https://github.com/login/device from any device)
& $gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Log "LOGIN NEEDED - the one-time code is printed below. Approve at https://github.com/login/device"
  "" | & $gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key 2>&1 | ForEach-Object { Log "gh: $_" }
}
& $gh auth status 2>&1 | ForEach-Object { Log "auth: $_" }
if ($LASTEXITCODE -ne 0) { Log "ERROR: not logged in"; exit 1 }
& $gh auth setup-git 2>&1 | ForEach-Object { Log "setup-git: $_" }
$user = (& $gh api user --jq .login).Trim()
Log "github user: $user"

# 4. local repo
if (-not (Test-Path ".git")) {
  & $git init -b main 2>&1 | ForEach-Object { Log "git: $_" }
  & $git add -A 2>&1 | Out-Null
  & $git -c user.name="Hydra collector" -c user.email="hydra-collector@users.noreply.github.com" commit -m "Hydra collector: free live odds + stats logger on GitHub Actions" 2>&1 | ForEach-Object { Log "git: $_" }
}

# 5. create public repo + push
& $gh repo view "$user/hydra-collector" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  & $gh repo create hydra-collector --public --description "Free live football odds and match-stats collector (GitHub Actions)" --source . --remote origin --push 2>&1 | ForEach-Object { Log "create: $_" }
} else {
  & $git remote remove origin 2>$null
  & $git remote add origin "https://github.com/$user/hydra-collector.git"
  & $git push -u origin main 2>&1 | ForEach-Object { Log "push: $_" }
}

# 6. first runs
Start-Sleep -Seconds 20
& $gh workflow run collect.yml -R "$user/hydra-collector" 2>&1 | ForEach-Object { Log "run collect: $_" }
& $gh workflow run daily.yml -R "$user/hydra-collector" 2>&1 | ForEach-Object { Log "run daily: $_" }
Start-Sleep -Seconds 10
& $gh run list -R "$user/hydra-collector" --limit 5 2>&1 | ForEach-Object { Log "runs: $_" }
Log "DONE https://github.com/$user/hydra-collector/actions"
