param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ListaryMac\bin"),
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA "ListaryMac\state"),
    [string]$StartupDir = [Environment]::GetFolderPath("Startup"),
    [string]$UiLauncher = "",
    [switch]$RemoveState
)

$ErrorActionPreference = "Stop"

$install = [System.IO.Path]::GetFullPath($InstallRoot)
$state = [System.IO.Path]::GetFullPath($StateDir)
$startup = [System.IO.Path]::GetFullPath($StartupDir)
$installedExe = Join-Path $install "listary-agent.exe"
$installMarker = Join-Path $install "listary-agent.install.json"
$launcher = Join-Path $startup "Spiffy Listary Agent.vbs"
$endpoint = Join-Path $state "integration-endpoint-v1.json"
$requestedUiLauncher = $null
if ($UiLauncher.Trim()) {
    $requestedUiLauncher = [System.IO.Path]::GetFullPath($UiLauncher)
}

function Get-AgentEndpointOwnerProcess([string]$endpointPath) {
    if (-not (Test-Path -LiteralPath $endpointPath -PathType Leaf)) {
        return $null
    }
    try {
        $descriptor = Get-Content -LiteralPath $endpointPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Listary agent endpoint is unreadable; refusing to act on an unproven resident: $endpointPath"
    }
    $pidValue = $descriptor.process_id -as [int]
    if ([int]$descriptor.schema_version -ne 1 -or [int]$descriptor.protocol_version -ne 1 -or -not $pidValue -or $pidValue -le 0) {
        throw "Listary agent endpoint has an unsupported format; refusing to act on it: $endpointPath"
    }
    return Get-Process -Id $pidValue -ErrorAction SilentlyContinue
}

$normalizedInstallForMutex = $install.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar).ToUpperInvariant()
$mutexHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $mutexDigest = $mutexHasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($normalizedInstallForMutex))
}
finally {
    $mutexHasher.Dispose()
}
$transactionMutexName = "Local\SpiffyListaryInstaller-" + ([System.BitConverter]::ToString($mutexDigest).Replace("-", ""))
$transactionMutex = [System.Threading.Mutex]::new($false, $transactionMutexName)
$transactionMutexOwned = $false
try {
    $transactionMutexOwned = $transactionMutex.WaitOne(0)
}
catch [System.Threading.AbandonedMutexException] {
    $transactionMutexOwned = $true
}
if (-not $transactionMutexOwned) {
    $transactionMutex.Dispose()
    throw "Another Listary installation transaction is active; refusing concurrent uninstall state changes: $install"
}

