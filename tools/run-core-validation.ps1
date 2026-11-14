$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$vsWhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$llvmBin = "C:\Program Files\LLVM\bin"

if (-not (Test-Path -LiteralPath $vsWhere)) {
    throw "vswhere.exe was not found. Install Visual Studio Build Tools with the C++ workload."
}
if (-not (Test-Path -LiteralPath (Join-Path $llvmBin "libclang.dll"))) {
    throw "libclang.dll was not found. Install LLVM and ensure C:\Program Files\LLVM\bin exists."
}

$vsPath = & $vsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) {
    throw "A Visual Studio C++ Build Tools installation was not found."
}

$vsDevCmd = Join-Path $vsPath "Common7\Tools\VsDevCmd.bat"
if (-not (Test-Path -LiteralPath $vsDevCmd)) {
    throw "VsDevCmd.bat was not found at $vsDevCmd"
}

Push-Location $projectRoot
try {
    $command = 'call "' + $vsDevCmd + '" -arch=x64 >nul && set "LIBCLANG_PATH=' + $llvmBin + '" && set "PATH=' + $llvmBin + ';%PATH%" && cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace --no-fail-fast && cargo test -p listary-core --features test-fault-injection --test persistence_faults -- --test-threads=1 && cargo test -p listary-core --features test-fault-injection --test portable_config_faults -- --test-threads=1 && cargo test -p listary-core --features test-fault-injection --lib history::tests::history_reservation_survives_interrupted_writes_and_migration -- --exact --test-threads=1 && cargo build --workspace --release'
    & cmd.exe /d /s /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "Core validation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
