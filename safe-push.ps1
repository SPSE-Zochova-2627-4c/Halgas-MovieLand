<#
.SYNOPSIS
  Safety-first Git commit & push helper for this project.

.DESCRIPTION
  Checks for likely secrets, blocks pushes if found, stages only non-blacklisted files,
  commits and pushes to the chosen branch.

.PARAMETER Branch
  The branch to push to. Default 'main'.

.PARAMETER Remote
  The git remote to push to. Default 'origin'.
#>

param(
    [string]$Branch = "main",
    [string]$Remote = "origin"
)

function Abort($msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

function Confirm-OrAbort($msg) {
    $answer = Read-Host "$msg [Y/n]"
    if ($answer -and $answer.Trim().ToLower().StartsWith('n')) {
        Abort "Operation cancelled by user."
    }
}

# ensure we're in a git repo
if (-not (Test-Path ".git")) {
    Abort "This does not appear to be the root of a git repository. cd to repo root and retry."
}

Write-Host "Running safety checks..." -ForegroundColor Cyan

# 1) quick grep for suspicious strings in working tree (tracked & untracked files)
$dangerPatterns = @("TMDB_API_KEY", "API_KEY")
$foundMatches = @()

# use git grep where possible (searches tracked files)
foreach ($pat in $dangerPatterns) {
    try {
        $res = git grep -n -- $pat 2>$null
        if ($res) { $foundMatches += $res -split "`n" }
    } catch { }
}

# also search for hex-like tokens (common key shape, may produce false positives)
try {
    $hexRes = git grep -n -E "[0-9a-fA-F]{32}" 2>$null
    if ($hexRes) { $foundMatches += $hexRes -split "`n" }
} catch { }

# fallback: if git grep returned nothing, do a filesystem scan for untracked files (Select-String)
if ($foundMatches.Count -eq 0) {
    try {
        $ss = Select-String -Path * -Pattern "TMDB_API_KEY|API_KEY" -SimpleMatch -ErrorAction SilentlyContinue
        if ($ss) { $foundMatches += $ss | ForEach-Object { \"{0}:{1}:{2}\" -f $_.Path,$_.LineNumber,$_.Line.Trim() } }
    } catch { }
    try {
        $ss2 = Select-String -Path * -Pattern "[0-9a-fA-F]{32}" -AllMatches -ErrorAction SilentlyContinue
        if ($ss2) { $foundMatches += $ss2 | ForEach-Object { \"{0}:{1}:{2}\" -f $_.Path,$_.LineNumber,$_.Line.Trim() } }
    } catch { }
}

if ($foundMatches.Count -gt 0) {
    Write-Host ""
    Write-Host "Potential secret matches found in repository files:" -ForegroundColor Yellow
    $foundMatches | Select-Object -Unique | ForEach-Object { Write-Host $_ }
    Write-Host ""
    Abort "Aborting: please remove/separate any secrets (move to .env/api.env), rotate keys if needed, and ensure they are not tracked before pushing."
}

# 2) ensure api.env and .env are NOT tracked by git
$trackedApi = (git ls-files --error-unmatch [api.env](http://_vscodecontentref_/1) 2>$null) ; $trackedApiExit = $LASTEXITCODE
if ($trackedApiExit -eq 0) {
    Abort "api.env is tracked by git. Remove it from tracking: `git rm --cached api.env` then commit and retry."
}
$trackedDotEnv = (git ls-files --error-unmatch .env 2>$null) ; $trackedDotEnvExit = $LASTEXITCODE
if ($trackedDotEnvExit -eq 0) {
    Abort ".env is tracked by git. Remove it from tracking: `git rm --cached .env` then commit and retry."
}

# 3) determine changed files (staged or unstaged)
$porcelain = git status --porcelain 2>$null
if (-not $porcelain) {
    Write-Host "No local changes detected. Nothing to commit." -ForegroundColor Green
    exit 0
}

$changedFiles = @()
$porcelain -split "`n" | ForEach-Object {
    $line = $_.Trim()
    if ($line.Length -lt 4) { return }
    # path starts at index 3 in porcelain output
    $path = $line.Substring(3).Trim()
    # remove leading and trailing quotes if any
    if ($path.StartsWith('"') -and $path.EndsWith('"')) {
        $path = $path.Trim('"')
    }
    if ($path) { $changedFiles += $path }
}

if ($changedFiles.Count -eq 0) {
    Write-Host "No changed paths detected." -ForegroundColor Green
    exit 0
}

Write-Host "Changed files found:" -ForegroundColor Cyan
$changedFiles | ForEach-Object { Write-Host " - $_" }

# 4) filter out blacklisted files (do NOT stage these)
$blacklistPatterns = @(
    'api.env',
    '.env',
    'movies.db',
    'instance/',
    '__pycache__',
    '*.pyc',
    '*.sqlite',
    '*.db',
    '.vscode/',
    '.env.local'
)

function IsBlacklisted($path) {
    # normalize slashes
    $p = $path -replace '\\','/'
    foreach ($pat in $blacklistPatterns) {
        if ($pat.EndsWith('/') -and $p -like "$pat*") { return $true }
        if ($pat -like '*.*' -and $p -like $pat) { return $true }
        if ($p -eq $pat) { return $true }
        # contains match (covers most cases)
        if ($p -like "*$pat*") { return $true }
    }
    return $false
}

$toAdd = @()
$skipped = @()
foreach ($f in $changedFiles) {
    if (IsBlacklisted $f) {
        $skipped += $f
    } else {
        $toAdd += $f
    }
}

if ($toAdd.Count -eq 0) {
    Write-Host ""
    Write-Host "No safe files to stage after filtering blacklisted paths." -ForegroundColor Yellow
    if ($skipped.Count -gt 0) {
        Write-Host "Blacklisted/Skipped files:" -ForegroundColor Yellow
        $skipped | ForEach-Object { Write-Host " - $_" }
    }
    Abort "Nothing staged; fix tracked sensitive files and run the script again."
}

Write-Host ""
Write-Host "Files that will be staged:" -ForegroundColor Green
$toAdd | ForEach-Object { Write-Host " - $_" }

if ($skipped.Count -gt 0) {
    Write-Host ""
    Write-Host "Files that will be SKIPPED (not staged):" -ForegroundColor Yellow
    $skipped | ForEach-Object { Write-Host " - $_" }
}

Confirm-OrAbort "Proceed to stage and commit the above files?"

# 5) stage only the safe files
Write-Host "`nStaging files..." -ForegroundColor Cyan
# call git add with the array of paths
& git add -- $toAdd
if ($LASTEXITCODE -ne 0) { Abort "git add failed." }

# 6) commit
$defaultMsg = "Security: load TMDB API key from env; add .gitignore and .env.example; README_SECURITY"
$commitMsg = Read-Host "Commit message (leave empty for default)" 
if (-not $commitMsg) { $commitMsg = $defaultMsg }

Write-Host "Committing..." -ForegroundColor Cyan
git commit -m "$commitMsg"
if ($LASTEXITCODE -ne 0) {
    Abort "git commit failed. Possibly nothing to commit (staged files unchanged)."
}

# 7) push
Confirm-OrAbort "Push commit to $Remote/$Branch now?"
Write-Host "Pushing..." -ForegroundColor Cyan
git push $Remote $Branch
if ($LASTEXITCODE -ne 0) {
    Abort "git push failed. Check your remote/credentials and try again."
}

Write-Host "`nDone. Changes pushed to $Remote/$Branch." -ForegroundColor Green
exit 0