try {
$ownedInstall = $false
if (Test-Path -LiteralPath $installMarker -PathType Leaf) {
    try {
        $marker = Get-Content -LiteralPath $installMarker -Raw | ConvertFrom-Json
    }
    catch {
        throw "Listary install marker is unreadable; refusing recursive removal: $installMarker"
    }
    if (
        [int]$marker.schema_version -ne 1 -or
        [System.IO.Path]::GetFullPath([string]$marker.install_root) -ne $install -or
        [System.IO.Path]::GetFullPath([string]$marker.state_dir) -ne $state -or
        [System.IO.Path]::GetFullPath([string]$marker.executable) -ne $installedExe -or
        [System.IO.Path]::GetFullPath([string]$marker.startup_launcher) -ne $launcher
    ) {
        throw "Listary install marker does not match the requested uninstall paths; refusing recursive removal: $installMarker"
    }
    if (-not ($marker.PSObject.Properties.Name -contains "executable_sha256")) {
        throw "Listary install marker predates executable identity binding; refusing recursive removal until the installation is upgraded through the current installer: $installMarker"
    }
    $expectedExecutableHash = ([string]$marker.executable_sha256).Trim().ToUpperInvariant()
    if (
        $expectedExecutableHash -notmatch '^[0-9A-F]{64}$' -or
        -not (Test-Path -LiteralPath $installedExe -PathType Leaf)
    ) {
        throw "Listary install marker has no verifiable accepted executable identity; refusing recursive removal: $installMarker"
    }
    $currentExecutableHash = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($currentExecutableHash -ne $expectedExecutableHash) {
        throw "Installed Listary agent bytes no longer match the accepted install marker; refusing to remove newer or unqualified state: $installedExe"
    }
    $ownedInstall = $true
    $recordedUiLauncher = [string]$marker.ui_launcher
    if ($recordedUiLauncher) {
        $recordedUiLauncher = [System.IO.Path]::GetFullPath($recordedUiLauncher)
        if ($requestedUiLauncher -and $requestedUiLauncher -ne $recordedUiLauncher) {
            throw "Listary UI launcher does not match the install marker; refusing to execute it: $recordedUiLauncher"
        }
    }
}

if ($requestedUiLauncher -and (Test-Path -LiteralPath $requestedUiLauncher -PathType Leaf)) {
    & $requestedUiLauncher --quit-resident --state-dir $state | Out-Null
}

if ((Test-Path -LiteralPath $installedExe -PathType Leaf) -and (Test-Path -LiteralPath $endpoint)) {
    $residentProcess = Get-AgentEndpointOwnerProcess $endpoint
    if ($residentProcess) {
        try {
            $residentPath = [System.IO.Path]::GetFullPath($residentProcess.Path)
        }
        catch {
            throw "Could not verify the live Listary agent executable path for PID $($residentProcess.Id); refusing uninstall state changes."
        }
        if ($residentPath -ne $installedExe) {
            throw "Listary endpoint is owned by a different executable; refusing uninstall state changes: PID $($residentProcess.Id), $residentPath"
        }
    }

    & $installedExe --state-dir $state --stop | Out-Null
    if ($LASTEXITCODE -ne 0 -and $residentProcess) {
        throw "Live Listary agent did not accept the uninstall stop request: PID $($residentProcess.Id)"
    }

    for ($i = 0; $i -lt 50; $i++) {
        $endpointStillPresent = Test-Path -LiteralPath $endpoint -PathType Leaf
        $residentStillRunning = $residentProcess -and (Get-Process -Id $residentProcess.Id -ErrorAction SilentlyContinue)
        if (-not $endpointStillPresent -and -not $residentStillRunning) {
            break
        }
        Start-Sleep -Milliseconds 100
    }

    if ($residentProcess -and (Get-Process -Id $residentProcess.Id -ErrorAction SilentlyContinue)) {
        throw "Live Listary agent did not exit after the uninstall stop request: PID $($residentProcess.Id)"
    }
    if (Test-Path -LiteralPath $endpoint -PathType Leaf) {
        $remainingOwner = Get-AgentEndpointOwnerProcess $endpoint
        if ($remainingOwner) {
            throw "Listary endpoint is still owned after the uninstall stop request: PID $($remainingOwner.Id), $endpoint"
        }
        Remove-Item -LiteralPath $endpoint -Force
    }
}

if (Test-Path -LiteralPath $launcher -PathType Leaf) {
    Remove-Item -LiteralPath $launcher -Force
}
if ($ownedInstall) {
    Remove-Item -LiteralPath $install -Recurse -Force
    if (Test-Path -LiteralPath $install) {
        throw "Listary install root still exists after removal; refusing to report uninstall success: $install"
    }
}
elseif (Test-Path -LiteralPath $installedExe -PathType Leaf) {
    # Backward-compatible cleanup for a pre-marker install: remove only the
    # known binary and never recursively delete an unproven directory.
    Remove-Item -LiteralPath $installedExe -Force
    if ((Test-Path -LiteralPath $install -PathType Container) -and -not (Get-ChildItem -LiteralPath $install -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $install -Force
    }
}

if ($RemoveState) {
    if (-not $ownedInstall) {
        throw "Cannot safely remove Listary state without a matching install marker: $installMarker"
    }
    if (Test-Path -LiteralPath $state) {
        Remove-Item -LiteralPath $state -Recurse -Force
    }
    if (Test-Path -LiteralPath $state) {
        throw "Listary state directory still exists after removal; refusing to report uninstall success: $state"
    }
    Write-Output "Removed Listary agent and state: $state"
}
else {
    Write-Output "Removed Listary agent. Preserved state: $state"
}
}
finally {
    if ($transactionMutexOwned) {
        $transactionMutex.ReleaseMutex()
    }
    $transactionMutex.Dispose()
}
