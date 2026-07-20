# watch_dashboard.ps1
# Background watcher: regenerates reports\_snapshot.json + the HTML dashboards
# every N seconds. Designed to run from a separate window, or as a scheduled
# task. Writes its PID to reports\_watch.pid so it can be stopped cleanly.
#
# Usage (from PowerShell, in any directory):
#   powershell -ExecutionPolicy Bypass -File F:\Wallpapers\reports\watch_dashboard.ps1 `
#              -IntervalSeconds 120 `
#              -Once              # run once and exit (good for Task Scheduler)
#
# Stop:
#   powershell -ExecutionPolicy Bypass -File F:\Wallpapers\reports\watch_dashboard.ps1 -Stop

param(
    [int]$IntervalSeconds = 120,
    [int]$JitterSeconds = 15,
    [switch]$Once,
    [switch]$Stop,
    [string]$ReportsDir = "F:\Wallpapers\reports",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

if ($Stop) {
    $pidFile = Join-Path $ReportsDir "_watch.pid"
    if (Test-Path $pidFile) {
        $other = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($other -and (Get-Process -Id $other -ErrorAction SilentlyContinue)) {
            Write-Host "Stopping watch_dashboard pid=$other"
            Stop-Process -Id $other -Force
            Remove-Item $pidFile -Force
            exit 0
        } else {
            Write-Host "Stale pid file, removing"
            Remove-Item $pidFile -Force
            exit 0
        }
    }
    Write-Host "No pid file at $pidFile - nothing to stop"
    exit 0
}

$pidFile = Join-Path $ReportsDir "_watch.pid"
Set-Content -Path $pidFile -Value $PID -Encoding ASCII

if (-not $LogFile) {
    $LogFile = Join-Path $ReportsDir "_watch.log"
}

function Write-Log($msg) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$stamp] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Run-Build {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $py) {
        Write-Log "ERROR: python not found in PATH"
        return $false
    }
    $build = Join-Path $ReportsDir "_build_dashboard.py"
    $render = Join-Path $ReportsDir "_render_dashboard.py"
    try {
        Write-Log "running _build_dashboard.py"
        & $py $build 2>&1 | ForEach-Object { Write-Log "  build: $_" } | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Log "build failed (exit=$LASTEXITCODE)"; return $false }
        Write-Log "running _render_dashboard.py"
        & $py $render 2>&1 | ForEach-Object { Write-Log "  render: $_" } | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Log "render failed (exit=$LASTEXITCODE)"; return $false }
        Write-Log "ok - snapshot + dashboard regenerated"
        return $true
    } catch {
        Write-Log "exception: $_"
        return $false
    }
}

# Check we are the only watcher
$existing = $null
if (Test-Path $pidFile) {
    $existing = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($existing -and ($existing -ne $PID) -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
        Write-Host "Another watch_dashboard is already running (pid=$existing). Use -Stop first." -ForegroundColor Yellow
        exit 1
    }
}

Write-Log "watch_dashboard starting pid=$PID interval=${IntervalSeconds}s once=$Once"
Write-Log "  reports: $ReportsDir"
Write-Log "  log:     $LogFile"

# Graceful Ctrl-C cleanup
$stop = $false
[Console]::TreatControlCAsInput = $false
$handler = [ConsoleCancelEventHandler]{
    param($s, $e)
    $e.Cancel = $true
    $script:stop = $true
    Write-Log "Ctrl-C received, will exit after this cycle"
}
[Console]::add_CancelKeyPress($handler)

if ($Once) {
    Run-Build | Out-Null
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

# Initial run so the dashboard is fresh right after start
Run-Build | Out-Null

while (-not $stop) {
    $jitter = Get-Random -Minimum 0 -Maximum ([Math]::Max(1, $JitterSeconds))
    $sleep = $IntervalSeconds + $jitter
    Write-Log "sleeping ${sleep}s (base ${IntervalSeconds}s + jitter ${jitter}s)"
    $slept = 0
    while ($slept -lt $sleep -and -not $stop) {
        Start-Sleep -Seconds 1
        $slept++
    }
    if ($stop) { break }
    Run-Build | Out-Null
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Write-Log "watch_dashboard exiting"
exit 0
