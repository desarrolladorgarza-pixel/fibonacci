# Fibonacci — Windows nativo (sin WSL)
$ErrorActionPreference = "Stop"
Write-Host "> Fibonacci" -ForegroundColor Cyan

$py = $null
foreach ($c in @("python", "python3", "py")) {
  try {
    $v = & $c -c "import sys;print(sys.version_info>=(3,11))" 2>$null
    if ($v -eq "True") { $py = $c; break }
  } catch {}
}
if (-not $py) {
  Write-Host "  x Se requiere Python 3.11+" -ForegroundColor Red
  Write-Host "    winget install Python.Python.3.12"
  exit 1
}
Write-Host "  + $py" -ForegroundColor Green

& $py -m pip install --user --upgrade fibonacci-agent

$scripts = & $py -c "import site,os;print(os.path.join(site.USER_BASE,'Scripts'))"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$scripts*") {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$scripts", "User")
  $env:Path += ";$scripts"
  Write-Host "  + PATH actualizado" -ForegroundColor Green
}

& "$scripts\fib.exe" doctor
Write-Host ""
Write-Host "  Listo:  fib" -ForegroundColor Cyan
