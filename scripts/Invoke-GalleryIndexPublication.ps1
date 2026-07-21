<#
.SYNOPSIS
Preview or execute the fail-closed gallery index publication workflow.

.DESCRIPTION
This SND-HOST wrapper supplies Windows-only machine, path, task, process,
listener, hold, and handle observations to the portable Python publication
core. Preview is the default. Prepare, Publish, and Recover can write only when
-Apply is present, -WhatIf is absent, ShouldProcess approves the operation, and
every applicable live path and runtime proof passes.

The wrapper never creates or releases a maintenance hold, starts or stops a
process or listener, or changes a scheduled task. Runtime evidence is delivered
to Python over stdin; it is never staged in a temporary file.
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Inspect', 'Prepare', 'Publish', 'Recover')]
    [string]$Mode,

    [switch]$Apply,
    [switch]$CutoverAuthorized,

    [Parameter(Mandatory = $true)]
    [string]$CanonicalDatabase,
    [string]$CandidateDatabase,
    [string]$LibraryRoot,
    [string]$WallhavenLedger,
    [string]$ProviderLedger,
    [string]$SiblingDatabase,
    [string]$VerificationReportRoot,
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,
    [Parameter(Mandatory = $true)]
    [string]$BackupDirectory,
    [Parameter(Mandatory = $true)]
    [string]$RecoveryJournal,
    [string[]]$ContinuationSegments = @(),
    [Parameter(Mandatory = $true)]
    [string]$RecoveryResultRoot,
    [Parameter(Mandatory = $true)]
    [string]$QueueStatePath,
    [Parameter(Mandatory = $true)]
    [string]$HoldPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ExpectedMachineId = 'snd-host'
$script:ExpectedInstanceId = '13af5dd3-9cfe-4c8f-82ef-806f256cc1c2'
$script:ExpectedComputerName = 'SND-HOST'
$script:ExpectedQualifiedUser = 'SND-HOST\Dev'
$script:IdentityVerifier = 'C:\Users\Dev\OneDrive\common\common_dev\Get-VerifiedMachineIdentity.ps1'

$script:CanonicalProjectRoot = 'F:\Wallpapers\webgallery'
$script:CanonicalDatabase = 'F:\Wallpapers\webgallery_library.sqlite'
$script:CanonicalLibraryRoot = 'F:\Wallpapers\library'
$script:CanonicalWallhavenLedger = 'F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl'
$script:CanonicalProviderLedger = 'F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl'
$script:ProtectedSiblingDatabase = 'F:\Wallpapers\wallpaper_library.sqlite'
$script:PublicationRoot = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication'
$script:CandidateRoot = Join-Path $script:PublicationRoot 'candidates'
$script:ManifestRoot = Join-Path $script:PublicationRoot 'manifests'
$script:BackupRoot = Join-Path $script:PublicationRoot 'backups'
$script:JournalRoot = Join-Path $script:PublicationRoot 'journals'
$script:CanonicalRecoveryResultRoot = Join-Path $script:PublicationRoot 'recovery-results'
$script:CanonicalReportRoot = 'F:\Wallpapers\reports\gallery-publication'
$script:CanonicalQueueStateRoot = 'F:\Wallpapers\.wallpaper-download-queue'
$script:CanonicalHoldPath = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication-hold.json'
$script:QueueTaskPath = '\'
$script:QueueTaskName = 'Wallpaper Download Queue'
$script:MinimumObservationMilliseconds = 30000

function Get-GalleryPublicationUtcTimestamp {
    return [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
}

function ConvertTo-GalleryPublicationUtcTimestamp {
    param(
        [Parameter(Mandatory = $true)][AllowNull()][object]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Value -is [DateTime]) {
        return $Value.ToUniversalTime().ToString(
            'o',
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    if ($Value -is [DateTimeOffset]) {
        return $Value.UtcDateTime.ToString(
            'o',
            [Globalization.CultureInfo]::InvariantCulture
        )
    }
    $parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse(
        [string]$Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsed
    )) {
        throw "$Label is not a valid UTC timestamp."
    }
    return $parsed.UtcDateTime.ToString(
        'o',
        [Globalization.CultureInfo]::InvariantCulture
    )
}

function Get-GalleryPublicationProperty {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][object]$Default = $null
    )

    if ($null -eq $InputObject) {
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

function ConvertTo-GalleryPublicationAbsolutePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Label is required and may not be empty."
    }
    if ($Path.IndexOfAny([char[]]@('*', '?')) -ge 0) {
        throw "$Label must be a literal path without wildcard characters: $Path"
    }
    if ($Path -match '^[A-Za-z]+::' -or $Path -match '^[A-Za-z]+:\\' -and $Path -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be a local filesystem path, not a PowerShell provider path: $Path"
    }
    if ($Path.StartsWith('\\', [StringComparison]::Ordinal) -or
        $Path.StartsWith('//', [StringComparison]::Ordinal)) {
        throw "$Label must be machine-local, not UNC: $Path"
    }

    $full = [IO.Path]::GetFullPath($Path)
    if (-not [IO.Path]::IsPathFullyQualified($full) -or $full -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an absolute drive-qualified Windows path: $Path"
    }
    return $full.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}

function Test-GalleryPublicationPathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowEqual
    )

    $candidate = (ConvertTo-GalleryPublicationAbsolutePath -Path $Path -Label 'Candidate path')
    $container = (ConvertTo-GalleryPublicationAbsolutePath -Path $Root -Label 'Container root')
    if ($AllowEqual -and $candidate.Equals($container, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidate.StartsWith(
        $container + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-GalleryPublicationNoReparseAncestor {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = ConvertTo-GalleryPublicationAbsolutePath -Path $Path -Label $Label
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "$Label has no filesystem root: $full"
    }

    $components = [Collections.Generic.List[string]]::new()
    [void]$components.Add($root)
    $current = $root
    $relative = $full.Substring($root.Length).TrimStart('\', '/')
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { $_ })) {
        $current = Join-Path $current $segment
        [void]$components.Add($current)
    }

    foreach ($component in $components) {
        try {
            $item = Get-Item -LiteralPath $component -Force -ErrorAction Stop
        }
        catch {
            if ($_.Exception -is [Management.Automation.ItemNotFoundException] -or
                $_.Exception -is [IO.DirectoryNotFoundException] -or
                $_.Exception -is [IO.FileNotFoundException]) {
                break
            }
            throw "Could not inspect $Label path component '$component': $($_.Exception.Message)"
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse point: $component"
        }
    }
}

function Get-GalleryPublicationNearestExistingPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $candidate = ConvertTo-GalleryPublicationAbsolutePath -Path $Path -Label $Label
    while (-not (Test-Path -LiteralPath $candidate)) {
        $parent = [IO.Path]::GetDirectoryName($candidate)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $candidate) {
            throw "No existing ancestor could be established for ${Label}: $Path"
        }
        $candidate = $parent
    }
    return $candidate
}

function Get-GalleryPublicationVolumeIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($null -eq (Get-Command Get-Volume -ErrorAction SilentlyContinue)) {
        throw "Get-Volume is unavailable; exact volume identity cannot be proven for $Label."
    }
    $probe = Get-GalleryPublicationNearestExistingPath -Path $Path -Label $Label
    if (Test-Path -LiteralPath $probe -PathType Leaf) {
        $probe = Split-Path -Parent $probe
    }
    $volume = Get-Volume -FilePath $probe -ErrorAction Stop
    $uniqueId = [string](Get-GalleryPublicationProperty -InputObject $volume -Name 'UniqueId' -Default '')
    if ([string]::IsNullOrWhiteSpace($uniqueId)) {
        throw "Get-Volume returned no UniqueId for ${Label}: $probe"
    }
    return [PSCustomObject][ordered]@{
        unique_id = $uniqueId
        drive_letter = [string](Get-GalleryPublicationProperty -InputObject $volume -Name 'DriveLetter' -Default '')
        file_system = [string](Get-GalleryPublicationProperty -InputObject $volume -Name 'FileSystem' -Default '')
        path = [string](Get-GalleryPublicationProperty -InputObject $volume -Name 'Path' -Default '')
    }
}

function Get-GalleryPublicationPathEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = ConvertTo-GalleryPublicationAbsolutePath -Path $Path -Label $Label
    Assert-GalleryPublicationNoReparseAncestor -Path $full -Label $Label
    $exists = Test-Path -LiteralPath $full
    $resolved = if ($exists) {
        (Resolve-Path -LiteralPath $full -ErrorAction Stop).Path
    }
    else {
        # This is a projected final path. The nearest existing ancestor and every
        # existing component have already been checked for reparse traversal.
        $full
    }
    $pathType = if (-not $exists) {
        'absent'
    }
    elseif (Test-Path -LiteralPath $full -PathType Leaf) {
        'file'
    }
    elseif (Test-Path -LiteralPath $full -PathType Container) {
        'directory'
    }
    else {
        'other'
    }
    if ($pathType -eq 'other') {
        throw "$Label is neither a normal file nor directory: $full"
    }
    return [PSCustomObject][ordered]@{
        literal_path = $Path
        full_path = $full
        resolved_path = $resolved
        exists = [bool]$exists
        path_type = $pathType
        projected = -not $exists
        reparse_free = $true
        volume = Get-GalleryPublicationVolumeIdentity -Path $full -Label $Label
    }
}

