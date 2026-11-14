$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$vsWhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
$llvmBin = "C:\Program Files\LLVM\bin"

$vsPath = & $vsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) {
    throw "A Visual Studio C++ Build Tools installation was not found."
}
$vsDevCmd = Join-Path $vsPath "Common7\Tools\VsDevCmd.bat"

function Invoke-CoreBenchmark {
    param(
        [int]$Items,
        [int]$Updates,
        [int]$Queries
    )

    $command = 'call "' + $vsDevCmd + '" -arch=x64 >nul && set "LIBCLANG_PATH=' + $llvmBin + '" && set "PATH=' + $llvmBin + ';%PATH%" && cargo run --release -p listary-core --example benchmark_core -- --items ' + $Items + ' --updates ' + $Updates + ' --queries ' + $Queries
    & cmd.exe /d /s /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "Benchmark failed for $Items items with exit code $LASTEXITCODE"
    }
}

Push-Location $projectRoot
try {
    Invoke-CoreBenchmark -Items 100000 -Updates 5000 -Queries 200
    Invoke-CoreBenchmark -Items 1000000 -Updates 10000 -Queries 100
}
finally {
    Pop-Location
}
