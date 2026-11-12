param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $projectRoot "src\black_myth_save_manager.py"
$manifest = Join-Path $projectRoot "packaging\app.manifest"
$versionInfo = Join-Path $projectRoot "packaging\version_info.txt"
$icon = Join-Path $projectRoot "assets\logo.ico"
$logo = Join-Path $projectRoot "assets\logo.png"
$pbzBridge = Join-Path $projectRoot "native\pbz_bridge\pbz_bridge.dll"
$pbzOodle = Join-Path $projectRoot "native\pbz_bridge\oo2core_9_win64.dll"
$outputDirectory = Join-Path $projectRoot $OutputDirectory

$bridgeSource = Join-Path $projectRoot "native\pbz_bridge\pbz_bridge.cpp"
$needBridgeBuild = -not (Test-Path $pbzBridge)
if (-not $needBridgeBuild) {
    $bridgeTime = (Get-Item $pbzBridge).LastWriteTimeUtc
    $needBridgeBuild = (Get-Item $bridgeSource).LastWriteTimeUtc -gt $bridgeTime
}
if ($needBridgeBuild) {
    & (Join-Path $projectRoot "native\pbz_bridge\build.ps1")
}
if (-not (Test-Path $pbzBridge) -or -not (Test-Path $pbzOodle)) {
    throw "PBZ bridge assets are missing."
}
python -m pip install --quiet --upgrade pyinstaller
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "BlackMythSaveManager" `
    --manifest $manifest `
    --version-file $versionInfo `
    --icon $icon `
    --add-data "$icon;assets" `
    --add-data "$logo;assets" `
    --add-binary "$pbzBridge;native" `
    --add-binary "$pbzOodle;native" `
    --distpath $outputDirectory `
    $source

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}
Write-Host "Built: $(Join-Path $outputDirectory 'BlackMythSaveManager.exe')"
