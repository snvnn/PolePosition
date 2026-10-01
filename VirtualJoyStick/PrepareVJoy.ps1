# Download only; installation/UAC/reboot remain interactive user actions.
$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$lock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'dependency-lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$packagePath = Join-Path $workspacePath 'target/vjoy-2.2.2.0'
New-Item -ItemType Directory -Force -Path $packagePath | Out-Null
foreach ($asset in @(@{Name='SDK.zip';Info=$lock.sdk}, @{Name='vJoySetup_v2.2.2.0_Win10_Win11.exe';Info=$lock.installer})) {
    $assetPath = Join-Path $packagePath $asset.Name
    if (-not (Test-Path -LiteralPath $assetPath)) { Invoke-WebRequest -Uri $asset.Info.url -OutFile $assetPath }
    if ((Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.Info.sha256) { throw "Pinned download hash mismatch: $assetPath" }
}
$installerPath = Join-Path $packagePath 'vJoySetup_v2.2.2.0_Win10_Win11.exe'
$signature = Get-AuthenticodeSignature -LiteralPath $installerPath
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Brunner Elektronik AG') { throw 'Installer signature verification failed' }
$sdkPath = Join-Path $packagePath 'SDK'
if (-not (Test-Path -LiteralPath $sdkPath)) { Expand-Archive -LiteralPath (Join-Path $packagePath 'SDK.zip') -DestinationPath $sdkPath }
$dllPath = Join-Path $sdkPath 'SDK/lib/x64/vJoyInterface.dll'
if ((Get-FileHash -LiteralPath $dllPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.sdk.x64_dll_sha256) { throw 'SDK x64 DLL hash mismatch' }
Write-Output "Verified installer: $installerPath"
Write-Output "Verified SDK: $dllPath"
