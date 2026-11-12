# minuta installer for Windows.
#
#   irm https://raw.githubusercontent.com/damianpeaf/minuta/main/install.ps1 | iex
#   .\install.ps1 --non-interactive --name Ana          # arguments go to `minuta setup`
#
# Environment:
#   MINUTA_REPO_URL  git URL or local folder to install from (default: the GitHub repository)
#   MINUTA_REF       branch or tag (default: the repository's default branch); `minuta update` keeps it
#   MINUTA_APP_DIR   where the source checkout lives (default: %LOCALAPPDATA%\minuta\app)
#   MINUTA_EXTRAS    package extras (default: "local", plus "cuda" when an NVIDIA GPU is present; "" for none)
#   MINUTA_PYTHON    Python version for the tool environment (default: 3.12)
#   MINUTA_NO_SETUP  set to 1 to install without running `minuta setup`
$ErrorActionPreference = "Stop"

function Say($msg) { Write-Host "minuta: $msg" }

$RepoUrl = if ($env:MINUTA_REPO_URL) { $env:MINUTA_REPO_URL } else { "https://github.com/damianpeaf/minuta" }
$Ref = $env:MINUTA_REF
$PythonVersion = if ($env:MINUTA_PYTHON) { $env:MINUTA_PYTHON } else { "3.12" }
$AppDir = if ($env:MINUTA_APP_DIR) { $env:MINUTA_APP_DIR } else { Join-Path $env:LOCALAPPDATA "minuta\app" }
$Extras = if (Test-Path Env:MINUTA_EXTRAS) { $env:MINUTA_EXTRAS }
          elseif (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { "local,cuda" }
          else { "local" }

# Remove the app folder only if it is empty or this installer made it (it leaves a marker file).
function Clear-AppDir {
    if (Test-Path $AppDir) {
        $empty = -not (Get-ChildItem -Force $AppDir | Select-Object -First 1)
        if (-not $empty -and -not (Test-Path (Join-Path $AppDir ".minuta-installer"))) {
            throw "$AppDir exists and was not created by this installer; move it away or set MINUTA_APP_DIR"
        }
        Remove-Item -Recurse -Force $AppDir
    }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Say "ffmpeg is not installed. minuta needs it to save audio: winget install ffmpeg"
}

# 1. uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $local = Join-Path $HOME ".local\bin"
    if (-not (Test-Path (Join-Path $local "uv.exe"))) {
        Say "installing uv"
        powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    }
    $env:Path = "$local;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv was not found after installing it" }

# 2. source
New-Item -ItemType Directory -Force -Path (Split-Path $AppDir) | Out-Null
if (Get-Command git -ErrorAction SilentlyContinue) {
    if ((Test-Path (Join-Path $AppDir ".git")) -and (Test-Path (Join-Path $AppDir ".minuta-installer"))) {
        Say "updating $AppDir"
        git -C $AppDir remote set-url origin $RepoUrl
        git -C $AppDir fetch --depth 1 origin $(if ($Ref) { $Ref } else { "HEAD" })
        git -C $AppDir reset --hard FETCH_HEAD
    } else {
        Say "downloading into $AppDir"
        Clear-AppDir
        if ($Ref) { git clone --depth 1 --branch $Ref $RepoUrl $AppDir } else { git clone --depth 1 $RepoUrl $AppDir }
    }
    if ($LASTEXITCODE -ne 0) { throw "git failed" }
} else {
    Say "git not found; downloading an archive into $AppDir"
    Clear-AppDir
    if (Test-Path $RepoUrl -PathType Container) {
        Copy-Item -Recurse -Path $RepoUrl -Destination $AppDir
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $AppDir ".git"), (Join-Path $AppDir ".venv")
    } else {
        $branch = if ($Ref) { $Ref } else { "main" }
        $zip = Join-Path $env:TEMP "minuta-src.zip"
        $tmp = Join-Path $env:TEMP "minuta-src"
        # archive/<ref> takes a branch or a tag.
        Invoke-WebRequest -Uri "$($RepoUrl.TrimEnd('/'))/archive/$branch.zip" -OutFile $zip
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
        Expand-Archive -Path $zip -DestinationPath $tmp
        Move-Item -Path (Get-ChildItem $tmp | Select-Object -First 1).FullName -Destination $AppDir
        Remove-Item -Force $zip
        Remove-Item -Recurse -Force $tmp
    }
}
New-Item -ItemType File -Force -Path (Join-Path $AppDir ".minuta-installer") | Out-Null

# 3. the tool, with the versions pinned in uv.lock (exported to constraints.txt)
$Spec = if ($Extras) { "$AppDir[$Extras]" } else { $AppDir }
Say "installing the minuta command (extras: $(if ($Extras) { $Extras } else { 'none' }))"
$Constraints = Join-Path $AppDir "constraints.txt"
if (Test-Path $Constraints) {
    uv tool install --force --reinstall --python $PythonVersion --constraints $Constraints $Spec
} else {  # a release from before constraints.txt
    uv tool install --force --reinstall --python $PythonVersion $Spec
}
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
$BinDir = (uv tool dir --bin).Trim()
$Minuta = Join-Path $BinDir "minuta.exe"
if (-not (Test-Path $Minuta)) { throw "minuta was not installed in $BinDir" }
if (-not ($env:Path -split ";" | Where-Object { $_ -eq $BinDir })) {
    Say "add $BinDir to your PATH (or run: uv tool update-shell)"
}

# 4. setup
if ($env:MINUTA_NO_SETUP -eq "1") {
    Say "installed. Run: $Minuta setup"
    exit 0
}
& $Minuta setup --app-dir $AppDir "--install-extras=$Extras" "--install-ref=$Ref" @args
exit $LASTEXITCODE