function Get-GalleryPublicationSha256Bytes {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return (($hasher.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $hasher.Dispose()
    }
}

function Get-GalleryPublicationSha256Text {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    return Get-GalleryPublicationSha256Bytes -Bytes $bytes
}

function Get-GalleryPublicationFileEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowEmpty
    )

    $pathEvidence = Get-GalleryPublicationPathEvidence -Path $Path -Label $Label
    if (-not $pathEvidence.exists -or $pathEvidence.path_type -ne 'file') {
        throw "$Label must be an existing file: $($pathEvidence.full_path)"
    }
    $before = Get-Item -LiteralPath $pathEvidence.full_path -Force -ErrorAction Stop
    if (-not $AllowEmpty -and $before.Length -le 0) {
        throw "$Label must not be empty: $($pathEvidence.full_path)"
    }
    $stream = [IO.File]::Open(
        $pathEvidence.full_path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = (($hasher.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
    $after = Get-Item -LiteralPath $pathEvidence.full_path -Force -ErrorAction Stop
    if ($before.Length -ne $after.Length -or $before.LastWriteTimeUtc -ne $after.LastWriteTimeUtc) {
        throw "$Label changed while it was being fingerprinted: $($pathEvidence.full_path)"
    }
    return [PSCustomObject][ordered]@{
        path = $pathEvidence.full_path
        final_path = $pathEvidence.resolved_path
        exists = $true
        size_bytes = [int64]$after.Length
        sha256 = $hash
        mtime_utc = $after.LastWriteTimeUtc.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        volume_serial = [string]$pathEvidence.volume.unique_id
    }
}

function Test-GalleryPublicationExclusiveRead {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $true
    }
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::None
        )
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Assert-GalleryPublicationModeArguments {
    param([bool]$ApplyRequested)

    $fullOnly = @(
        'CandidateDatabase', 'LibraryRoot', 'WallhavenLedger', 'ProviderLedger',
        'SiblingDatabase', 'VerificationReportRoot'
    )
    if ($Mode -eq 'Recover') {
        foreach ($name in $fullOnly) {
            if ($PSBoundParameters.ContainsKey($name) -or
                -not [string]::IsNullOrWhiteSpace([string](Get-Variable -Name $name -ValueOnly))) {
                throw "-$name is not accepted in Recover mode. Recovery is backward-only and uses its journal/backup anchors."
            }
        }
    }
    else {
        if ($ContinuationSegments.Count -ne 0) {
            throw '-ContinuationSegments is accepted only in Recover mode.'
        }
        foreach ($name in $fullOnly) {
            $value = [string](Get-Variable -Name $name -ValueOnly)
            if ([string]::IsNullOrWhiteSpace($value)) {
                throw "-$name is required in $Mode mode."
            }
        }
    }

    if ($Mode -eq 'Inspect' -and $Apply) {
        throw '-Apply is invalid in Inspect mode.'
    }
    if ($Mode -ne 'Publish' -and $CutoverAuthorized) {
        throw '-CutoverAuthorized is accepted only in Publish mode.'
    }
    if ($Mode -eq 'Publish' -and $ApplyRequested -and -not $CutoverAuthorized) {
        throw 'Publish -Apply requires the separate -CutoverAuthorized flag.'
    }
}

function Assert-GalleryPublicationExactPath {
    param(
        [Parameter(Mandatory = $true)][string]$Actual,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $actualFull = ConvertTo-GalleryPublicationAbsolutePath -Path $Actual -Label $Label
    $expectedFull = ConvertTo-GalleryPublicationAbsolutePath -Path $Expected -Label "Expected $Label"
    if (-not $actualFull.Equals($expectedFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is not the pinned SND-HOST path. Expected '$expectedFull'; received '$actualFull'."
    }
}

function Assert-GalleryPublicationDirectChild {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = ConvertTo-GalleryPublicationAbsolutePath -Path $Path -Label $Label
    $rootFull = ConvertTo-GalleryPublicationAbsolutePath -Path $Root -Label "$Label root"
    $parent = [IO.Path]::GetDirectoryName($full)
    if ([string]::IsNullOrWhiteSpace($parent) -or
        -not $parent.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be one direct, unique child of '$rootFull': $full"
    }
}

function Assert-GalleryPublicationNewPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (Test-Path -LiteralPath $Path) {
        throw "$Label must be collision-free and not already exist: $Path"
    }
}

function Assert-GalleryPublicationExistingFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label must be an existing file: $Path"
    }
}

function Assert-GalleryPublicationExistingDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label must be an existing directory: $Path"
    }
}

