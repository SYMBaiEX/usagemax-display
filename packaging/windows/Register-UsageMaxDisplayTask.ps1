[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$CliPath,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$ConfigPath,

    [string]$TaskName = "UsageMaxDisplay"
)

$ErrorActionPreference = "Stop"
$CliPath = (Resolve-Path -LiteralPath $CliPath).Path
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$WorkingDirectory = Split-Path -Parent $ConfigPath
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction `
    -Execute $CliPath `
    -Argument ('--config "{0}"' -f $ConfigPath) `
    -WorkingDirectory $WorkingDirectory
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Runs UsageMax Display for the current user at logon." `
    -Force | Out-Null

Write-Output "Registered user task '$TaskName' for $CurrentUser."
Write-Output "Review its action and start it from Task Scheduler after confirming USB access."
