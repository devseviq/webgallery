# Background watcher for the generated operations snapshot and dashboard.
# Source scripts always come from this repository; runtime roots are explicit.

[CmdletBinding()]
param(
    [int]$IntervalSeconds = 120,
    [int]$JitterSeconds = 15,
    [switch]$Once,
    [switch]$Stop,
    [string]$SourceReportsDir = $PSScriptRoot,
    [string]$CollectionRoot = "",
    [string]$QueueStatePath = "",
    [string]$LibraryRoot = "",
    [string]$PreviewRoot = "",
    [string]$ReportOutputRoot = "",
    [string]$PauseFlagPath = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

function Require-PathArgument([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "-$Name is required. Runtime roots are never inferred from the repository."
    }
}

Require-PathArgument -Name "ReportOutputRoot" -Value $ReportOutputRoot
$pidFile = Join-Path $ReportOutputRoot "_watch.pid"

if ($Stop) {
    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Host "No pid file at $pidFile - nothing to stop"
        exit 0
    }

    $other = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    $otherPid = 0
    if ([int]::TryParse([string]$other, [ref]$otherPid) -and
        (Get-Process -Id $otherPid -ErrorAction SilentlyContinue)) {
        Write-Host "Stopping watch_dashboard pid=$otherPid"
        Stop-Process -Id $otherPid -Force
    } else {
        Write-Host "Stale pid file, removing"
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

foreach ($required in @(
    @{ Name = "CollectionRoot"; Value = $CollectionRoot },
    @{ Name = "QueueStatePath"; Value = $QueueStatePath },
    @{ Name = "LibraryRoot"; Value = $LibraryRoot },
    @{ Name = "PreviewRoot"; Value = $PreviewRoot },
    @{ Name = "PauseFlagPath"; Value = $PauseFlagPath }
)) {
    Require-PathArgument -Name $required.Name -Value $required.Value
}

$build = Join-Path $SourceReportsDir "_build_dashboard.py"
$render = Join-Path $SourceReportsDir "_render_dashboard.py"
if (-not (Test-Path -LiteralPath $build -PathType Leaf)) {
    throw "In-repository builder not found: $build"
}
if (-not (Test-Path -LiteralPath $render -PathType Leaf)) {
    throw "In-repository renderer not found: $render"
}

New-Item -ItemType Directory -Path $ReportOutputRoot -Force | Out-Null

# Check the prior owner before publishing this process ID. The former ordering
# overwrote the old PID first, which made duplicate-instance detection inert.
if (Test-Path -LiteralPath $pidFile) {
    $existing = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    $existingPid = 0
    if ([int]::TryParse([string]$existing, [ref]$existingPid) -and
        $existingPid -ne $PID -and
        (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        Write-Host "Another watch_dashboard is already running (pid=$existingPid). Use -Stop first." -ForegroundColor Yellow
        exit 1
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII

if (-not $LogFile) {
    $LogFile = Join-Path $ReportOutputRoot "_watch.log"
}

function Write-Log([string]$Message) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$stamp] $Message"
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Run-Build {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) {
        $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source
    }
    if (-not $py) {
        Write-Log "ERROR: python not found in PATH"
        return $false
    }

    $snapshotPath = Join-Path $ReportOutputRoot "_snapshot.json"
    $dashboardPath = Join-Path $ReportOutputRoot "download-queue-dashboard.html"
    $buildArgs = @(
        $build,
        "--collection-root", $CollectionRoot,
        "--queue-state-path", $QueueStatePath,
        "--library-root", $LibraryRoot,
        "--preview-root", $PreviewRoot,
        "--report-output-root", $ReportOutputRoot,
        "--pause-flag-path", $PauseFlagPath
    )
    $renderArgs = @(
        $render,
        "--snapshot-path", $snapshotPath,
        "--output-path", $dashboardPath
    )

    try {
        Write-Log "running in-repository _build_dashboard.py"
        & $py @buildArgs 2>&1 | ForEach-Object { Write-Log "  build: $_" } | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Log "build failed (exit=$LASTEXITCODE)"
            return $false
        }

        Write-Log "running in-repository _render_dashboard.py"
        & $py @renderArgs 2>&1 | ForEach-Object { Write-Log "  render: $_" } | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Log "render failed (exit=$LASTEXITCODE)"
            return $false
        }
        Write-Log "ok - snapshot + dashboard regenerated"
        return $true
    } catch {
        Write-Log "exception: $_"
        return $false
    }
}

Write-Log "watch_dashboard starting pid=$PID interval=${IntervalSeconds}s once=$Once"
Write-Log "  source:   $SourceReportsDir"
Write-Log "  reports:  $ReportOutputRoot"
Write-Log "  log:      $LogFile"

$stopRequested = $false
$cancelHandler = [ConsoleCancelEventHandler]{
    param($sender, $eventArgs)
    $eventArgs.Cancel = $true
    $script:stopRequested = $true
    Write-Log "Ctrl-C received, will exit after this cycle"
}
[Console]::add_CancelKeyPress($cancelHandler)

try {
    if ($Once) {
        if (Run-Build) { exit 0 }
        exit 1
    }

    Run-Build | Out-Null
    while (-not $stopRequested) {
        $jitter = Get-Random -Minimum 0 -Maximum ([Math]::Max(1, $JitterSeconds))
        $sleep = $IntervalSeconds + $jitter
        Write-Log "sleeping ${sleep}s (base ${IntervalSeconds}s + jitter ${jitter}s)"
        $slept = 0
        while ($slept -lt $sleep -and -not $stopRequested) {
            Start-Sleep -Seconds 1
            $slept++
        }
        if ($stopRequested) { break }
        Run-Build | Out-Null
    }
} finally {
    [Console]::remove_CancelKeyPress($cancelHandler)
    $publishedPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ([string]$publishedPid -eq [string]$PID) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Log "watch_dashboard exiting"
exit 0
