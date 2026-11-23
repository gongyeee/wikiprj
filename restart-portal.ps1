param(
  [string]$BackendHost = "0.0.0.0",
  [int]$BackendPort = 8091,
  [string]$FrontendHost = "0.0.0.0",
  [int]$FrontendPort = 8080,
  [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $Root "backend"
$FrontendDir = Join-Path $Root "frontend"
$BackendPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$BackendOutLog = Join-Path $BackendDir "backend-runserver.out.log"
$BackendErrLog = Join-Path $BackendDir "backend-runserver.err.log"
$FrontendOutLog = Join-Path $FrontendDir "frontend-vite.out.log"
$FrontendErrLog = Join-Path $FrontendDir "frontend-vite.err.log"

function Stop-ProcessOnPort {
  param(
    [int]$Port,
    [string]$Label
  )

  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  $connectionProcessIds = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
  $netstatProcessIds = @(netstat -ano | Select-String ":$Port\s+.*LISTENING\s+(\d+)\s*$" | ForEach-Object {
    if ($_.Matches.Count) { [int]$_.Matches[0].Groups[1].Value }
  })
  $processIds = @($connectionProcessIds + $netstatProcessIds | Where-Object { $_ } | Sort-Object -Unique)
  foreach ($processId in $processIds) {
    if (!$processId) {
      continue
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
      Write-Host "Stopping $Label on port ${Port}: $($process.Id) $($process.ProcessName)"
      Stop-Process -Id $process.Id -Force
    }
  }
}

function Assert-PathExists {
  param(
    [string]$Path,
    [string]$Message
  )

  if (!(Test-Path -LiteralPath $Path)) {
    throw $Message
  }
}

Set-Location $Root

Assert-PathExists $BackendDir "Missing backend directory: $BackendDir"
Assert-PathExists $BackendPython "Missing backend Python venv: $BackendPython"

if (!$NoFrontend) {
  Assert-PathExists $FrontendDir "Missing frontend directory: $FrontendDir"
  Assert-PathExists (Join-Path $FrontendDir "node_modules") "Missing frontend node_modules. Please install dependencies or use the LAN package."
}

$NpmCommand = $null
if (!$NoFrontend) {
  $NpmCommand = (Get-Command "npm.cmd" -ErrorAction SilentlyContinue).Source
  if (!$NpmCommand) {
    $NpmCommand = (Get-Command "npm" -ErrorAction SilentlyContinue).Source
  }
  if (!$NpmCommand) {
    throw "npm was not found. Please install Node.js and add it to PATH."
  }
}

Write-Host "Checking and upgrading backend database..."
Push-Location $BackendDir
try {
  & $BackendPython manage.py migrate --noinput
  if ($LASTEXITCODE -ne 0) {
    throw "Database upgrade failed. Existing services were not stopped; check database permissions and migration output."
  }
} finally {
  Pop-Location
}

Write-Host "Restarting LLM Wiki Portal..."
Stop-ProcessOnPort -Port $BackendPort -Label "backend"
if (!$NoFrontend) {
  Stop-ProcessOnPort -Port $FrontendPort -Label "frontend"
}

Write-Host "Starting backend: http://$BackendHost`:$BackendPort"
$env:DJANGO_ALLOWED_HOSTS = "*"
Start-Process `
  -FilePath $BackendPython `
  -ArgumentList "manage.py", "runserver", "$BackendHost`:$BackendPort", "--noreload" `
  -WorkingDirectory $BackendDir `
  -RedirectStandardOutput $BackendOutLog `
  -RedirectStandardError $BackendErrLog `
  -WindowStyle Hidden

if (!$NoFrontend) {
  Write-Host "Starting frontend: http://$FrontendHost`:$FrontendPort"
  Start-Process `
    -FilePath $NpmCommand `
    -ArgumentList "run", "dev", "--", "--host", $FrontendHost, "--port", "$FrontendPort" `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput $FrontendOutLog `
    -RedirectStandardError $FrontendErrLog `
    -WindowStyle Hidden
}

Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Restart requested."
Write-Host "Backend health: http://127.0.0.1:$BackendPort/api/health/"
if (!$NoFrontend) {
  Write-Host "Local frontend: http://127.0.0.1:$FrontendPort/"
  Write-Host "LAN frontend: http://<server-lan-ip>:$FrontendPort/"
}
Write-Host "Backend stdout log: $BackendOutLog"
Write-Host "Backend stderr log: $BackendErrLog"
if (!$NoFrontend) {
  Write-Host "Frontend stdout log: $FrontendOutLog"
  Write-Host "Frontend stderr log: $FrontendErrLog"
}
