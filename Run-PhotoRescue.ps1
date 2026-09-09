<#
.SYNOPSIS
  Guided launcher for PhotoRescue (deep photo recovery) on Windows 10/11.
.DESCRIPTION
  Self-elevates, verifies Python, lists disks, then runs photorescue.py.
  Everything it does against the damaged disk is read-only.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\Run-PhotoRescue.ps1
#>
[CmdletBinding()]
param(
    [string]$Source,                       # "D:"  |  "1"  |  "\\.\PhysicalDrive1"
    [string]$Out,                          # must be on a DIFFERENT disk
    [string]$Phases = "sweep,bin,vss,carve",
    [string]$Types  = "all",
    [int]$MinSizeKB = 16,
    [switch]$SkipVideo,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$script:Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Py   = Join-Path $Root "photorescue.py"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "Elevating to Administrator (raw disk access requires it)..." -ForegroundColor Yellow
        $argList = @("-ExecutionPolicy","Bypass","-NoExit","-File","`"$PSCommandPath`"")
        foreach ($kv in $PSBoundParameters.GetEnumerator()) {
            if ($kv.Value -is [switch]) { if ($kv.Value.IsPresent) { $argList += "-$($kv.Key)" } }
            else { $argList += @("-$($kv.Key)", "`"$($kv.Value)`"") }
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

function Get-DiskLettersFor([string]$src) {
    if ($src -match "(?i)PhysicalDrive(\d+)|^(\d+)$") {
        $n = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
        return (Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue |
                Where-Object DriveLetter | ForEach-Object { "$($_.DriveLetter):" })
    }
    if ($src -match "^([A-Za-z]):?$") { return @("$($Matches[1].ToUpper()):") }
    return @()
}

Assert-Admin
if (-not (Test-Path $script:Py)) { Write-Host "photorescue.py not found next to this script." -ForegroundColor Red; exit 1 }
$py = Get-Python
Write-Host "PhotoRescue - deep photo recovery" -ForegroundColor Green
Write-Host "Python: $($py[0]) $($py[1])`n"

Show-Disks

if (-not $Source) {
    Write-Host "Pick the SOURCE to recover from:"
    Write-Host "  * whole disk (best for formatted / RAW / unreadable drives) : type the disk number, e.g. 1"
    Write-Host "  * one partition (faster, needs a working filesystem)        : type the letter, e.g. D:"
    $Source = Read-Host "Source"
}
if (-not $Out) {
    Write-Host "`nPick the OUTPUT folder. It MUST be on a different physical disk."
    Write-Host "Rule of thumb: reserve free space >= the size of the source disk."
    $Out = Read-Host "Output folder (e.g. E:\Recovered)"
}

# --- safety checks ---------------------------------------------------------
$srcLetters = Get-DiskLettersFor $Source
$outLetter  = ""
if ($Out -match "^([A-Za-z]):") { $outLetter = "$($Matches[1].ToUpper()):" }
elseif ($Out -notmatch "^\\\\") {
    $outLetter = ([System.IO.Path]::GetPathRoot((Join-Path (Get-Location) $Out))).TrimEnd("\")
}
if ($srcLetters -contains $outLetter.ToUpper()) {
    Write-Host "`nREFUSING TO RUN: the output folder is on the disk you are recovering." -ForegroundColor Red
    Write-Host "Writing there overwrites the deleted photos you are trying to get back."
    exit 1
}
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$free = (Get-PSDrive -Name $outLetter.TrimEnd(":") -ErrorAction SilentlyContinue).Free
if ($free) { Write-Host ("Free space on {0} : {1:N1} GB" -f $outLetter, ($free/1GB)) }

Write-Host "`nWhile the scan runs: do NOT use the source disk, do NOT let Windows 'repair' it," -ForegroundColor Yellow
Write-Host "and do NOT run chkdsk /f or format it. The scan itself only reads." -ForegroundColor Yellow

$argv = @($script:Py, "--source", $Source, "--out", $Out, "--phases", $Phases,
          "--types", $Types, "--min-size", ($MinSizeKB * 1024))
if ($SkipVideo) { $argv += "--skip-video" }
if ($Resume)    { $argv += "--resume" }

Write-Host "`nRunning: $($py[0]) $($py[1]) $($argv -join ' ')`n" -ForegroundColor Cyan
$sw = [Diagnostics.Stopwatch]::StartNew()
if ($py[1]) { & $py[0] $py[1] @argv } else { & $py[0] @argv }
$sw.Stop()
Write-Host "`nElapsed: $($sw.Elapsed.ToString('hh\:mm\:ss'))" -ForegroundColor Green
Write-Host "Recovered files: $Out"
Write-Host "Manifest       : $Out\_photorescue_manifest.csv"
Write-Host "Interrupted? re-run the same command with -Resume"
