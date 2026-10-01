# Run elevated with ACC and all feeders closed. Final check sends neutral only.
# vJoy 2.2.2.0 driver/sys/hid.c: the built-in device uses Device00\Init;
# configured devices use DeviceNN\Init with Device00 fallback. Values are bytes (%).
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $workspacePath
$isAdmin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Administrator approval is required for vJoy driver defaults.' }
$busy = @(Get-Process | Where-Object { $_.ProcessName -match '^(AC2.*|poleposition-.*|vJoyFeeder|vJoyConf|vJoyConfig)$' })
if ($busy.Count) { throw ('Close ACC and feeders first: ' + ($busy.ProcessName -join ', ')) }
$driverPath = Join-Path $env:WINDIR 'System32/drivers/vjoy.sys'
if ((Get-Item -LiteralPath $driverPath).VersionInfo.FileVersion.Trim() -ne '2.2.2.0') { throw 'Expected vJoy 2.2.2.0.' }
$configPath = Join-Path $env:ProgramFiles 'vJoy/x64/vJoyConfig.exe'
$configuration = (& $configPath -t | Out-String)
if ($LASTEXITCODE -ne 0 -or $configuration -notmatch 'Device 1:\s+Status: FREE') { throw 'Device 1 must be free.' }
foreach ($deviceId in 2..16) {
    if ($configuration -notmatch ("Device ${deviceId}:\s+Status: MISSING")) { throw 'Only device 1 is supported by this setup script.' }
}
$pnp = @(Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -eq 'vJoy Device' -and $_.InstanceId -like 'ROOT\HIDCLASS\*' })
if ($pnp.Count -ne 1 -or $pnp[0].Status -ne 'OK') { throw 'Expected one healthy vJoy root device.' }
$resultPath = Join-Path $workspacePath ('input_profiles/neutral-defaults-' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $resultPath | Out-Null
$result = [ordered]@{
    schema_version = 1; complete = $false; registry_applied = $false; device_reenabled = $false
    axis_output_sent = $false; created_at = [DateTimeOffset]::Now.ToString('o')
    boot_time = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
    driver_sha256 = (Get-FileHash -LiteralPath $driverPath -Algorithm SHA256).Hash.ToLowerInvariant()
    pnp_instance = $pnp[0].InstanceId; configuration = $configuration
    source = 'https://github.com/BrunnerInnovation/vJoy/blob/v2.2.2.0/driver/sys/hid.c'
}
function Save-Result {
    $result | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath (Join-Path $resultPath 'result.json') -Encoding UTF8
}
function Read-Probe {
    $json = & (Join-Path $workspacePath 'target/release/poleposition-vjoy.exe') probe
    if ($LASTEXITCODE -ne 0) { throw 'Read-only device probe failed.' }
    return ($json | ConvertFrom-Json)
}
try {
    $result.before = Read-Probe
    $backup = @()
    $undo = @('Windows Registry Editor Version 5.00', '')
    foreach ($deviceKey in @('Device00', 'Device01')) {
        $relativeKey = "SYSTEM\CurrentControlSet\services\vjoy\Parameters\$deviceKey\Init"
        $registryKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($relativeKey)
        $undo += "[HKEY_LOCAL_MACHINE\$relativeKey]"
        foreach ($axis in @('Y','Z')) {
            $exists = $null -ne $registryKey -and $registryKey.GetValueNames() -contains $axis
            $prior = $null
            if ($exists) {
                if ($registryKey.GetValueKind($axis) -ne [Microsoft.Win32.RegistryValueKind]::Binary) { throw 'Unexpected initial-axis registry type; no defaults changed.' }
                $prior = [byte[]]$registryKey.GetValue($axis)
                $undo += ('"' + $axis + '"=hex:' + (($prior | ForEach-Object { $_.ToString('x2') }) -join ','))
            } else { $undo += ('"' + $axis + '"=-') }
            $backup += [ordered]@{ key=$relativeKey; name=$axis; existed=$exists; bytes=$prior }
        }
        if ($null -ne $registryKey) { $registryKey.Close() }
        $undo += ''
    }
    $backup | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $resultPath 'registry-before.json') -Encoding UTF8
    $undo | Set-Content -LiteralPath (Join-Path $resultPath 'restore-defaults.reg') -Encoding Unicode
    Save-Result
    foreach ($entry in $backup) {
        $registryKey = [Microsoft.Win32.Registry]::LocalMachine.CreateSubKey($entry.key)
        try { $registryKey.SetValue($entry.name, [byte[]]@(0), [Microsoft.Win32.RegistryValueKind]::Binary) }
        finally { $registryKey.Close() }
    }
    $result.registry_applied = $true
    Save-Result
    # Reload only the verified vJoy root device; always attempt to re-enable it.
    try { Disable-PnpDevice -InstanceId $pnp[0].InstanceId -Confirm:$false -ErrorAction Stop }
    finally {
        Enable-PnpDevice -InstanceId $pnp[0].InstanceId -Confirm:$false -ErrorAction Stop
        $result.device_reenabled = $true
        Save-Result
    }
    Start-Sleep -Seconds 2
    $result.after = Read-Probe
    $positions = @($result.after.windows_positions)
    if ($positions.Count -ne 1) { throw 'Expected exactly one Windows vJoy position.' }
    $position = $positions[0]
    $center = ($position.min[0] + $position.max[0]) / 2
    $result.new_winmm_client_neutral = [Math]::Abs($position.xyz[0] - $center) -le 4 -and [Math]::Abs($position.xyz[1] - $position.min[1]) -le 4 -and [Math]::Abs($position.xyz[2] - $position.min[2]) -le 4
    if ($result.after.status -ne 1) { throw 'Device did not return to Free status.' }
    # A newly opened WinMM client may show its own midpoint until a HID report
    # arrives. Read actual driver position after acquire, BEFORE sending neutral.
    $result.axis_output_sent = $true # Diagnostic attempts neutral send/release.
    Save-Result
    $diagnostic = & (Join-Path $workspacePath 'target/release/examples/neutral_check.exe')
    $diagnosticExit = $LASTEXITCODE
    $result.hardware_check = $diagnostic | ConvertFrom-Json
    $initial = $result.hardware_check.acquired.initial_axes
    if ($diagnosticExit -ne 0 -or $result.hardware_check.complete -ne $true -or $null -eq $initial -or $initial.Count -ne 3) { throw 'Neutral hardware diagnostic failed.' }
    # Driver's 0% initialization formula can produce raw 1 (one integer step).
    if ([Math]::Abs($initial[0] - $result.before.mapping.steering.center) -gt 1 -or [Math]::Abs($initial[1] - $result.before.mapping.throttle.min) -gt 1 -or [Math]::Abs($initial[2] - $result.before.mapping.brake.min) -gt 1) {
        throw 'Driver initial axes were not neutral before the first report.'
    }
    $result.driver_initial_neutral_verified = $true
    $result.limitations = 'Driver reactivation initial values and neutral send/release only; not ACC driving or forced-exit verification.'
    $result.complete = $true
    Save-Result
    Write-Output $resultPath
} catch {
    $result.error = $_.Exception.Message
    Save-Result
    Write-Error ($result.error + ' Evidence: ' + $resultPath)
    exit 1
}
