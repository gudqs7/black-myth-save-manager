$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolchain = "D:\code\ue4ss-mods\.tools\w64devkit\bin"
$gpp = Join-Path $toolchain "g++.exe"
if (-not (Test-Path $gpp)) {
    $gpp = "g++.exe"
}
$dll = Join-Path $root "pbz_bridge.dll"
$compilerArgs = @(
    "-std=c++17",
    "-O2",
    "-DNDEBUG",
    "-s",
    "-shared",
    "-static",
    "-static-libgcc",
    "-static-libstdc++",
    "-Wl,--no-insert-timestamp",
    (Join-Path $root "pbz_bridge.cpp"),
    "-o",
    $dll,
    "-lbcrypt",
    "-lcomctl32"
)

& $gpp @compilerArgs
if ($LASTEXITCODE -ne 0) {
    throw "build failed"
}
Write-Host "Built: $dll"
