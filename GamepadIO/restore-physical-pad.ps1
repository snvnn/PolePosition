# Run after stopping autonomous training (F8).
$ErrorActionPreference='Stop'
if (Get-Process poleposition-gamepad -ErrorAction SilentlyContinue) { throw 'Stop training with F8 before reconnecting the physical pad.' }
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"')
    exit
}
Enable-PnpDevice -InstanceId 'USB\VID_045E&PID_0B12\3039373030343039313433333039' -Confirm:$false -ErrorAction Stop
