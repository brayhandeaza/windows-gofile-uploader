[CmdletBinding()]
param(
    [ValidateRange(1, 2147483647)][int]$Workers = 1,
    [ValidateRange(0, 2147483647)][int]$Limit = 0,
    [switch]$Once,
    [switch]$AllowLargeSource,
    [switch]$Webtor,
    [string]$Directory,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.runtime\python\python.exe'
$name = if ($Webtor) { 'upload_webtor_gofile.py' } else { 'upload_local_gofile.py' }
$script = Join-Path $PSScriptRoot ('apps\scraper\scripts\' + $name)
if (!(Test-Path -LiteralPath $python)) {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python)) { throw 'Ejecuta Setup-Windows.cmd primero para instalar las dependencias.' }
}
$arguments = @('-X', 'utf8', '-u', $script, '--workers', "$Workers", '--limit', "$Limit")
if (!$Webtor -and $Directory) { $arguments += @('--directory', $Directory) }
if ($Webtor -and $Once) { $arguments += '--once' }
if ($Webtor -and $AllowLargeSource) { $arguments += '--allow-large-source' }
if ($Check) {
    & $python -X utf8 -c "import sys; sys.path.insert(0, sys.argv[1]); import requests, certifi; import upload_webtor_gofile; print('Python y dependencias disponibles; transferencia original sin FFmpeg')" (Split-Path -Parent $script)
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $arguments += '--dry-run'
}
# Keep the computer awake while this launcher is running; restore on exit.
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class SatoruPower { [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags); }'
$result = 1
try {
    $null = [SatoruPower]::SetThreadExecutionState([uint32]2147483649)
    $ErrorActionPreference = 'Continue'
    # Inherit the console directly so Python can redraw the fixed worker panel.
    & $python @arguments
    $result = $LASTEXITCODE
} finally {
    $null = [SatoruPower]::SetThreadExecutionState([uint32]2147483648)
}
exit $result
