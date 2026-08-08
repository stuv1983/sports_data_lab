param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe = (Get-Command python).Source
)

$ErrorActionPreference = "Stop"
$zone = (Get-TimeZone).Id
if ($zone -ne "AUS Eastern Standard Time") {
    throw "This schedule must use Sydney time. Windows timezone is '$zone', not 'AUS Eastern Standard Time'."
}

$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited

function Install-SdlTask {
    param([string]$Name, [string]$Arguments, $Trigger)
    $action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ProjectDir
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Installed $Name"
}

$regular = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Friday,Saturday,Sunday,Monday -At "00:10"
Install-SdlTask "Sports Data Lab - regular database update" '-m database_updates run --event regular --sports afl nba mlb nfl --trigger scheduler' $regular

# Weekly triggers plus the Python due-date guards implement the annual rules.
$brownlow = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Tuesday -At "01:00"
Install-SdlTask "Sports Data Lab - Brownlow and awards update" '-m database_updates run --event brownlow-awards --sports afl --trigger scheduler --only-if-due' $brownlow

$grandFinal = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At "01:00"
Install-SdlTask "Sports Data Lab - Grand Final and awards update" '-m database_updates run --event grand-final-awards --sports afl --trigger scheduler --only-if-due' $grandFinal

# Gridley publishes one board a day, so this runs daily rather than on the
# Friday-to-Monday scores schedule.
$gridley = New-ScheduledTaskTrigger -Daily -At "06:30"
Install-SdlTask "Sports Data Lab - Gridley board scan" '-m database_updates gridley-scan --trigger scheduler' $gridley

Write-Host "Database update tasks use Australia/Sydney local time and run missed starts when the computer resumes."
