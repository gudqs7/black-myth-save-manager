param(
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $projectRoot "src\black_myth_save_manager.py"
$manifest = Join-Path $projectRoot "packaging\app.manifest"
$icon = Join-Path $projectRoot "assets\logo.ico"
$logo = Join-Path $projectRoot "assets\logo.png"
$outputDirectory = Join-Path $projectRoot $OutputDirectory

python -m pip install --quiet --upgrade pyinstaller
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "BlackMythSaveManager" `
    --manifest $manifest `
    --icon $icon `
    --add-data "$icon;assets" `
    --add-data "$logo;assets" `
    --distpath $outputDirectory `
    $source

Write-Host "Built: $(Join-Path $outputDirectory 'BlackMythSaveManager.exe')"
