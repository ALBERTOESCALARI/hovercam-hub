# HoverCam hub installer for Windows.
#
# Open PowerShell as Administrator and run:
#   irm https://raw.githubusercontent.com/ALBERTOESCALARI/hovercam-hub/main/install-hub.ps1 | iex
#
# Installs Git, Python, go2rtc and ffmpeg, puts the hub in C:\HoverCam, starts it now and at
# every boot (Windows task, runs in the background without anyone signed in), then shows the
# pairing code to enter in the HoverCam app. The hub keeps itself up to date from GitHub.
# Safe to run again: it repairs/updates an existing install instead of making a second hub.
#
# Options (set before running): $env:HOVERCAM_DIR, $env:HOVERCAM_API_URL

function Install-HoverCamHub {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'          # Invoke-WebRequest is very slow with it on
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $Dir = if ($env:HOVERCAM_DIR) { $env:HOVERCAM_DIR } else { 'C:\HoverCam' }
    $Api = if ($env:HOVERCAM_API_URL) { $env:HOVERCAM_API_URL } else { 'https://hovercam-api.hoverlayer.com' }
    $Repo = 'https://github.com/ALBERTOESCALARI/hovercam-hub.git'
    $Go2rtcUrl = 'https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_win64.zip'
    $FfmpegUrls = @('https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
                    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip')
    $Task = 'HoverCam Hub'

    function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
    function Refresh-Path {
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
    }

    # --- checks -------------------------------------------------------------------------
    $me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Please run this in PowerShell opened with "Run as administrator".' -ForegroundColor Red
        return
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host 'This needs "App Installer" (winget) from the Microsoft Store. Install it and run this again.' -ForegroundColor Red
        return
    }
    $existing = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    if ($existing) {
        $existingDir = Split-Path -Parent ($existing.Actions[0].Execute.Trim('"'))
        if ($existingDir -and ($existingDir.TrimEnd('\') -ne $Dir.TrimEnd('\'))) {
            Write-Host "A HoverCam hub is already installed in $existingDir and updates itself." -ForegroundColor Yellow
            Write-Host 'Nothing to do. (One hub per home is enough.)'
            return
        }
    }

    # --- tools ----------------------------------------------------------------------------
    Step 'Installing Git and Python (skipped if already installed)'
    Refresh-Path
    if (-not (Get-Command git -ErrorAction SilentlyContinue) -and -not (Test-Path "$env:ProgramFiles\Git\cmd\git.exe")) {
        winget install --id Git.Git -e --silent --scope machine --accept-package-agreements --accept-source-agreements | Out-Host
    }
    $python = "$env:ProgramFiles\Python312\python.exe"
    if (-not (Test-Path $python)) {
        winget install --id Python.Python.3.12 -e --silent --scope machine --accept-package-agreements --accept-source-agreements | Out-Host
    }
    Refresh-Path
    $git = if (Test-Path "$env:ProgramFiles\Git\cmd\git.exe") { "$env:ProgramFiles\Git\cmd\git.exe" } else { (Get-Command git).Source }
    if (-not (Test-Path $python)) { throw "Python 3.12 did not install (expected $python)." }

    # --- hub code -------------------------------------------------------------------------
    Step "Getting the HoverCam hub into $Dir"
    if (Test-Path "$Dir\.git") {
        & $git -c safe.directory=* -C $Dir fetch --quiet origin main
        & $git -c safe.directory=* -C $Dir reset --quiet --hard origin/main
    } elseif ((Test-Path $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
        throw "$Dir already exists and is not a HoverCam install. Move it away or set `$env:HOVERCAM_DIR."
    } else {
        & $git clone --quiet $Repo $Dir
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not download the hub code from GitHub.' }

    # --- go2rtc and ffmpeg --------------------------------------------------------------------
    $tmp = Join-Path $env:TEMP ('hovercam-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        if (-not (Test-Path "$Dir\go2rtc.exe")) {
            Step 'Downloading go2rtc (video relay)'
            Invoke-WebRequest -UseBasicParsing $Go2rtcUrl -OutFile "$tmp\go2rtc.zip"
            Expand-Archive "$tmp\go2rtc.zip" -DestinationPath "$tmp\go2rtc"
            Copy-Item (Get-ChildItem "$tmp\go2rtc" -Recurse -Filter go2rtc*.exe | Select-Object -First 1).FullName "$Dir\go2rtc.exe"
        }
        if (-not (Test-Path "$Dir\ffmpeg\bin\ffmpeg.exe")) {
            Step 'Downloading ffmpeg (snapshots and recordings, ~100 MB)'
            $got = $false
            foreach ($url in $FfmpegUrls) {
                try { Invoke-WebRequest -UseBasicParsing $url -OutFile "$tmp\ffmpeg.zip"; $got = $true; break }
                catch { Write-Host "  (download from $url failed, trying another source)" }
            }
            if (-not $got) { throw 'Could not download ffmpeg.' }
            Expand-Archive "$tmp\ffmpeg.zip" -DestinationPath "$tmp\ffmpeg"
            New-Item -ItemType Directory -Force -Path "$Dir\ffmpeg\bin" | Out-Null
            Get-ChildItem "$tmp\ffmpeg" -Recurse -Include ffmpeg.exe, ffprobe.exe |
                ForEach-Object { Copy-Item $_.FullName "$Dir\ffmpeg\bin\" }
        }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }

    # --- python environment -------------------------------------------------------------------
    Step 'Setting up the hub (Python packages)'
    if (-not (Test-Path "$Dir\.venv\Scripts\python.exe")) { & $python -m venv "$Dir\.venv" }
    & "$Dir\.venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e $Dir
    if ($LASTEXITCODE -ne 0) { throw 'Installing the hub packages failed.' }

    # --- firewall (home network only) ---------------------------------------------------------
    Step 'Allowing video through Windows Firewall on your home network'
    foreach ($rule in @(
            @{ Name = 'HoverCam go2rtc'; Program = "$Dir\go2rtc.exe" },
            @{ Name = 'HoverCam hub'; Program = $python })) {
        if (-not (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow `
                -Program $rule.Program -Profile Private, Domain | Out-Null
        }
    }

    # --- start now and at every boot ----------------------------------------------------------
    Step 'Starting the hub (and at every boot)'
    [Environment]::SetEnvironmentVariable('HOVERCAM_API_URL', $Api, 'Machine')
    schtasks /End /TN $Task 2>$null | Out-Null
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*-m hub*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    schtasks /Create /TN $Task /TR "`"$Dir\start-hub.bat`"" /SC ONSTART /DELAY 0001:00 /RU SYSTEM /RL HIGHEST /F | Out-Null
    schtasks /Run /TN $Task | Out-Null

    # --- pairing code ---------------------------------------------------------------------------
    Step 'Waiting for the hub to get its pairing code'
    $stateFile = "$Dir\state-prod\hub_state.json"
    $code = $null
    for ($i = 0; $i -lt 90 -and -not $code; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Path $stateFile) {
            try {
                $state = Get-Content $stateFile -Raw | ConvertFrom-Json
                if ($state.paired) { $code = 'PAIRED' } elseif ($state.pairing_code) { $code = $state.pairing_code }
            } catch { }
        }
    }
    Write-Host ''
    if ($code -eq 'PAIRED') {
        Write-Host 'The hub is running and already paired to a HoverCam account.' -ForegroundColor Green
    } elseif ($code) {
        Write-Host 'HoverCam hub is running. In the HoverCam app tap  Add hub  and enter:' -ForegroundColor Green
        Write-Host ''
        Write-Host "        $code" -ForegroundColor Yellow
        Write-Host ''
        Write-Host 'The code renews every 15 minutes; the current one is always at http://localhost:8099'
        Start-Process 'http://localhost:8099'
    } else {
        Write-Host "The hub is starting but has no pairing code yet. Check $Dir\hub.log, or open http://localhost:8099 in a minute." -ForegroundColor Yellow
    }
}

Install-HoverCamHub
