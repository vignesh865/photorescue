<#
.SYNOPSIS
  Guided launcher for PhotoRescue (deep photo recovery) on Windows 10/11.
.DESCRIPTION
  Lists disks and connected Android phones, self-elevates when the source is a
  disk (raw sector access needs it), verifies Python, then runs photorescue.py.
  Everything it does against the source is read-only.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\Run-PhotoRescue.ps1
.EXAMPLE
  .\Run-PhotoRescue.ps1 -Source adb -Out E:\Recovered
#>
[CmdletBinding()]
param(
    [string]$Source,                       # "D:" | "1" | "\\.\PhysicalDrive1" | "adb"
    [string]$Out,                          # must be on a DIFFERENT disk
    [string]$Phases,                       # default depends on the source type
    [string]$Types  = "all",
    [int]$MinSizeKB = 16,
    [switch]$SkipVideo,
    [switch]$Resume,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$script:Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Py   = Join-Path $Root "photorescue.py"

function Test-PhoneSource([string]$s) { return $s -match '^(?i)\s*(adb|phone|android)(:|$)' }

function Assert-Admin([hashtable]$resolved) {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "Elevating to Administrator (raw disk access requires it)..." -ForegroundColor Yellow
        $argList = @("-ExecutionPolicy","Bypass","-NoExit","-File","`"$PSCommandPath`"")
        # carry the interactively-entered answers across, not just -bound params,
        # or the elevated copy would ask for them all over again
        foreach ($kv in $resolved.GetEnumerator()) {
            if ($null -eq $kv.Value -or "$($kv.Value)" -eq "") { continue }
            if ($kv.Value -is [switch] -or $kv.Value -is [bool]) {
                if ($kv.Value) { $argList += "-$($kv.Key)" }
            } else { $argList += @("-$($kv.Key)", "`"$($kv.Value)`"") }
        }
        Start-Process powershell -Verb RunAs -ArgumentList $argList
        exit
    }
}

function Get-Python {
    foreach ($c in @("py -3", "python", "python3")) {
        $exe, $arg = $c.Split(" ", 2)
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                $v = & $exe $arg --version 2>&1
                if ($v -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 8) { return ,@($exe, $arg) }
            } catch { }
        }
    }
    Write-Host "Python 3.8+ not found." -ForegroundColor Red
    Write-Host "Install it with:  winget install -e --id Python.Python.3.12" -ForegroundColor Yellow
    Write-Host "(tick 'Add python.exe to PATH'), then re-run this script."
    exit 1
}

function Find-Adb {
    $c = Get-Command adb -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @(
        (Join-Path $script:Root "platform-tools\adb.exe"),
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
        "C:\platform-tools\adb.exe",
        "$env:ProgramFiles\platform-tools\adb.exe",
        "$env:USERPROFILE\Downloads\platform-tools\adb.exe",
        "$env:USERPROFILE\Desktop\platform-tools\adb.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Show-Disks {
    Write-Host "`n=== Physical disks ===" -ForegroundColor Cyan
    Get-Disk | Sort-Object Number | ForEach-Object {
        $d = $_
        "{0,-5} {1,-38} {2,10:N1} GB  {3,-6} health={4} bus={5}" -f `
            "[$($d.Number)]", $d.FriendlyName, ($d.Size/1GB), $d.PartitionStyle, $d.HealthStatus, $d.BusType | Write-Host
        Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
            $p = $_
            $v = Get-Volume -Partition $p -ErrorAction SilentlyContinue
            $letter = if ($p.DriveLetter) { "$($p.DriveLetter):" } else { "  -" }
            "        part {0}  {1}  {2,8:N1} GB  {3,-6} {4}" -f `
                $p.PartitionNumber, $letter, ($p.Size/1GB), $v.FileSystemType, $v.FileSystemLabel | Write-Host
        }
    }
    Write-Host ""
}

function Show-Phones {
    Write-Host "=== Android phones ===" -ForegroundColor Cyan
    $adb = Find-Adb
    if (-not $adb) {
        Write-Host "  adb not installed - a phone cannot be read without it."
        Write-Host "  Install:  winget install -e --id Google.PlatformTools" -ForegroundColor Yellow
        Write-Host ""
        return
    }
    $lines = @(& $adb devices -l 2>$null | Select-Object -Skip 1 | Where-Object { $_.Trim() })
    if (-not $lines) {
        Write-Host "  none detected. Unlock the phone, set the USB mode to 'File transfer',"
        Write-Host "  and turn on USB debugging (Settings > Developer options)."
    }
    foreach ($l in $lines) {
        $f = $l -split '\s+'
        $note = switch ($f[1]) {
            "unauthorized" { "  <- accept the 'Allow USB debugging?' prompt on the phone" }
            "offline"      { "  <- replug the cable / try another USB port" }
            default        { "" }
        }
        $desc = if ($f.Count -gt 2) { ($f[2..($f.Count-1)] -join " ") } else { "" }
        "  {0,-24} {1,-14} {2}{3}" -f $f[0], $f[1], $desc, $note | Write-Host
    }
    Write-Host "  Use one with:  -Source adb   (recovers trash, thumbnails and app caches)"
    Write-Host ""
}

