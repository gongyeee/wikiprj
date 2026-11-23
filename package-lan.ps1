param(
  [string]$OutputDir = ".",
  [string]$Password = "123",
  [ValidateSet("ZipCrypto", "AES256")]
  [string]$Encryption = "ZipCrypto",
  [switch]$IncludeUserData,
  [switch]$IncludeModelRegistry,
  [switch]$KeepStage
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step([string]$Message) {
  Write-Host "==> $Message"
}

# --- 1. 前置校验 -------------------------------------------------------------
$requiredPaths = @(
  "README.md",
  "API_README.md",
  "PROJECT_HANDOFF.md",
  "LAN_START.md",
  "backend",
  "backend\.env.example",
  "frontend",
  "frontend\node_modules"
)

foreach ($path in $requiredPaths) {
  if (!(Test-Path -LiteralPath $path)) {
    throw "Missing required path: $path"
  }
}

$usePassword = -not [string]::IsNullOrEmpty($Password)

$sevenZip = $null
$candidates = @(
  "C:\Program Files\7-Zip\7z.exe",
  "C:\Program Files (x86)\7-Zip\7z.exe",
  (Join-Path $env:LOCALAPPDATA "Programs\7-Zip\7z.exe")
)
foreach ($candidate in $candidates) {
  if (Test-Path -LiteralPath $candidate) { $sevenZip = $candidate; break }
}
if (-not $sevenZip) {
  $cmd = Get-Command 7z.exe -ErrorAction SilentlyContinue
  if ($cmd) { $sevenZip = $cmd.Source }
}
if ($usePassword -and -not $sevenZip) {
  throw "需要 7-Zip 才能给压缩包加密码。请安装 7-Zip (https://www.7-zip.org/)，或显式传 -Password '' 生成无密码包。"
}

if (!(Test-Path -LiteralPath $OutputDir)) {
  New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$packageName = "llm-wiki-portal-lan-$stamp"
$stage = Join-Path $Root $packageName
$zipPath = Join-Path (Resolve-Path -LiteralPath $OutputDir) "$packageName.zip"

if (Test-Path -LiteralPath $stage) {
  Remove-Item -LiteralPath $stage -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
  Remove-Item -LiteralPath $zipPath -Force
}

# --- 2. 复制文件 -------------------------------------------------------------
Write-Step "Creating staging directory: $stage"
New-Item -ItemType Directory -Path $stage | Out-Null

Write-Step "Copying project files..."
Copy-Item -LiteralPath README.md, API_README.md, PROJECT_HANDOFF.md, LAN_START.md, AUDIT_RETENTION.md, restart-portal.ps1 -Destination $stage
# 后端虚拟环境在部署机器上安装，不复制进暂存目录。
$stagedBackend = Join-Path $stage "backend"
New-Item -ItemType Directory -Path $stagedBackend | Out-Null
Get-ChildItem -LiteralPath (Join-Path $Root "backend") -Force |
  Where-Object { $_.Name -ne ".venv" } |
  Copy-Item -Destination $stagedBackend -Recurse -Force
Copy-Item -LiteralPath frontend -Destination $stage -Recurse

# --- 3. 排除敏感 / 无用文件 ---------------------------------------------------
# 注意：Remove-Item -LiteralPath 不展开通配符，带 * 的路径必须用 -Path。
Write-Step "Removing sensitive/irrelevant files..."

$alwaysRemove = @(
  "backend\.env",                                  # 含 LLM key / token
  "backend\*.log",                                 # runserver 日志
  "frontend\*.log",                                # vite 日志
  "backend\*.sqlite3",                             # 运行库
  "backend\backups",                               # 数据库快照
  "backend\logs",                                  # 运行和任务日志，不随包分发
  "backend\data\domain_layers",
  "backend\data\wiki_layer_jobs",
  ".git",
  ".agents",
  "frontend\node_modules\.vite",
  "frontend\node_modules\.vite-temp",
  "frontend\node_modules\.cache"
)

# 用户上传内容 / 项目登记，默认不随包分发
$userDataPaths = @(
  "backend\data\upload-projects",
  "backend\data\upload-staging",
  "backend\data\manual_wiki_projects.json"
)

foreach ($item in $alwaysRemove) {
  $target = Join-Path $stage $item
  Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue
}

if (-not $IncludeUserData) {
  foreach ($item in $userDataPaths) {
    Remove-Item -Path (Join-Path $stage $item) -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# 兜底：清掉任意层级的 *.log（.venv / node_modules 里可能有正常日志文件，跳过）
Get-ChildItem -LiteralPath $stage -Recurse -Force -File -Filter "*.log" -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch '\\(\.venv|node_modules)\\' } |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

# llm_models.json 里是明文 apiKey；缺失时后端会回落到 .env 的默认模型，因此默认剔除。
if (-not $IncludeModelRegistry) {
  Remove-Item -Path (Join-Path $stage "backend\data\llm_models.json") -Force -ErrorAction SilentlyContinue
} else {
  Write-Warning "IncludeModelRegistry 已开启：llm_models.json 内含明文 apiKey，请确认交付对象可信。"
}

Write-Step "Cleaning caches..."
Get-ChildItem -LiteralPath (Join-Path $stage "backend") -Recurse -Force -Directory -Filter "__pycache__" `
  -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
foreach ($cache in @("backend\.pytest_cache", "backend\.mypy_cache", "frontend\.vite-temp")) {
  Remove-Item -Path (Join-Path $stage $cache) -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 4. 打包前再扫一遍密钥（避免默认值被改坏后把密钥打进去）-------------------
Write-Step "Scanning staged tree for secrets..."
$secretHits = Get-ChildItem -LiteralPath $stage -Recurse -Force -File -Include *.json, *.py, *.md, *.txt, *.env* `
    -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch '\\(\.venv|node_modules)\\' } |
  Select-String -Pattern '"apiKey"\s*:\s*"[^"]*sk-|LLM_API_KEY\s*=\s*sk-|BEGIN [A-Z ]*PRIVATE KEY' `
    -List -ErrorAction SilentlyContinue

if ($secretHits) {
  $list = ($secretHits | ForEach-Object { "  " + $_.Path + ":" + $_.LineNumber }) -join [Environment]::NewLine
  throw "Staged package still contains secrets:" + [Environment]::NewLine + $list
}

# --- 5. 压缩（带密码）--------------------------------------------------------
$stagedFileCount = (Get-ChildItem -LiteralPath $stage -Recurse -Force -File).Count

Write-Step "Compressing to zip: $zipPath"
if ($usePassword) {
  Write-Step "Encryption: $Encryption, password length $($Password.Length)"
}

if ($sevenZip) {
  $sevenZipArgs = @("a", "-tzip", "-mx=5", $zipPath, $packageName)
  if ($usePassword) {
    $sevenZipArgs = @("a", "-tzip", "-mx=5")
    if ($Encryption -eq "AES256") { $sevenZipArgs += "-mem=AES256" }
    $sevenZipArgs += @("-p$Password", $zipPath, $packageName)
  }
  & $sevenZip $sevenZipArgs | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    throw "7-Zip failed with exit code $LASTEXITCODE. Zip removed."
  }
} else {
  # 未找到 7z 且未要求加密时的兜底
  Compress-Archive -Path $stage -DestinationPath $zipPath -CompressionLevel Optimal
}

# --- 6. 校验压缩包（完整性 / 文件数 / 加密是否生效）--------------------------
if ($sevenZip) {
  Write-Step "Verifying archive..."
  $testArgs = @("t", $zipPath)
  if ($usePassword) { $testArgs = @("t", "-p$Password", $zipPath) }
  $testOutput = & $sevenZip $testArgs 2>&1
  $testExit = $LASTEXITCODE
  if ($testExit -ne 0) {
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    throw "Archive integrity test failed (exit $testExit), zip removed."
  }

  $listOutput = (& $sevenZip l $zipPath) -join [Environment]::NewLine
  $summary = [regex]::Match($listOutput, '(\d+)\s+files?,\s+(\d+)\s+folders?')
  if (-not $summary.Success) {
    throw "Could not parse archive listing."
  }
  $zippedFileCount = [int]$summary.Groups[1].Value
  if ($zippedFileCount -ne $stagedFileCount) {
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    throw "Archive file count mismatch: staged=$stagedFileCount, zipped=$zippedFileCount. Zip removed."
  }

  if ($usePassword) {
    # 普通列表不显示加密状态，必须用 -slt 查看 "Encrypted = +"
    $sltOutput = (& $sevenZip l -slt $zipPath) -join [Environment]::NewLine
    $hasEncrypted = $sltOutput -match 'Encrypted\s*=\s*\+'
    if (-not $hasEncrypted) {
      Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
      throw "Archive is not encrypted although a password was requested. Zip removed."
    }
  }
}

# --- 7. 收尾 ---------------------------------------------------------------
if (-not $KeepStage) {
  Write-Step "Removing staging directory..."
  Remove-Item -LiteralPath $stage -Recurse -Force
}

$zip = Get-Item -LiteralPath $zipPath
$sizeMb = [Math]::Round($zip.Length / 1MB, 2)
Write-Host ""
Write-Host "Package done:"
Write-Host $zip.FullName
Write-Host "Size: $sizeMb MB  ($stagedFileCount files)"
if ($usePassword) {
  Write-Host "Password: $Password  (7-Zip / WinRAR / macOS 可解压；Windows 资源管理器仅支持 ZipCrypto)"
} else {
  Write-Host "Password: (none)"
}