function Assert-GalleryPublicationApplyPaths {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][hashtable]$PathEvidence
    )

    Assert-GalleryPublicationExactPath -Actual $ProjectRoot -Expected $script:CanonicalProjectRoot -Label 'ProjectRoot'
    Assert-GalleryPublicationExactPath -Actual $CanonicalDatabase -Expected $script:CanonicalDatabase -Label 'CanonicalDatabase'
    Assert-GalleryPublicationExactPath -Actual $RecoveryResultRoot -Expected $script:CanonicalRecoveryResultRoot -Label 'RecoveryResultRoot'
    Assert-GalleryPublicationExactPath -Actual $QueueStatePath -Expected $script:CanonicalQueueStateRoot -Label 'QueueStatePath'
    Assert-GalleryPublicationExactPath -Actual $HoldPath -Expected $script:CanonicalHoldPath -Label 'HoldPath'

    Assert-GalleryPublicationDirectChild -Path $BackupDirectory -Root $script:BackupRoot -Label 'BackupDirectory'
    Assert-GalleryPublicationDirectChild -Path $RecoveryJournal -Root $script:JournalRoot -Label 'RecoveryJournal'
    Assert-GalleryPublicationDirectChild -Path $ManifestPath -Root $script:ManifestRoot -Label 'ManifestPath'
    for ($index = 0; $index -lt $ContinuationSegments.Count; $index++) {
        $segment = [string]$ContinuationSegments[$index]
        Assert-GalleryPublicationDirectChild `
            -Path $segment `
            -Root $script:JournalRoot `
            -Label "ContinuationSegments[$index]"
        Assert-GalleryPublicationExistingFile `
            -Path $segment `
            -Label "ContinuationSegments[$index]"
    }

    if ($Mode -ne 'Recover') {
        Assert-GalleryPublicationExactPath -Actual $LibraryRoot -Expected $script:CanonicalLibraryRoot -Label 'LibraryRoot'
        Assert-GalleryPublicationExactPath -Actual $WallhavenLedger -Expected $script:CanonicalWallhavenLedger -Label 'WallhavenLedger'
        Assert-GalleryPublicationExactPath -Actual $ProviderLedger -Expected $script:CanonicalProviderLedger -Label 'ProviderLedger'
        Assert-GalleryPublicationExactPath -Actual $SiblingDatabase -Expected $script:ProtectedSiblingDatabase -Label 'SiblingDatabase'
        Assert-GalleryPublicationExactPath -Actual $VerificationReportRoot -Expected $script:CanonicalReportRoot -Label 'VerificationReportRoot'
        Assert-GalleryPublicationDirectChild -Path $CandidateDatabase -Root $script:CandidateRoot -Label 'CandidateDatabase'
        Assert-GalleryPublicationExistingDirectory -Path $LibraryRoot -Label 'LibraryRoot'
        Assert-GalleryPublicationExistingFile -Path $WallhavenLedger -Label 'WallhavenLedger'
        Assert-GalleryPublicationExistingFile -Path $ProviderLedger -Label 'ProviderLedger'
        Assert-GalleryPublicationExistingFile -Path $SiblingDatabase -Label 'SiblingDatabase'
    }

    switch ($Mode) {
        'Prepare' {
            Assert-GalleryPublicationExistingFile -Path $CanonicalDatabase -Label 'CanonicalDatabase'
            Assert-GalleryPublicationNewPath -Path $CandidateDatabase -Label 'CandidateDatabase'
            Assert-GalleryPublicationNewPath -Path $ManifestPath -Label 'ManifestPath'
            Assert-GalleryPublicationNewPath -Path $BackupDirectory -Label 'BackupDirectory'
            Assert-GalleryPublicationNewPath -Path $RecoveryJournal -Label 'RecoveryJournal'
        }
        'Publish' {
            Assert-GalleryPublicationExistingFile -Path $CanonicalDatabase -Label 'CanonicalDatabase'
            Assert-GalleryPublicationExistingFile -Path $CandidateDatabase -Label 'CandidateDatabase'
            Assert-GalleryPublicationExistingFile -Path $ManifestPath -Label 'ManifestPath'
            Assert-GalleryPublicationNewPath -Path $BackupDirectory -Label 'BackupDirectory'
            Assert-GalleryPublicationNewPath -Path $RecoveryJournal -Label 'RecoveryJournal'
        }
        'Recover' {
            Assert-GalleryPublicationExistingFile -Path $CanonicalDatabase -Label 'CanonicalDatabase'
            Assert-GalleryPublicationExistingFile -Path $ManifestPath -Label 'ManifestPath'
            if ((Test-Path -LiteralPath $BackupDirectory) -and
                -not (Test-Path -LiteralPath $BackupDirectory -PathType Container)) {
                throw "BackupDirectory exists but is not a directory: $BackupDirectory"
            }
            if ((Test-Path -LiteralPath $RecoveryJournal) -and
                -not (Test-Path -LiteralPath $RecoveryJournal -PathType Leaf)) {
                throw "RecoveryJournal exists but is not a file: $RecoveryJournal"
            }
        }
    }

    $canonicalVolume = [string]$PathEvidence.CanonicalDatabase.volume.unique_id
    foreach ($entry in $PathEvidence.GetEnumerator()) {
        if ([string]$entry.Value.volume.unique_id -ne $canonicalVolume) {
            throw "Apply requires one exact volume identity. $($entry.Key) is on '$($entry.Value.volume.unique_id)', canonical is on '$canonicalVolume'."
        }
    }

    $distinct = @{
        CanonicalDatabase = $PathEvidence.CanonicalDatabase.resolved_path
        BackupDirectory = $PathEvidence.BackupDirectory.resolved_path
        RecoveryJournal = $PathEvidence.RecoveryJournal.resolved_path
        ManifestPath = $PathEvidence.ManifestPath.resolved_path
    }
    for ($index = 0; $index -lt $ContinuationSegments.Count; $index++) {
        $distinct["ContinuationSegment$index"] = $PathEvidence["ContinuationSegment$index"].resolved_path
    }
    if ($Mode -ne 'Recover') {
        $distinct.CandidateDatabase = $PathEvidence.CandidateDatabase.resolved_path
        $distinct.SiblingDatabase = $PathEvidence.SiblingDatabase.resolved_path
    }
    $seen = @{}
    foreach ($entry in $distinct.GetEnumerator()) {
        $key = ([string]$entry.Value).ToUpperInvariant()
        if ($seen.ContainsKey($key)) {
            throw "$($entry.Key) aliases $($seen[$key]): $($entry.Value)"
        }
        $seen[$key] = $entry.Key
    }
}

function Get-GalleryPublicationVerifiedIdentity {
    if (-not (Test-Path -LiteralPath $script:IdentityVerifier -PathType Leaf)) {
        throw "Machine identity verifier is missing: $script:IdentityVerifier"
    }
    # The authoritative shared verifier intentionally lives beneath the user's
    # enrolled OneDrive common-dev tree, whose sync root may itself be a reparse
    # point. Its fixed literal path and its own registry/marker/MachineGuid
    # validation are the authority; publication data paths remain reparse-free.
    $raw = & $script:IdentityVerifier -AsJson
    try {
        $identity = ([string]($raw -join [Environment]::NewLine)) | ConvertFrom-Json
    }
    catch {
        throw "Machine identity verifier returned invalid JSON: $($_.Exception.Message)"
    }
    $expected = [ordered]@{
        status = 'VERIFIED'
        machineId = $script:ExpectedMachineId
        instanceId = $script:ExpectedInstanceId
        computerName = $script:ExpectedComputerName
        qualifiedUser = $script:ExpectedQualifiedUser
    }
    foreach ($entry in $expected.GetEnumerator()) {
        $observed = [string](Get-GalleryPublicationProperty -InputObject $identity -Name $entry.Key -Default '')
        if (-not $observed.Equals([string]$entry.Value, [StringComparison]::Ordinal)) {
            throw "Machine identity mismatch for '$($entry.Key)': expected '$($entry.Value)', observed '$observed'."
        }
    }
    $verifiedAt = ConvertTo-GalleryPublicationUtcTimestamp `
        -Value (Get-GalleryPublicationProperty -InputObject $identity -Name 'verifiedAtUtc') `
        -Label 'Machine identity verifiedAtUtc'
    return [PSCustomObject][ordered]@{
        status = 'VERIFIED'
        machine_id = [string]$identity.machineId
        instance_id = [string]$identity.instanceId
        computer_name = [string]$identity.computerName
        qualified_user = [string]$identity.qualifiedUser
        verified_at = $verifiedAt
        verifier_path = (Resolve-Path -LiteralPath $script:IdentityVerifier).Path
    }
}

function Get-GalleryPublicationProjectRuntime {
    $projectRoot = ConvertTo-GalleryPublicationAbsolutePath `
        -Path (Split-Path -Parent $PSScriptRoot) `
        -Label 'ProjectRoot'
    $sourceRoot = Join-Path $projectRoot 'src'
    $cliPath = Join-Path $projectRoot 'scripts\publish_gallery_index.py'
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
        throw "Project source root is missing: $sourceRoot"
    }
    if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
        throw "Gallery publication CLI is missing: $cliPath"
    }

    $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $sharedPython = Join-Path $script:CanonicalProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) {
        $python = $localPython
    }
    elseif (-not $projectRoot.Equals($script:CanonicalProjectRoot, [StringComparison]::OrdinalIgnoreCase) -and
        $projectRoot.StartsWith('F:\Wallpapers\wt-webgallery-', [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath (Join-Path $projectRoot '.git')) -and
        (Test-Path -LiteralPath $sharedPython -PathType Leaf)) {
        # Linked worktrees intentionally share the canonical project's package
        # environment. PYTHONPATH below still forces imports from this worktree.
        $python = $sharedPython
    }
    else {
        throw "Approved project Python was not found beneath '$projectRoot' or the linked-worktree fallback."
    }

    Assert-GalleryPublicationNoReparseAncestor -Path $python -Label 'Python executable'
    Assert-GalleryPublicationNoReparseAncestor -Path $sourceRoot -Label 'Project source root'
    Assert-GalleryPublicationNoReparseAncestor -Path $cliPath -Label 'Publication CLI'
    return [PSCustomObject][ordered]@{
        project_root = $projectRoot
        source_root = $sourceRoot
        cli_path = $cliPath
        python = (Resolve-Path -LiteralPath $python).Path
    }
}

function Invoke-GalleryPublicationPython {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [AllowNull()][string]$StandardInput
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    if ($null -eq $startInfo.PSObject.Properties['ArgumentList']) {
        throw 'The publication wrapper requires PowerShell 7/.NET ProcessStartInfo.ArgumentList.'
    }
    $startInfo.FileName = [string]$Runtime.python
    $startInfo.WorkingDirectory = [string]$Runtime.project_root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.Environment['PYTHONPATH'] = [string]$Runtime.source_root
    $startInfo.Environment['PYTHONDONTWRITEBYTECODE'] = '1'
    $startInfo.Environment['PYTHONNOUSERSITE'] = '1'
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add([string]$argument)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Could not start project Python: $($Runtime.python)"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($null -ne $StandardInput) {
            $process.StandardInput.Write($StandardInput)
        }
        $process.StandardInput.Close()
        $process.WaitForExit()
        return [PSCustomObject][ordered]@{
            exit_code = [int]$process.ExitCode
            stdout = [string]$stdoutTask.GetAwaiter().GetResult()
            stderr = [string]$stderrTask.GetAwaiter().GetResult()
        }
    }
    finally {
        $process.Dispose()
    }
}

function ConvertFrom-GalleryPublicationCliInvocation {
    param(
        [Parameter(Mandatory = $true)]$Invocation,
        [Parameter(Mandatory = $true)][string]$ExpectedMode,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $stdout = [string]$Invocation.stdout
    $stderr = [string]$Invocation.stderr
    $payload = $null
    if (-not [string]::IsNullOrWhiteSpace($stdout)) {
        try {
            $payload = $stdout.Trim() | ConvertFrom-Json -Depth 100
        }
        catch {
            throw "$Label returned invalid JSON: $($stdout.Trim())"
        }
    }
    if ($Invocation.exit_code -ne 0) {
        $detail = if ($null -ne $payload) {
            $errorDocument = Get-GalleryPublicationProperty -InputObject $payload -Name 'error'
            [string](Get-GalleryPublicationProperty -InputObject $errorDocument -Name 'message' -Default '')
        }
        else {
            ''
        }
        if ([string]::IsNullOrWhiteSpace($detail)) {
            $detail = $stderr.Trim()
        }
        throw "$Label failed with exit code $($Invocation.exit_code): $detail"
    }
    if ($null -eq $payload -or
        (Get-GalleryPublicationProperty -InputObject $payload -Name 'ok' -Default $false) -ne $true) {
        throw "$Label did not return an ok result."
    }
    $observedMode = [string](Get-GalleryPublicationProperty -InputObject $payload -Name 'mode' -Default '')
    if (-not $observedMode.Equals($ExpectedMode, [StringComparison]::Ordinal)) {
        throw "$Label returned mode '$observedMode', expected '$ExpectedMode'."
    }
    return $payload
}

function Invoke-GalleryPublicationReadOnlyInspection {
    param([Parameter(Mandatory = $true)]$Runtime)

    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($argument in @(
        '-B', [string]$Runtime.cli_path, 'inspect',
        '--canonical-database', $CanonicalDatabase,
        '--backup-directory', $BackupDirectory,
        '--recovery-journal', $RecoveryJournal,
        '--recovery-result-root', $RecoveryResultRoot,
        '--queue-state-path', $QueueStatePath,
        '--hold-path', $HoldPath,
        '--candidate-database', $CandidateDatabase,
        '--library-root', $LibraryRoot,
        '--wallhaven-ledger', $WallhavenLedger,
        '--provider-ledger', $ProviderLedger,
        '--sibling-database', $SiblingDatabase,
        '--verification-report-root', $VerificationReportRoot,
        '--manifest', $ManifestPath
    )) {
        [void]$arguments.Add([string]$argument)
    }
    $invocation = Invoke-GalleryPublicationPython `
        -Runtime $Runtime `
        -Arguments ([string[]]$arguments.ToArray()) `
        -StandardInput $null
    $payload = ConvertFrom-GalleryPublicationCliInvocation `
        -Invocation $invocation `
        -ExpectedMode 'inspect' `
        -Label 'Read-only publication inspection'
    $inspection = Get-GalleryPublicationProperty -InputObject $payload -Name 'result'
    foreach ($required in @('candidate', 'canonical', 'durable_inputs')) {
        if ($null -eq (Get-GalleryPublicationProperty -InputObject $inspection -Name $required)) {
            throw "Read-only publication inspection did not return required '$required' evidence."
        }
    }
    return $inspection
}

function Invoke-GalleryPublicationReadOnlyRecoveryInspection {
    param([Parameter(Mandatory = $true)]$Runtime)

    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($argument in @(
        '-B', [string]$Runtime.cli_path, 'recover',
        '--canonical-database', $CanonicalDatabase,
        '--backup-directory', $BackupDirectory,
        '--recovery-journal', $RecoveryJournal,
        '--recovery-result-root', $RecoveryResultRoot,
        '--queue-state-path', $QueueStatePath,
        '--hold-path', $HoldPath,
        '--manifest', $ManifestPath,
        '--runtime-evidence-stdin'
    )) {
        [void]$arguments.Add([string]$argument)
    }
    $previewEvidence = [ordered]@{
        continuation_segments = @($ContinuationSegments)
    } | ConvertTo-Json -Depth 10 -Compress
    $invocation = Invoke-GalleryPublicationPython `
        -Runtime $Runtime `
        -Arguments ([string[]]$arguments.ToArray()) `
        -StandardInput $previewEvidence
    return ConvertFrom-GalleryPublicationCliInvocation `
        -Invocation $invocation `
        -ExpectedMode 'recover' `
        -Label 'Read-only recovery inspection'
}

function Read-GalleryPublicationStrictJsonObject {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $probeCode = @'
import json
import pathlib
import sys
from dl_engine.gallery_publication import load_json_strict

value = load_json_strict(pathlib.Path(sys.argv[1]))
if not isinstance(value, dict):
    raise TypeError("document must be a JSON object")
print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
'@
    $invocation = Invoke-GalleryPublicationPython `
        -Runtime $Runtime `
        -Arguments @('-B', '-c', $probeCode, $Path) `
        -StandardInput $null
    if ($invocation.exit_code -ne 0) {
        throw "$Label is not strict JSON: $($invocation.stderr.Trim())"
    }
    try {
        return $invocation.stdout.Trim() | ConvertFrom-Json -Depth 100
    }
    catch {
        throw "$Label strict-JSON probe returned invalid JSON: $($invocation.stdout.Trim())"
    }
}