function Get-DiskLettersFor([string]$src) {
    if ($src -match "(?i)PhysicalDrive(\d+)|^(\d+)$") {
        $n = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
        return (Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue |
                Where-Object DriveLetter | ForEach-Object { "$($_.DriveLetter):" })
    }
    if ($src -match "^([A-Za-z]):?$") { return @("$($Matches[1].ToUpper()):") }
    return @()
}

if (-not (Test-Path $script:Py)) { Write-Host "photorescue.py not found next to this script." -ForegroundColor Red; exit 1 }
$py = Get-Python
Write-Host "PhotoRescue - deep photo recovery" -ForegroundColor Green
Write-Host "Python: $($py[0]) $($py[1])`n"

Show-Disks
Show-Phones

if (-not $Source) {
    Write-Host "Pick the SOURCE to recover from:"
    Write-Host "  * whole disk (best for formatted / RAW / unreadable drives) : type the disk number, e.g. 1"
    Write-Host "  * one partition (faster, needs a working filesystem)        : type the letter, e.g. D:"
    Write-Host "  * an Android phone plugged in over USB                      : type adb"
    $Source = Read-Host "Source"
}
$isPhone = Test-PhoneSource $Source

# A phone is reached through adb, which does NOT need Administrator - and
# elevating would start a second adb server the phone has not authorised yet.
if (-not $isPhone) { Assert-Admin @{ Source=$Source; Out=$Out; Phases=$Phases; Types=$Types;
                                     MinSizeKB=$MinSizeKB; SkipVideo=$SkipVideo; Resume=$Resume; DryRun=$DryRun } }

if (-not $Phases) { $Phases = if ($isPhone) { "phone" } else { "sweep,bin,vss,carve" } }

if (-not $Out) {
    Write-Host "`nPick the OUTPUT folder."
    if ($isPhone) { Write-Host "Anywhere on this PC is fine - we never write to the phone." }
    else {
        Write-Host "It MUST be on a different physical disk."
        Write-Host "Rule of thumb: reserve free space >= the size of the source disk."
    }
    $Out = Read-Host "Output folder (e.g. E:\Recovered)"
}

# --- safety checks ---------------------------------------------------------
$outLetter = ""
if ($Out -match "^([A-Za-z]):") { $outLetter = "$($Matches[1].ToUpper()):" }
elseif ($Out -notmatch "^\\\\") {
    $outLetter = ([System.IO.Path]::GetPathRoot((Join-Path (Get-Location) $Out))).TrimEnd("\")
}
if (-not $isPhone) {
    $srcLetters = Get-DiskLettersFor $Source
    if ($srcLetters -contains $outLetter.ToUpper()) {
        Write-Host "`nREFUSING TO RUN: the output folder is on the disk you are recovering." -ForegroundColor Red
        Write-Host "Writing there overwrites the deleted photos you are trying to get back."
        exit 1
    }
}
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$free = (Get-PSDrive -Name $outLetter.TrimEnd(":") -ErrorAction SilentlyContinue).Free
if ($free) { Write-Host ("Free space on {0} : {1:N1} GB" -f $outLetter, ($free/1GB)) }

if ($isPhone) {
    Write-Host "`nWhile the scan runs: keep the phone unlocked and plugged in, and do NOT" -ForegroundColor Yellow
    Write-Host "take new photos or install anything - that is what overwrites deleted ones." -ForegroundColor Yellow
    Write-Host "Nothing is written to the phone; it is only read." -ForegroundColor Yellow
} else {
    Write-Host "`nWhile the scan runs: do NOT use the source disk, do NOT let Windows 'repair' it," -ForegroundColor Yellow
    Write-Host "and do NOT run chkdsk /f or format it. The scan itself only reads." -ForegroundColor Yellow
}

$argv = @($script:Py, "--source", $Source, "--out", $Out, "--phases", $Phases,
          "--types", $Types, "--min-size", ($MinSizeKB * 1024))
if ($SkipVideo) { $argv += "--skip-video" }
if ($Resume)    { $argv += "--resume" }
if ($DryRun)    { $argv += "--dry-run" }

Write-Host "`nRunning: $($py[0]) $($py[1]) $($argv -join ' ')`n" -ForegroundColor Cyan
$sw = [Diagnostics.Stopwatch]::StartNew()
if ($py[1]) { & $py[0] $py[1] @argv } else { & $py[0] @argv }
$sw.Stop()
Write-Host "`nElapsed: $($sw.Elapsed.ToString('hh\:mm\:ss'))" -ForegroundColor Green
Write-Host "Recovered files: $Out"
Write-Host "Manifest       : $Out\_photorescue_manifest.csv"
Write-Host "Interrupted? re-run the same command with -Resume"
