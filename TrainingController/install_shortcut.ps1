$ErrorActionPreference = 'Stop'
$controllerRoot = Split-Path -Parent $PSScriptRoot
$controllerPython = Join-Path $controllerRoot '.venv-training\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $controllerPython -PathType Leaf)) {
    throw 'The pinned Python training environment is missing.'
}
$controllerDesktop = [Environment]::GetFolderPath('Desktop')
$controllerLinkPath = Join-Path $controllerDesktop 'PolePosition Controller.lnk'
$controllerShell = New-Object -ComObject WScript.Shell
$controllerLink = $controllerShell.CreateShortcut($controllerLinkPath)
if ((Test-Path -LiteralPath $controllerLinkPath) -and
    (($controllerLink.TargetPath -ne $controllerPython) -or
     ($controllerLink.Arguments -ne '-m TrainingController'))) {
    throw 'An unrelated shortcut already uses this name.'
}
$controllerLink.TargetPath = $controllerPython
$controllerLink.Arguments = '-m TrainingController'
$controllerLink.WorkingDirectory = $controllerRoot
$controllerLink.Description = 'Pole Position training controller'
$controllerLink.Save()
Write-Output $controllerLinkPath