function Assert-GalleryPublicationImportOrigin {
    param([Parameter(Mandatory = $true)]$Runtime)

    $probeCode = @'
import json
import pathlib
import dl_engine
print(json.dumps({"module": str(pathlib.Path(dl_engine.__file__).resolve())}, sort_keys=True))
'@
    $probe = Invoke-GalleryPublicationPython `
        -Runtime $Runtime `
        -Arguments @('-B', '-c', $probeCode) `
        -StandardInput $null
    if ($probe.exit_code -ne 0) {
        throw "Project import probe failed: $($probe.stderr.Trim())"
    }
    try {
        $payload = $probe.stdout.Trim() | ConvertFrom-Json
    }
    catch {
        throw "Project import probe returned invalid JSON: $($probe.stdout.Trim())"
    }
    $modulePath = ConvertTo-GalleryPublicationAbsolutePath -Path ([string]$payload.module) -Label 'dl_engine import origin'
    $expectedRoot = ConvertTo-GalleryPublicationAbsolutePath `
        -Path (Join-Path $Runtime.source_root 'dl_engine') `
        -Label 'Expected dl_engine root'
    if (-not (Test-GalleryPublicationPathWithin -Path $modulePath -Root $expectedRoot -AllowEqual)) {
        throw "Project Python resolved dl_engine outside this worktree: $modulePath"
    }
    return [PSCustomObject][ordered]@{
        python = [string]$Runtime.python
        module = $modulePath
        expected_root = $expectedRoot
        isolated = $true
    }
}

function Get-GalleryPublicationQueueTaskEvidence {
    if ($null -eq (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) -or
        $null -eq (Get-Command Export-ScheduledTask -ErrorAction SilentlyContinue)) {
        throw 'ScheduledTasks cmdlets are unavailable; queue task authority cannot be proven.'
    }
    $task = Get-ScheduledTask `
        -TaskPath $script:QueueTaskPath `
        -TaskName $script:QueueTaskName `
        -ErrorAction Stop
    $xml = [string](Export-ScheduledTask `
        -TaskPath $script:QueueTaskPath `
        -TaskName $script:QueueTaskName `
        -ErrorAction Stop)
    if ([string]::IsNullOrWhiteSpace($xml)) {
        throw 'Scheduled task export returned no XML.'
    }

    $actions = @($task.Actions)
    if ($actions.Count -ne 1) {
        throw "Queue task must have exactly one action; observed $($actions.Count)."
    }
    $action = $actions[0]
    $argumentText = [string](Get-GalleryPublicationProperty -InputObject $action -Name 'Arguments' -Default '')
    $requiredActionValues = @(
        'F:\Wallpapers\dl-engine\scripts\wallpaper-download-queue.ps1',
        'F:\Wallpapers\wallpaper-download-queue.txt',
        $script:CanonicalQueueStateRoot,
        'F:\Wallpapers',
        'F:\Wallpapers\dl-engine\scripts\wallpaper-library-maintenance.ps1'
    )
    foreach ($required in $requiredActionValues) {
        if ($argumentText.IndexOf($required, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "Queue task action does not contain required pinned path: $required"
        }
    }
    foreach ($requiredSwitch in @('-Drain', '-CompactCompleted')) {
        if ($argumentText -notmatch ('(?i)(?:^|\s){0}(?:\s|$)' -f [regex]::Escape($requiredSwitch))) {
            throw "Queue task action is missing required switch $requiredSwitch."
        }
    }

    $taskInfo = Get-ScheduledTaskInfo `
        -TaskPath $script:QueueTaskPath `
        -TaskName $script:QueueTaskName `
        -ErrorAction Stop
    return [PSCustomObject][ordered]@{
        path = $script:QueueTaskPath
        name = $script:QueueTaskName
        definition_sha256 = Get-GalleryPublicationSha256Text -Text $xml
        state = [string]$task.State
        last_result = [int](Get-GalleryPublicationProperty -InputObject $taskInfo -Name 'LastTaskResult' -Default 0)
        instance_id = $null
        observed_at = Get-GalleryPublicationUtcTimestamp
        action_execute = [string](Get-GalleryPublicationProperty -InputObject $action -Name 'Execute' -Default '')
        action_arguments = $argumentText
        action_working_directory = [string](Get-GalleryPublicationProperty -InputObject $action -Name 'WorkingDirectory' -Default '')
        contract_valid = $true
    }
}

function Get-GalleryPublicationProcessSample {
    param(
        [Parameter(Mandatory = $true)][int]$Sequence,
        [Parameter(Mandatory = $true)][Diagnostics.Stopwatch]$Stopwatch
    )

    if ($null -eq (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
        throw 'Get-CimInstance is unavailable; downloader/index-writer proof cannot be collected.'
    }
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $childrenByParent = @{}
    foreach ($process in $processes) {
        $processId = [int](Get-GalleryPublicationProperty -InputObject $process -Name 'ProcessId' -Default 0)
        $parentId = [int](Get-GalleryPublicationProperty -InputObject $process -Name 'ParentProcessId' -Default 0)
        if ($processId -le 0) {
            continue
        }
        if (-not $childrenByParent.ContainsKey($parentId)) {
            $childrenByParent[$parentId] = [Collections.Generic.List[int]]::new()
        }
        $childrenByParent[$parentId].Add($processId)
    }

    $downloaderRoots = [Collections.Generic.HashSet[int]]::new()
    $downloaders = [Collections.Generic.HashSet[int]]::new()
    $writers = [Collections.Generic.HashSet[int]]::new()
    foreach ($process in $processes) {
        $processId = [int](Get-GalleryPublicationProperty -InputObject $process -Name 'ProcessId' -Default 0)
        if ($processId -le 0 -or $processId -eq $PID) {
            continue
        }
        $name = [string](Get-GalleryPublicationProperty -InputObject $process -Name 'Name' -Default '')
        $commandLine = [string](Get-GalleryPublicationProperty -InputObject $process -Name 'CommandLine' -Default '')
        $downloaderName = $name -match '^(?i:wallpaper-download(?:\.exe)?|wallpaper-anime-pictures(?:\.exe)?|gallery-dl(?:\.exe)?)$'
        $pythonName = $name -match '^(?i:python(?:w)?(?:\.exe)?)$'
        $downloaderCommand = $commandLine -match '(?i:wallpaper-download|wallpaper-anime-pictures|dl_engine\.(?:download_wallpapers|anime_pictures)|gallery-dl)'
        if ($downloaderName -or ($pythonName -and $downloaderCommand)) {
            [void]$downloaderRoots.Add($processId)
        }

        $writerCommand = $commandLine -match '(?i:dl_engine\.index_library|publish_gallery_index\.py|dl_engine\.gallery_publication)'
        $databaseWriterCommand = $commandLine.IndexOf($CanonicalDatabase, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine -match '(?i:index|publish|sqlite)'
        if ($writerCommand -or $databaseWriterCommand) {
            [void]$writers.Add($processId)
        }
        if ($pythonName -and [string]::IsNullOrWhiteSpace($commandLine)) {
            # A Python process that cannot be classified is a fail-closed writer
            # blocker. The requested write may be retried once its command line
            # can be observed or the process exits.
            [void]$writers.Add($processId)
        }
    }

    # Expand every identified downloader root through the live parent/child
    # graph. A helper or provider child remains a blocker even if its executable
    # name does not itself contain a downloader keyword.
    $pending = [Collections.Generic.Queue[int]]::new()
    foreach ($rootId in $downloaderRoots) {
        $pending.Enqueue($rootId)
    }
    while ($pending.Count -gt 0) {
        $currentId = $pending.Dequeue()
        if (-not $downloaders.Add($currentId)) {
            continue
        }
        if ($childrenByParent.ContainsKey($currentId)) {
            foreach ($childId in $childrenByParent[$currentId]) {
                $pending.Enqueue($childId)
            }
        }
    }
    $allIds = @($downloaders) + @($writers) | Sort-Object -Unique
    return [PSCustomObject][ordered]@{
        sequence = $Sequence
        elapsed_seconds = [Math]::Round($Stopwatch.Elapsed.TotalSeconds, 6)
        elapsed_milliseconds = [int64]$Stopwatch.ElapsedMilliseconds
        sampled_at = Get-GalleryPublicationUtcTimestamp
        downloader_descendant_count = $downloaders.Count
        index_writer_count = $writers.Count
        process_ids = [int[]]@($allIds)
    }
}

function Get-GalleryPublicationWriterWindow {
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $first = Get-GalleryPublicationProcessSample -Sequence 0 -Stopwatch $stopwatch
    Start-Sleep -Milliseconds $script:MinimumObservationMilliseconds
    $second = Get-GalleryPublicationProcessSample -Sequence 1 -Stopwatch $stopwatch
    $stopwatch.Stop()
    $actualWindow = [int64]$second.elapsed_milliseconds - [int64]$first.elapsed_milliseconds
    return [PSCustomObject][ordered]@{
        minimum_window_milliseconds = $script:MinimumObservationMilliseconds
        actual_window_milliseconds = $actualWindow
        samples = @($first, $second)
        zero_writers = (
            $first.downloader_descendant_count -eq 0 -and
            $first.index_writer_count -eq 0 -and
            $second.downloader_descendant_count -eq 0 -and
            $second.index_writer_count -eq 0
        )
    }
}

function Get-GalleryPublicationPublishObservationWindow {
    param([Parameter(Mandatory = $true)]$Runtime)

    $windowStartedAt = Get-GalleryPublicationUtcTimestamp
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $firstWriter = Get-GalleryPublicationProcessSample -Sequence 0 -Stopwatch $stopwatch
    $firstInspection = Invoke-GalleryPublicationReadOnlyInspection -Runtime $Runtime
    $firstSettled = [PSCustomObject][ordered]@{
        sequence = 0
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 6)
        sampled_at = Get-GalleryPublicationUtcTimestamp
        writer_sample = $firstWriter
        inspection = $firstInspection
    }

    # The quiet interval begins only after the first complete core inspection.
    # This makes the two durable-input/database snapshots themselves at least
    # 30 seconds apart instead of merely spacing the process samples.
    Start-Sleep -Milliseconds $script:MinimumObservationMilliseconds

    $secondWriter = Get-GalleryPublicationProcessSample -Sequence 1 -Stopwatch $stopwatch
    $secondInspection = Invoke-GalleryPublicationReadOnlyInspection -Runtime $Runtime
    $secondSettled = [PSCustomObject][ordered]@{
        sequence = 1
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 6)
        sampled_at = Get-GalleryPublicationUtcTimestamp
        writer_sample = $secondWriter
        inspection = $secondInspection
    }
    $stopwatch.Stop()

    $actualWriterMilliseconds = [int64]$secondWriter.elapsed_milliseconds -
        [int64]$firstWriter.elapsed_milliseconds
    $actualSettledSeconds = [double]$secondSettled.elapsed_seconds -
        [double]$firstSettled.elapsed_seconds
    return [PSCustomObject][ordered]@{
        window_started_at = $windowStartedAt
        window_ended_at = Get-GalleryPublicationUtcTimestamp
        writer_window = [PSCustomObject][ordered]@{
            minimum_window_milliseconds = $script:MinimumObservationMilliseconds
            actual_window_milliseconds = $actualWriterMilliseconds
            samples = @($firstWriter, $secondWriter)
            zero_writers = (
                $firstWriter.downloader_descendant_count -eq 0 -and
                $firstWriter.index_writer_count -eq 0 -and
                $secondWriter.downloader_descendant_count -eq 0 -and
                $secondWriter.index_writer_count -eq 0
            )
        }
        minimum_settled_window_seconds = [double]($script:MinimumObservationMilliseconds / 1000)
        actual_settled_window_seconds = [Math]::Round($actualSettledSeconds, 6)
        settled_inspections = @($firstSettled, $secondSettled)
    }
}

function Get-GalleryPublicationListenerSnapshot {
    param([Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port)

    if ($null -eq (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        throw 'Get-NetTCPConnection is unavailable; all-address listener proof cannot be collected.'
    }
    try {
        $connections = @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                Sort-Object LocalAddress, LocalPort, OwningProcess -Unique
        )
    }
    catch {
        # The NetTCPIP cmdlet reports a normal empty result as a terminating
        # CimJobException. Only that exact condition means zero listeners;
        # access/provider failures remain fatal because silence is not proof.
        if ($_.Exception -is [Microsoft.PowerShell.Cmdletization.Cim.CimJobException] -and
            $_.Exception.Message.StartsWith(
                'No matching MSFT_NetTCPConnection objects found',
                [StringComparison]::Ordinal
            )) {
            $connections = @()
        }
        else {
            throw "Listener inspection failed for port ${Port}: $($_.Exception.Message)"
        }
    }
    $bindings = @(
        foreach ($connection in $connections) {
            [PSCustomObject][ordered]@{
                address = [string]$connection.LocalAddress
                port = [int]$connection.LocalPort
                pid = [int]$connection.OwningProcess
            }
        }
    )
    return [PSCustomObject][ordered]@{
        scope = 'all-local-addresses'
        port = $Port
        listen_count = $bindings.Count
        bindings = $bindings
        observed_at = Get-GalleryPublicationUtcTimestamp
    }
}

function Get-GalleryPublicationHoldEvidence {
    param([Parameter(Mandatory = $true)]$Runtime)

    $stateFile = Join-Path $QueueStatePath 'state.json'
    $pauseFile = Join-Path $QueueStatePath 'pause.flag'
    $holdFileEvidence = Get-GalleryPublicationFileEvidence -Path $HoldPath -Label 'Hold token'
    $stateFileEvidence = Get-GalleryPublicationFileEvidence -Path $stateFile -Label 'Queue state'
    $pauseFileEvidence = Get-GalleryPublicationFileEvidence -Path $pauseFile -Label 'Queue pause token' -AllowEmpty
    $holdDocument = Read-GalleryPublicationStrictJsonObject `
        -Runtime $Runtime `
        -Path $HoldPath `
        -Label 'Hold token'
    $stateDocument = Read-GalleryPublicationStrictJsonObject `
        -Runtime $Runtime `
        -Path $stateFile `
        -Label 'Queue state'
    $holdFileAfter = Get-GalleryPublicationFileEvidence -Path $HoldPath -Label 'Hold token'
    $stateFileAfter = Get-GalleryPublicationFileEvidence -Path $stateFile -Label 'Queue state'
    $pauseFileAfter = Get-GalleryPublicationFileEvidence `
        -Path $pauseFile `
        -Label 'Queue pause token' `
        -AllowEmpty
    if ($holdFileEvidence.sha256 -ne $holdFileAfter.sha256 -or
        $stateFileEvidence.sha256 -ne $stateFileAfter.sha256 -or
        $pauseFileEvidence.sha256 -ne $pauseFileAfter.sha256) {
        throw 'Hold token, pause token, or queue state changed while its strict JSON was being parsed.'
    }
    $holdFileEvidence = $holdFileAfter
    $stateFileEvidence = $stateFileAfter
    $pauseFileEvidence = $pauseFileAfter
    if ([int](Get-GalleryPublicationProperty -InputObject $stateDocument -Name 'schemaVersion' -Default -1) -ne 1) {
        throw 'Queue state schemaVersion is not 1.'
    }
    return [PSCustomObject][ordered]@{
        hold_file = $holdFileEvidence
        hold_document = $holdDocument
        state_file = $stateFileEvidence
        state_document = $stateDocument
        pause_file = $pauseFileEvidence
        queue_state_schema_version = 1
        externally_owned = $true
        publisher_may_release = $false
        observed_at = Get-GalleryPublicationUtcTimestamp
    }
}

function Get-GalleryPublicationCanonicalHandleEvidence {
    $handlePaths = @($CanonicalDatabase, "$CanonicalDatabase-wal", "$CanonicalDatabase-shm")
    $handleResults = [ordered]@{}
    foreach ($path in $handlePaths) {
        $handleResults[$path] = Test-GalleryPublicationExclusiveRead -Path $path
    }
    $canonicalHandleFree = @($handleResults.Values | Where-Object { -not $_ }).Count -eq 0
    return [PSCustomObject][ordered]@{
        observations = $handleResults
        canonical_handle_free = $canonicalHandleFree
    }
}

function Get-GalleryPublicationRequiredStringProperty {
    param(
        [Parameter(Mandatory = $true)]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $value = Get-GalleryPublicationProperty -InputObject $InputObject -Name $Name
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$value)) {
        throw "$Label is missing required non-empty string '$Name'."
    }
    return [string]$value
}

function Get-GalleryPublicationRequiredSha256Property {
    param(
        [Parameter(Mandatory = $true)]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $value = Get-GalleryPublicationRequiredStringProperty `
        -InputObject $InputObject `
        -Name $Name `
        -Label $Label
    if ($value -notmatch '^[0-9a-f]{64}$') {
        throw "$Label property '$Name' is not a lowercase SHA-256 value."
    }
    return $value
}

function ConvertTo-GalleryPublicationClosedHoldEvidence {
    param([Parameter(Mandatory = $true)]$HoldEvidence)

    $document = Get-GalleryPublicationProperty -InputObject $HoldEvidence -Name 'hold_document'
    if ($null -eq $document) {
        throw 'Fresh hold evidence has no parsed hold document.'
    }
    $owner = Get-GalleryPublicationRequiredStringProperty `
        -InputObject $document -Name 'owner' -Label 'Fresh hold token'
    $tokenId = Get-GalleryPublicationRequiredStringProperty `
        -InputObject $document -Name 'token_id' -Label 'Fresh hold token'
    $acknowledgementSha = Get-GalleryPublicationRequiredSha256Property `
        -InputObject $document -Name 'acknowledgement_sha256' -Label 'Fresh hold token'
    $taskAcknowledged = Get-GalleryPublicationProperty `
        -InputObject $document `
        -Name 'task_acknowledged'
    if ($taskAcknowledged -isnot [bool] -or $taskAcknowledged -ne $true) {
        throw "Fresh hold token property 'task_acknowledged' must be the JSON boolean true."
    }

    $holdFile = Get-GalleryPublicationProperty -InputObject $HoldEvidence -Name 'hold_file'
    $pauseFile = Get-GalleryPublicationProperty -InputObject $HoldEvidence -Name 'pause_file'
    $stateFile = Get-GalleryPublicationProperty -InputObject $HoldEvidence -Name 'state_file'
    foreach ($entry in @(
        @('hold_file', $holdFile),
        @('pause_file', $pauseFile),
        @('queue_state', $stateFile)
    )) {
        if ($null -eq $entry[1] -or
            (Get-GalleryPublicationProperty -InputObject $entry[1] -Name 'exists' -Default $false) -ne $true) {
            throw "Fresh hold evidence is missing the present $($entry[0]) identity."
        }
    }
    $tokenSha = [string](Get-GalleryPublicationProperty -InputObject $holdFile -Name 'sha256' -Default '')
    if ($tokenSha -notmatch '^[0-9a-f]{64}$') {
        throw 'Fresh hold-file identity has no lowercase SHA-256 value.'
    }

    return [PSCustomObject][ordered]@{
        externally_owned = $true
        owner = $owner
        token_id = $tokenId
        token_sha256 = $tokenSha
        hold_file = $holdFile
        pause_file = $pauseFile
        queue_state = $stateFile
        queue_state_schema_version = 1
        acquired_at = ConvertTo-GalleryPublicationUtcTimestamp `
            -Value (Get-GalleryPublicationRequiredStringProperty -InputObject $document -Name 'acquired_at' -Label 'Fresh hold token') `
            -Label 'Fresh hold token acquired_at'
        expires_at = ConvertTo-GalleryPublicationUtcTimestamp `
            -Value (Get-GalleryPublicationRequiredStringProperty -InputObject $document -Name 'expires_at' -Label 'Fresh hold token') `
            -Label 'Fresh hold token expires_at'
        acknowledged_at = ConvertTo-GalleryPublicationUtcTimestamp `
            -Value (Get-GalleryPublicationRequiredStringProperty -InputObject $document -Name 'acknowledged_at' -Label 'Fresh hold token') `
            -Label 'Fresh hold token acknowledged_at'
        acknowledgement_sha256 = $acknowledgementSha
        task_acknowledged = $true
        publisher_may_release = $false
    }
}

function ConvertTo-GalleryPublicationClosedTaskEvidence {
    param(
        [Parameter(Mandatory = $true)]$TaskEvidence,
        [Parameter(Mandatory = $true)]$HoldEvidence,
        [Parameter(Mandatory = $true)][string]$AcknowledgedAt
    )

    $document = Get-GalleryPublicationProperty -InputObject $HoldEvidence -Name 'hold_document'
    if ($null -eq $document) {
        throw 'Fresh hold evidence has no parsed hold document for task acknowledgement.'
    }
    $tokenTaskPath = Get-GalleryPublicationRequiredStringProperty `
        -InputObject $document -Name 'task_path' -Label 'Fresh hold token'
    $tokenTaskName = Get-GalleryPublicationRequiredStringProperty `
        -InputObject $document -Name 'task_name' -Label 'Fresh hold token'
    $tokenDefinition = Get-GalleryPublicationRequiredSha256Property `
        -InputObject $document -Name 'task_definition_sha256' -Label 'Fresh hold token'
    foreach ($comparison in @(
        @('path', $tokenTaskPath),
        @('name', $tokenTaskName),
        @('definition_sha256', $tokenDefinition)
    )) {
        $current = [string](Get-GalleryPublicationProperty `
            -InputObject $TaskEvidence -Name $comparison[0] -Default '')
        if (-not $current.Equals([string]$comparison[1], [StringComparison]::Ordinal)) {
            throw "Current scheduled task '$($comparison[0])' differs from the externally acknowledged hold token."
        }
    }
    $tokenObservedAt = ConvertTo-GalleryPublicationUtcTimestamp `
        -Value (Get-GalleryPublicationRequiredStringProperty `
            -InputObject $document -Name 'task_observed_at' -Label 'Fresh hold token') `
        -Label 'Fresh hold token task_observed_at'

    return [PSCustomObject][ordered]@{
        path = [string]$TaskEvidence.path
        name = [string]$TaskEvidence.name
        definition_sha256 = [string]$TaskEvidence.definition_sha256
        state = [string]$TaskEvidence.state
        last_result = $TaskEvidence.last_result
        instance_id = $TaskEvidence.instance_id
        observed_at = $tokenObservedAt
        acknowledged_at = $AcknowledgedAt
    }
}

function ConvertTo-GalleryPublicationRecoveryWriterSamples {
    param([Parameter(Mandatory = $true)]$WriterWindow)

    return @(
        foreach ($sample in @($WriterWindow.samples)) {
            [PSCustomObject][ordered]@{
                sequence = [int]$sample.sequence
                elapsed_milliseconds = [int64]$sample.elapsed_milliseconds
                sampled_at = ConvertTo-GalleryPublicationUtcTimestamp `
                    -Value $sample.sampled_at `
                    -Label 'Recovery writer sampled_at'
                downloader_descendant_count = [int]$sample.downloader_descendant_count
                index_writer_count = [int]$sample.index_writer_count
                process_ids = [int[]]@($sample.process_ids)
            }
        }
    )
}

function New-GalleryPublicationRecoveryHold {
    param(
        [Parameter(Mandatory = $true)]$RecoveryInspection,
        [Parameter(Mandatory = $true)]$HoldEvidence,
        [Parameter(Mandatory = $true)]$TaskEvidence,
        [Parameter(Mandatory = $true)]$WriterWindow,
        [Parameter(Mandatory = $true)]$ListenerSnapshot,
        [Parameter(Mandatory = $true)][bool]$CanonicalHandleFree,
        [Parameter(Mandatory = $true)][string]$RecoveryStartedAt
    )

    $result = Get-GalleryPublicationProperty -InputObject $RecoveryInspection -Name 'result'
    $journal = Get-GalleryPublicationProperty -InputObject $result -Name 'journal'
    $context = Get-GalleryPublicationProperty -InputObject $result -Name 'recovery_context'
    if ($null -eq $context) {
        # The CLI names this read-only pre-activation context `anchor`; accept
        # that authoritative result without re-reading or hashing the manifest
        # in PowerShell.
        $context = Get-GalleryPublicationProperty -InputObject $result -Name 'anchor'
    }
    if ($null -eq $journal) {
        throw 'CLI recovery inspection did not return its journal head.'
    }
    $authorizedHead = Get-GalleryPublicationProperty `
        -InputObject $journal `
        -Name 'head_sha256'
    if ($null -ne $authorizedHead -and
        ([string]$authorizedHead -notmatch '^[0-9a-f]{64}$')) {
        throw "CLI recovery journal inspection property 'head_sha256' is neither null nor a lowercase SHA-256 value."
    }
    if ($null -eq $context) {
        throw 'CLI recovery inspection did not return a historical hold-token anchor; a fresh recovery hold cannot be constructed.'
    }
    $historicalTokenSha = Get-GalleryPublicationRequiredSha256Property `
        -InputObject $context `
        -Name 'historical_hold_token_sha256' `
        -Label 'CLI recovery context'

    $closedHold = ConvertTo-GalleryPublicationClosedHoldEvidence -HoldEvidence $HoldEvidence
    if ($closedHold.token_sha256 -eq $historicalTokenSha) {
        throw 'Fresh recovery hold token SHA-256 equals the historical publication hold token.'
    }
    $closedTask = ConvertTo-GalleryPublicationClosedTaskEvidence `
        -TaskEvidence $TaskEvidence `
        -HoldEvidence $HoldEvidence `
        -AcknowledgedAt ([string]$closedHold.acknowledged_at)
    $writerSamples = ConvertTo-GalleryPublicationRecoveryWriterSamples -WriterWindow $WriterWindow

    return [PSCustomObject][ordered]@{
        purpose = 'emergency-recovery'
        recovery_attempt_id = [Guid]::NewGuid().ToString('N')
        authorized_head_sha256 = $authorizedHead
        recovery_started_at = $RecoveryStartedAt
        historical_hold_token_sha256 = $historicalTokenSha
        hold = $closedHold
        scheduled_task = $closedTask
        minimum_window_milliseconds = $script:MinimumObservationMilliseconds
        actual_window_milliseconds = [int64]$WriterWindow.actual_window_milliseconds
        writer_samples = $writerSamples
        listener_snapshot = $ListenerSnapshot
        canonical_handle_free = $CanonicalHandleFree
        maximum_evidence_age_seconds = 300
        verified_at = Get-GalleryPublicationUtcTimestamp
    }
}

function Get-GalleryPublicationPublishRuntimeEvidence {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)][hashtable]$PathEvidence,
        [Parameter(Mandatory = $true)][bool]$ApplyRequested
    )

    $hold = Get-GalleryPublicationHoldEvidence -Runtime $Runtime
    $task = Get-GalleryPublicationQueueTaskEvidence
    $observations = Get-GalleryPublicationPublishObservationWindow -Runtime $Runtime
    $listener = Get-GalleryPublicationListenerSnapshot -Port 8090
    $smokePort = Get-GalleryPublicationListenerSnapshot -Port 8091
    $handles = Get-GalleryPublicationCanonicalHandleEvidence

    if ($ApplyRequested) {
        if (-not $observations.writer_window.zero_writers) {
            throw 'Apply refused: downloader descendants or index writers were observed during the required 30-second interval.'
        }
        if ($listener.listen_count -ne 0) {
            throw 'Apply refused: port 8090 still has a listener. Cutover authorization never permits the wrapper to stop it.'
        }
        if ($smokePort.listen_count -ne 0) {
            throw 'Apply refused: smoke port 8091 is not free.'
        }
        if (-not $handles.canonical_handle_free) {
            throw 'Apply refused: the canonical database set did not pass the exclusive-handle probe.'
        }
    }

    return [PSCustomObject][ordered]@{
        evidence_version = 1
        mode = 'publish'
        captured_at = Get-GalleryPublicationUtcTimestamp
        machine_identity = $Identity
        paths = $PathEvidence
        hold = $hold
        scheduled_task = $task
        observation_window = $observations
        writer_window = $observations.writer_window
        settled_inspections = $observations.settled_inspections
        listener_snapshot = $listener
        smoke_port_snapshot = $smokePort
        canonical_handle_observations = $handles.observations
        canonical_handle_free = $handles.canonical_handle_free
        apply = $ApplyRequested
        apply_requested = $ApplyRequested
        cutover_authorized = [bool]$CutoverAuthorized
        what_if = [bool]$WhatIfPreference
        publisher_mutated_external_authority = $false
    }
}

function Get-GalleryPublicationRecoveryRuntimeEvidence {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)][hashtable]$PathEvidence,
        [Parameter(Mandatory = $true)][bool]$ApplyRequested
    )

    $recoveryStartedAt = Get-GalleryPublicationUtcTimestamp
    $inspection = Invoke-GalleryPublicationReadOnlyRecoveryInspection -Runtime $Runtime
    if (-not $ApplyRequested) {
        return [PSCustomObject][ordered]@{
            evidence_version = 1
            mode = 'recover'
            captured_at = Get-GalleryPublicationUtcTimestamp
            machine_identity = $Identity
            paths = $PathEvidence
            recovery_inspection = $inspection
            continuation_segments = @($ContinuationSegments)
            recovery_hold_ready = $false
            apply = $false
            apply_requested = $false
            cutover_authorized = $false
            what_if = [bool]$WhatIfPreference
            publisher_mutated_external_authority = $false
        }
    }

    try {
        $hold = Get-GalleryPublicationHoldEvidence -Runtime $Runtime
        $task = Get-GalleryPublicationQueueTaskEvidence
        # The recovery mutation boundary begins only after the external hold
        # and its acknowledged task snapshot exist.  The following writer and
        # listener observations must fall wholly inside that boundary.
        $recoveryStartedAt = Get-GalleryPublicationUtcTimestamp
        $writers = Get-GalleryPublicationWriterWindow
        $listener = Get-GalleryPublicationListenerSnapshot -Port 8090
        $handles = Get-GalleryPublicationCanonicalHandleEvidence
        $rawBoundary = [PSCustomObject][ordered]@{
            hold = $hold
            scheduled_task = $task
            writer_window = $writers
            listener_snapshot = $listener
            canonical_handle_observations = $handles.observations
            canonical_handle_free = $handles.canonical_handle_free
        }
        if (-not $writers.zero_writers) {
            throw 'downloader descendants or index writers were observed during the required 30-second interval.'
        }
        if ($listener.listen_count -ne 0) {
            throw 'port 8090 still has a listener; recovery authority never permits the wrapper to stop it.'
        }
        if (-not $handles.canonical_handle_free) {
            throw 'the canonical database set did not pass the stored exclusive-handle probe.'
        }
        $recoveryHold = New-GalleryPublicationRecoveryHold `
            -RecoveryInspection $inspection `
            -HoldEvidence $hold `
            -TaskEvidence $task `
            -WriterWindow $writers `
            -ListenerSnapshot $listener `
            -CanonicalHandleFree ([bool]$handles.canonical_handle_free) `
            -RecoveryStartedAt $recoveryStartedAt
    }
    catch {
        throw "Recover apply refused before mutation: $($_.Exception.Message)"
    }

    return [PSCustomObject][ordered]@{
        evidence_version = 1
        mode = 'recover'
        captured_at = Get-GalleryPublicationUtcTimestamp
        machine_identity = $Identity
        paths = $PathEvidence
        recovery_inspection = $inspection
        continuation_segments = @($ContinuationSegments)
        raw_recovery_boundary = $rawBoundary
        recovery_hold = $recoveryHold
        recovery_hold_ready = $true
        apply = $true
        apply_requested = $true
        cutover_authorized = $false
        what_if = [bool]$WhatIfPreference
        publisher_mutated_external_authority = $false
    }
}

function Get-GalleryPublicationRuntimeEvidence {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)][hashtable]$PathEvidence,
        [Parameter(Mandatory = $true)][bool]$ApplyRequested
    )

    switch ($Mode) {
        'Publish' {
            return Get-GalleryPublicationPublishRuntimeEvidence `
                -Runtime $Runtime `
                -Identity $Identity `
                -PathEvidence $PathEvidence `
                -ApplyRequested $ApplyRequested
        }
        'Recover' {
            return Get-GalleryPublicationRecoveryRuntimeEvidence `
                -Runtime $Runtime `
                -Identity $Identity `
                -PathEvidence $PathEvidence `
                -ApplyRequested $ApplyRequested
        }
        default {
            throw "Runtime evidence is not defined for mode '$Mode'."
        }
    }
}

function Get-GalleryPublicationPathMap {
    $paths = [ordered]@{
        CanonicalDatabase = $CanonicalDatabase
        BackupDirectory = $BackupDirectory
        RecoveryJournal = $RecoveryJournal
        RecoveryResultRoot = $RecoveryResultRoot
        QueueStatePath = $QueueStatePath
        HoldPath = $HoldPath
        ManifestPath = $ManifestPath
    }
    if ($Mode -eq 'Recover') {
        for ($index = 0; $index -lt $ContinuationSegments.Count; $index++) {
            $paths["ContinuationSegment$index"] = [string]$ContinuationSegments[$index]
        }
    }
    if ($Mode -ne 'Recover') {
        $paths.CandidateDatabase = $CandidateDatabase
        $paths.LibraryRoot = $LibraryRoot
        $paths.WallhavenLedger = $WallhavenLedger
        $paths.ProviderLedger = $ProviderLedger
        $paths.SiblingDatabase = $SiblingDatabase
        $paths.VerificationReportRoot = $VerificationReportRoot
    }
    return $paths
}

function Get-GalleryPublicationPythonArguments {
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)][bool]$ExecuteApply,
        [Parameter(Mandatory = $true)][bool]$IncludeRuntimeEvidence
    )

    $arguments = [Collections.Generic.List[string]]::new()
    [void]$arguments.Add('-B')
    [void]$arguments.Add([string]$Runtime.cli_path)
    [void]$arguments.Add($Mode.ToLowerInvariant())

    foreach ($pair in @(
        @('--canonical-database', $CanonicalDatabase),
        @('--backup-directory', $BackupDirectory),
        @('--recovery-journal', $RecoveryJournal),
        @('--recovery-result-root', $RecoveryResultRoot),
        @('--queue-state-path', $QueueStatePath),
        @('--hold-path', $HoldPath),
        @('--manifest', $ManifestPath)
    )) {
        [void]$arguments.Add($pair[0])
        [void]$arguments.Add($pair[1])
    }
    if ($Mode -ne 'Recover') {
        foreach ($pair in @(
            @('--candidate-database', $CandidateDatabase),
            @('--library-root', $LibraryRoot),
            @('--wallhaven-ledger', $WallhavenLedger),
            @('--provider-ledger', $ProviderLedger),
            @('--sibling-database', $SiblingDatabase),
            @('--verification-report-root', $VerificationReportRoot)
        )) {
            [void]$arguments.Add($pair[0])
            [void]$arguments.Add($pair[1])
        }
    }
    if ($IncludeRuntimeEvidence) {
        [void]$arguments.Add('--runtime-evidence-stdin')
    }
    if ($CutoverAuthorized) {
        [void]$arguments.Add('--cutover-authorized')
    }
    if ($ExecuteApply) {
        [void]$arguments.Add('--apply')
    }
    return [string[]]$arguments.ToArray()
}

$applyRequested = $Mode -in @('Prepare', 'Publish', 'Recover') -and
    $Apply.IsPresent -and -not [bool]$WhatIfPreference
Assert-GalleryPublicationModeArguments -ApplyRequested $applyRequested

$runtime = Get-GalleryPublicationProjectRuntime
$identity = Get-GalleryPublicationVerifiedIdentity
$importEvidence = Assert-GalleryPublicationImportOrigin -Runtime $runtime

$pathMap = Get-GalleryPublicationPathMap
$pathEvidence = @{}
foreach ($entry in $pathMap.GetEnumerator()) {
    $pathEvidence[$entry.Key] = Get-GalleryPublicationPathEvidence `
        -Path ([string]$entry.Value) `
        -Label ([string]$entry.Key)
}

if ($applyRequested) {
    Assert-GalleryPublicationApplyPaths `
        -ProjectRoot $runtime.project_root `
        -PathEvidence $pathEvidence
}

$operation = switch ($Mode) {
    'Inspect' { 'Inspect gallery publication state' }
    'Prepare' { 'Build and verify a unique schema-4 gallery candidate' }
    'Publish' { 'Publish the verified gallery candidate to the canonical database path' }
    'Recover' { 'Recover the identified publication transaction backward to its verified backup state' }
}
$target = if ($Mode -eq 'Prepare') { $CandidateDatabase } else { $CanonicalDatabase }
$shouldApply = $false
if ($Mode -in @('Prepare', 'Publish', 'Recover') -and $Apply.IsPresent) {
    # Invoke ShouldProcess even under -WhatIf so PowerShell emits its standard
    # transition preview. The independent applyRequested term still prevents an
    # accidental write if a host supplies an unusual ShouldProcess override.
    $shouldApply = $PSCmdlet.ShouldProcess($target, $operation)
}
$executeApply = $applyRequested -and $shouldApply

$includeRuntimeEvidence = $Mode -in @('Prepare', 'Publish', 'Recover')
$evidenceJson = $null
if ($includeRuntimeEvidence) {
    if ($Mode -eq 'Prepare') {
        # Candidate preparation needs verified machine/path authority but does
        # not enter the maintenance boundary. Keep this evidence read-only and
        # avoid the hold, 30-second writer window, handle, and listener probes.
        $runtimeEvidence = [PSCustomObject][ordered]@{
            evidence_version = 1
            mode = 'prepare'
            captured_at = Get-GalleryPublicationUtcTimestamp
            machine_identity = $identity
            import_origin = $importEvidence
            paths = $pathEvidence
            apply_requested = $executeApply
            cutover_authorized = $false
            what_if = [bool]$WhatIfPreference
            publisher_mutated_external_authority = $false
        }
    }
    else {
        $runtimeEvidence = Get-GalleryPublicationRuntimeEvidence `
            -Runtime $runtime `
            -Identity $identity `
            -PathEvidence $pathEvidence `
            -ApplyRequested $executeApply
        $runtimeEvidence | Add-Member -NotePropertyName import_origin -NotePropertyValue $importEvidence
    }
    $evidenceJson = $runtimeEvidence | ConvertTo-Json -Depth 100 -Compress
}

$pythonArguments = Get-GalleryPublicationPythonArguments `
    -Runtime $runtime `
    -ExecuteApply $executeApply `
    -IncludeRuntimeEvidence $includeRuntimeEvidence
$result = Invoke-GalleryPublicationPython `
    -Runtime $runtime `
    -Arguments $pythonArguments `
    -StandardInput $evidenceJson

if (-not [string]::IsNullOrWhiteSpace($result.stdout)) {
    $result.stdout.TrimEnd() | Write-Output
}
if (-not [string]::IsNullOrWhiteSpace($result.stderr)) {
    $result.stderr.TrimEnd() | Write-Error -ErrorAction Continue
}
if ($result.exit_code -ne 0) {
    throw "Gallery publication $($Mode.ToLowerInvariant()) failed with exit code $($result.exit_code)."
}
