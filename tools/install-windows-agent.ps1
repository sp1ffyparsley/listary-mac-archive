param(
    [string]$SourceBinary = (Join-Path $PSScriptRoot "..\target\release\listary-agent.exe"),
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ListaryMac\bin"),
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA "ListaryMac\state"),
    [string]$StartupDir = [Environment]::GetFolderPath("Startup"),
    [string]$UiLauncher = "",
    [string]$WorkspaceCatalog = (Join-Path $PSScriptRoot "..\..\..\workspace_catalog.json"),
    [switch]$NoWorkspaceCatalog,
    [switch]$DisableUiHotkey,
    [switch]$EnableUiHotkey,
    [switch]$DisableHttpApi,
    [switch]$EnableHttpApi,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$expectedSelfCheckSchema = "spiffy_listary_agent_self_check"
$expectedSelfCheckSchemaVersion = 1
$expectedAgentProtocolVersion = 1
$expectedCoreSchemaVersion = 1

$source = [System.IO.Path]::GetFullPath($SourceBinary)
$install = [System.IO.Path]::GetFullPath($InstallRoot)
$state = [System.IO.Path]::GetFullPath($StateDir)
$startup = [System.IO.Path]::GetFullPath($StartupDir)
$workspaceCatalogExplicit = $PSBoundParameters.ContainsKey("WorkspaceCatalog")
$noWorkspaceCatalogExplicit = $PSBoundParameters.ContainsKey("NoWorkspaceCatalog") -and [bool]$NoWorkspaceCatalog
if ($workspaceCatalogExplicit -and $noWorkspaceCatalogExplicit) {
    throw "Use either -WorkspaceCatalog or -NoWorkspaceCatalog, not both."
}
if ($DisableUiHotkey -and $EnableUiHotkey) {
    throw "Use either -DisableUiHotkey or -EnableUiHotkey, not both."
}
if ($DisableHttpApi -and $EnableHttpApi) {
    throw "Use either -DisableHttpApi or -EnableHttpApi, not both."
}
$uiHotkeyExplicit = $PSBoundParameters.ContainsKey("DisableUiHotkey") -or $PSBoundParameters.ContainsKey("EnableUiHotkey")
$uiHotkeyEnabled = -not [bool]$DisableUiHotkey
$httpApiExplicit = $PSBoundParameters.ContainsKey("DisableHttpApi") -or $PSBoundParameters.ContainsKey("EnableHttpApi")
$httpApiEnabled = [bool]$EnableHttpApi
$requestedWorkspaceCatalog = $null
if (-not $NoWorkspaceCatalog) {
    $requestedWorkspaceCatalog = [System.IO.Path]::GetFullPath($WorkspaceCatalog)
}
$installedExe = Join-Path $install "listary-agent.exe"
$installMarker = Join-Path $install "listary-agent.install.json"
$stagedExe = Join-Path $install "listary-agent.next.exe"
$rollbackExe = Join-Path $install "listary-agent.rollback.exe"
$launcher = Join-Path $startup "Spiffy Listary Agent.vbs"
$endpoint = Join-Path $state "integration-endpoint-v1.json"
$uiEndpoint = Join-Path $state "ui-endpoint-v1.txt"
$requestedUiLauncher = $null
if ($UiLauncher.Trim()) {
    $requestedUiLauncher = [System.IO.Path]::GetFullPath($UiLauncher)
    if (-not (Test-Path -LiteralPath $requestedUiLauncher -PathType Leaf)) {
        throw "Spiffy Listary UI launcher was not found: $requestedUiLauncher"
    }
}

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Release agent binary was not found: $source"
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
    throw "Another Listary installation transaction is active; refusing concurrent install state changes: $install"
}

try {
$ownedInstall = $false
if (Test-Path -LiteralPath $installMarker -PathType Leaf) {
    try {
        $marker = Get-Content -LiteralPath $installMarker -Raw | ConvertFrom-Json
    }
    catch {
        throw "Listary install marker is unreadable; refusing to overwrite this directory: $installMarker"
    }
    if (
        [int]$marker.schema_version -ne 1 -or
        [System.IO.Path]::GetFullPath([string]$marker.install_root) -ne $install -or
        [System.IO.Path]::GetFullPath([string]$marker.state_dir) -ne $state -or
        [System.IO.Path]::GetFullPath([string]$marker.executable) -ne $installedExe -or
        [System.IO.Path]::GetFullPath([string]$marker.startup_launcher) -ne $launcher
    ) {
        throw "Listary install marker does not match the requested install paths; refusing to overwrite this directory: $installMarker"
    }
    if (-not ($marker.PSObject.Properties.Name -contains "executable_sha256")) {
        throw "Listary install marker predates executable identity binding; refusing to use unqualified bytes as rollback authority: $installMarker"
    }
    $acceptedExecutableHash = ([string]$marker.executable_sha256).Trim().ToUpperInvariant()
    if (
        $acceptedExecutableHash -notmatch '^[0-9A-F]{64}$' -or
        -not (Test-Path -LiteralPath $installedExe -PathType Leaf)
    ) {
        throw "Listary install marker has no verifiable accepted executable identity; refusing to use it as rollback authority: $installMarker"
    }
    $currentInstalledHash = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($currentInstalledHash -ne $acceptedExecutableHash) {
        throw "Installed Listary agent bytes no longer match the accepted install marker; refusing to overwrite or restore drifted rollback state: $installedExe"
    }
    $ownedInstall = $true
    $markerHasWorkspaceCatalog = $marker.PSObject.Properties.Name -contains "workspace_catalog"
    if (-not $workspaceCatalogExplicit -and -not $noWorkspaceCatalogExplicit -and $markerHasWorkspaceCatalog) {
        $existingWorkspaceCatalog = [string]$marker.workspace_catalog
        $requestedWorkspaceCatalog = if ($existingWorkspaceCatalog) {
            [System.IO.Path]::GetFullPath($existingWorkspaceCatalog)
        } else {
            $null
        }
    }
    if (-not $httpApiExplicit -and ($marker.PSObject.Properties.Name -contains "http_api_enabled")) {
        $httpApiEnabled = [bool]$marker.http_api_enabled
    }
    $existingUiLauncher = [string]$marker.ui_launcher
    if ($existingUiLauncher) {
        $existingUiLauncher = [System.IO.Path]::GetFullPath($existingUiLauncher)
        if ($requestedUiLauncher -and $requestedUiLauncher -ne $existingUiLauncher) {
            throw "Installed Listary UI launcher does not match the requested launcher: $existingUiLauncher"
        }
        if (-not $requestedUiLauncher) {
            $requestedUiLauncher = $existingUiLauncher
        }
        if (-not $uiHotkeyExplicit -and ($marker.PSObject.Properties.Name -contains "ui_hotkey_enabled")) {
            $uiHotkeyEnabled = [bool]$marker.ui_hotkey_enabled
        }
    }
}
$newInstall = -not $ownedInstall

if ($requestedUiLauncher -and -not (Test-Path -LiteralPath $requestedUiLauncher -PathType Leaf)) {
    throw "Spiffy Listary UI launcher is no longer available: $requestedUiLauncher"
}
if ($EnableUiHotkey -and -not $requestedUiLauncher) {
    throw "-EnableUiHotkey requires -UiLauncher or an installed UI launcher."
}
if ($requestedWorkspaceCatalog -and -not (Test-Path -LiteralPath $requestedWorkspaceCatalog -PathType Leaf)) {
    throw "Workspace catalog was not found: $requestedWorkspaceCatalog. Use -NoWorkspaceCatalog for a standalone agent."
}

if ((Test-Path -LiteralPath $launcher -PathType Leaf) -and -not $ownedInstall) {
    throw "Startup launcher already exists but is not owned by this Listary installation: $launcher"
}
if (-not $ownedInstall) {
    if ((Test-Path -LiteralPath $install -PathType Container) -and (Get-ChildItem -LiteralPath $install -Force | Select-Object -First 1)) {
        throw "Install directory is not empty and has no matching Listary ownership marker: $install"
    }
}
New-Item -ItemType Directory -Force -Path $install, $state, $startup | Out-Null

function Get-AgentEndpointOwnerProcess([string]$endpointPath) {
    if (-not (Test-Path -LiteralPath $endpointPath -PathType Leaf)) {
        return $null
    }
    try {
        $descriptor = Get-Content -LiteralPath $endpointPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Listary agent endpoint is unreadable; refusing to remove an unproven descriptor: $endpointPath"
    }
    $pidValue = $descriptor.process_id -as [int]
    if ([int]$descriptor.schema_version -ne 1 -or [int]$descriptor.protocol_version -ne 1 -or -not $pidValue -or $pidValue -le 0) {
        throw "Listary agent endpoint has an unsupported format; refusing to remove it: $endpointPath"
    }
    return Get-Process -Id $pidValue -ErrorAction SilentlyContinue
}

function Get-UiEndpointOwnerProcess([string]$endpointPath) {
    if (-not (Test-Path -LiteralPath $endpointPath -PathType Leaf)) {
        return $null
    }
    try {
        $descriptor = (Get-Content -LiteralPath $endpointPath -Raw).Trim()
        $parts = @($descriptor -split '\|')
    }
    catch {
        return $null
    }
    $residentPid = if ($parts.Count -eq 4 -and $parts[0] -eq 'v1') { $parts[3] -as [int] } else { $null }
    if (-not $residentPid -or $residentPid -le 0) {
        return $null
    }
    return Get-Process -Id $residentPid -ErrorAction SilentlyContinue
}

function Start-AgentAndWaitReady([string]$binary, [string]$statePath, [string]$workspaceCatalogPath, [bool]$enableHttpApi) {
    $agentArguments = @('--state-dir', ([char]34 + $statePath + [char]34))
    if ($workspaceCatalogPath) {
        $agentArguments += @('--workspace-catalog', ([char]34 + $workspaceCatalogPath + [char]34))
    }
    if ($enableHttpApi) {
        $agentArguments += '--http-api'
    }
    Start-Process -FilePath $binary -ArgumentList $agentArguments -WindowStyle Hidden
    $statusArguments = @('--state-dir', ([char]34 + $statePath + [char]34), '--status')
    for ($i = 0; $i -lt 50; $i++) {
        Start-Sleep -Milliseconds 100
        if (Test-Path -LiteralPath $endpoint -PathType Leaf) {
            try {
                $statusProcess = Start-Process -FilePath $binary -ArgumentList $statusArguments -WindowStyle Hidden -Wait -PassThru
                if ($statusProcess.ExitCode -eq 0) {
                    return $true
                }
            }
            catch {
                # A replacement that cannot execute a status probe is not ready.
            }
        }
    }
    return $false
}

function Write-AtomicText([string]$path, [string]$content) {
    $parent = Split-Path -Parent $path
    $name = Split-Path -Leaf $path
    $temp = Join-Path $parent (".{0}.{1}.tmp" -f $name, [guid]::NewGuid().ToString('N'))
    $replaceBackup = Join-Path $parent (".{0}.{1}.replace-backup" -f $name, [guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllText($temp, $content, [System.Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [System.IO.File]::Replace($temp, $path, $replaceBackup)
            Remove-Item -LiteralPath $replaceBackup -Force -ErrorAction SilentlyContinue
        }
        else {
            [System.IO.File]::Move($temp, $path)
        }
    }
    finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $replaceBackup -Force -ErrorAction SilentlyContinue
    }
}

function Write-AtomicBytes([string]$path, [byte[]]$content) {
    $parent = Split-Path -Parent $path
    $name = Split-Path -Leaf $path
    $temp = Join-Path $parent (".{0}.{1}.tmp" -f $name, [guid]::NewGuid().ToString('N'))
    $replaceBackup = Join-Path $parent (".{0}.{1}.replace-backup" -f $name, [guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllBytes($temp, $content)
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [System.IO.File]::Replace($temp, $path, $replaceBackup)
            Remove-Item -LiteralPath $replaceBackup -Force -ErrorAction SilentlyContinue
        }
        else {
            [System.IO.File]::Move($temp, $path)
        }
    }
    finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $replaceBackup -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-AgentSelfCheck([string]$binary) {
    $operationId = [guid]::NewGuid().ToString("N")
    $stdoutPath = Join-Path $install (".listary-agent-self-check.{0}.stdout" -f $operationId)
    $stderrPath = Join-Path $install (".listary-agent-self-check.{0}.stderr" -f $operationId)
    $process = $null
    try {
        $process = Start-Process -FilePath $binary -ArgumentList @("--self-check") -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        if (-not $process.WaitForExit(5000)) {
            try {
                $process.Kill()
                $process.WaitForExit()
            }
            catch {
                throw "staged Listary self-check timed out and its exact validation process could not be retired: PID $($process.Id)"
            }
            throw "staged Listary self-check timed out after 5000 ms"
        }
        # Windows PowerShell can leave ExitCode unset after only the timed
        # overload even when it returned true. The zero-time follow-up is
        # immediate for an exited process and makes the exit observation
        # reliable on both Windows PowerShell 5.1 and PowerShell 7.
        $process.WaitForExit()
        $selfCheckExitCode = [int]$process.ExitCode
        if ($selfCheckExitCode -ne 0) {
            throw "staged Listary self-check returned exit code $selfCheckExitCode"
        }
        $stdoutLength = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) { (Get-Item -LiteralPath $stdoutPath).Length } else { 0 }
        $stderrLength = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) { (Get-Item -LiteralPath $stderrPath).Length } else { 0 }
        if ($stdoutLength -le 0 -or $stdoutLength -gt 65536 -or $stderrLength -gt 65536) {
            throw "staged Listary self-check output exceeded its bounded contract"
        }
        $raw = Get-Content -LiteralPath $stdoutPath -Raw
        try {
            $report = $raw | ConvertFrom-Json
        }
        catch {
            throw "staged Listary self-check did not return valid JSON"
        }
        $properties = @($report.PSObject.Properties.Name | Sort-Object)
        $expectedProperties = @(
            "agent_protocol_version",
            "binary_role",
            "core_schema_version",
            "package_version",
            "persistent_state_accessed",
            "resident_started",
            "schema",
            "schema_version",
            "supports_shutdown",
            "supports_status"
        )
        if (
            (Compare-Object -ReferenceObject $expectedProperties -DifferenceObject $properties) -or
            [string]$report.schema -ne $expectedSelfCheckSchema -or
            [int]$report.schema_version -ne $expectedSelfCheckSchemaVersion -or
            [string]$report.binary_role -ne "listary-agent" -or
            [int]$report.agent_protocol_version -ne $expectedAgentProtocolVersion -or
            [int]$report.core_schema_version -ne $expectedCoreSchemaVersion -or
            -not [string]$report.package_version -or
            [bool]$report.persistent_state_accessed -or
            [bool]$report.resident_started -or
            -not [bool]$report.supports_status -or
            -not [bool]$report.supports_shutdown
        ) {
            throw "staged Listary self-check returned an incompatible contract"
        }
        return $report
    }
    catch {
        throw "Listary staged artifact validation failed before owner shutdown or promotion: $($_.Exception.Message)"
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-AgentDisposableSemanticCheck([string]$binary) {
    $operationId = [guid]::NewGuid().ToString("N")
    $probeRoot = Join-Path ([System.IO.Path]::GetTempPath()) "SpiffyListaryAgentSemanticCheck"
    $probeState = Join-Path $probeRoot $operationId
    $probeProfileLock = Join-Path $probeRoot (".{0}.listary-profile.lock" -f $operationId)
    $probeEndpoint = Join-Path $probeState "integration-endpoint-v1.json"
    $resident = $null
    $accepted = $false
    try {
        New-Item -ItemType Directory -Force -Path $probeState | Out-Null
        $residentArguments = @('--state-dir', ([char]34 + $probeState + [char]34))
        $resident = Start-Process -FilePath $binary -ArgumentList $residentArguments -WindowStyle Hidden -PassThru

        for ($i = 0; $i -lt 50; $i++) {
            if ($resident.HasExited) {
                throw "staged Listary semantic probe exited before publishing its endpoint"
            }
            if (Test-Path -LiteralPath $probeEndpoint -PathType Leaf) {
                break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not (Test-Path -LiteralPath $probeEndpoint -PathType Leaf)) {
            throw "staged Listary semantic probe did not publish an endpoint within 5000 ms"
        }

        try {
            $descriptor = Get-Content -LiteralPath $probeEndpoint -Raw | ConvertFrom-Json
        }
        catch {
            throw "staged Listary semantic probe published an unreadable endpoint"
        }
        if (
            [int]$descriptor.schema_version -ne 1 -or
            [int]$descriptor.protocol_version -ne $expectedAgentProtocolVersion -or
            [int]$descriptor.process_id -ne $resident.Id
        ) {
            throw "staged Listary semantic probe published an incompatible endpoint"
        }

        $residentProcess = Get-Process -Id $resident.Id -ErrorAction SilentlyContinue
        if (-not $residentProcess) {
            throw "staged Listary semantic probe process disappeared before validation"
        }
        $residentPath = [System.IO.Path]::GetFullPath($residentProcess.Path)
        if ($residentPath -ne [System.IO.Path]::GetFullPath($binary)) {
            throw "staged Listary semantic probe endpoint is owned by a different executable: $residentPath"
        }

        $statusOutput = & $binary --state-dir $probeState --status
        if ($LASTEXITCODE -ne 0) {
            throw "staged Listary semantic probe status command failed with exit code $LASTEXITCODE"
        }
        try {
            $status = ($statusOutput -join [Environment]::NewLine) | ConvertFrom-Json
        }
        catch {
            throw "staged Listary semantic probe status response was not valid JSON"
        }
        if (
            -not [bool]$status.ok -or
            [int]$status.data.protocol_version -ne $expectedAgentProtocolVersion -or
            [int]$status.data.core_schema_version -ne $expectedCoreSchemaVersion
        ) {
            throw "staged Listary semantic probe status response was incompatible"
        }

        & $binary --state-dir $probeState --stop | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "staged Listary semantic probe stop command failed with exit code $LASTEXITCODE"
        }
        for ($i = 0; $i -lt 50; $i++) {
            $endpointStillPresent = Test-Path -LiteralPath $probeEndpoint -PathType Leaf
            $residentStillRunning = Get-Process -Id $resident.Id -ErrorAction SilentlyContinue
            if (-not $endpointStillPresent -and -not $residentStillRunning) {
                $accepted = $true
                break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $accepted) {
            throw "staged Listary semantic probe did not retire cleanly within 5000 ms"
        }
    }
    catch {
        $originalError = $_.Exception.Message
        if ($resident -and -not $resident.HasExited) {
            try {
                if (Test-Path -LiteralPath $probeEndpoint -PathType Leaf) {
                    & $binary --state-dir $probeState --stop | Out-Null
                    for ($i = 0; $i -lt 20 -and (Get-Process -Id $resident.Id -ErrorAction SilentlyContinue); $i++) {
                        Start-Sleep -Milliseconds 100
                    }
                }
                if (Get-Process -Id $resident.Id -ErrorAction SilentlyContinue) {
                    $resident.Kill()
                    $resident.WaitForExit()
                }
            }
            catch {
                throw "Listary staged semantic validation failed and its exact probe process could not be retired. Original error: $originalError Cleanup error: $($_.Exception.Message)"
            }
        }
        throw "Listary staged semantic validation failed before owner shutdown or promotion: $originalError"
    }
    finally {
        if ($resident -and -not $resident.HasExited) {
            try {
                $resident.Kill()
                $resident.WaitForExit()
            }
            catch {
                # The catch path reports cleanup failures. This is only a final
                # best-effort retirement before removing the disposable state.
            }
        }
        if (Test-Path -LiteralPath $probeState -PathType Container) {
            Remove-Item -LiteralPath $probeState -Recurse -Force
        }
        Remove-Item -LiteralPath $probeProfileLock -Force -ErrorAction SilentlyContinue
        if ((Test-Path -LiteralPath $probeRoot -PathType Container) -and -not (Get-ChildItem -LiteralPath $probeRoot -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $probeRoot -Force -ErrorAction SilentlyContinue
        }
    }
}

function Quote-Vbs([string]$value) {
    return $value.Replace([string][char]34, ([string][char]34 + [char]34))
}

$quote = [string][char]34
$command = $quote + $installedExe + $quote + " --state-dir " + $quote + $state + $quote
if ($requestedWorkspaceCatalog) {
    $command += " --workspace-catalog " + $quote + $requestedWorkspaceCatalog + $quote
}
if ($httpApiEnabled) {
    $command += " --http-api"
}
$vbs = 'CreateObject(' + $quote + 'Wscript.Shell' + $quote + ').Run ' + $quote + (Quote-Vbs $command) + $quote + ', 0, False' + [Environment]::NewLine
if ($requestedUiLauncher -and $uiHotkeyEnabled) {
    $uiCommand = $quote + $requestedUiLauncher + $quote + " --start-hidden --agent-already-starting --state-dir " + $quote + $state + $quote
    if ($requestedWorkspaceCatalog) {
        $uiCommand += " --workspace-catalog " + $quote + $requestedWorkspaceCatalog + $quote
    } else {
        $uiCommand += " --no-workspace-catalog"
    }
    $vbs += 'CreateObject(' + $quote + 'Wscript.Shell' + $quote + ').Run ' + $quote + (Quote-Vbs $uiCommand) + $quote + ', 0, False' + [Environment]::NewLine
}

$markerPayload = [ordered]@{
    schema_version = 1
    install_root = $install
    state_dir = $state
    executable = $installedExe
    startup_launcher = $launcher
    ui_launcher = $requestedUiLauncher
    ui_hotkey_enabled = [bool]($requestedUiLauncher -and $uiHotkeyEnabled)
    workspace_catalog = $requestedWorkspaceCatalog
    http_api_enabled = [bool]$httpApiEnabled
}

if ((Test-Path -LiteralPath $stagedExe) -or (Test-Path -LiteralPath $rollbackExe)) {
    throw "An unfinished Listary installation transaction exists; refusing to overwrite it: $install"
}

$sourceHashBefore = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
Copy-Item -LiteralPath $source -Destination $stagedExe
$sourceHashAfter = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
$stagedHash = (Get-FileHash -LiteralPath $stagedExe -Algorithm SHA256).Hash
if ($sourceHashBefore -ne $sourceHashAfter -or $stagedHash -ne $sourceHashAfter) {
    Remove-Item -LiteralPath $stagedExe -Force -ErrorAction SilentlyContinue
    throw "Listary source binary changed while staging; installation was not started."
}
try {
    $stagedSelfCheck = Invoke-AgentSelfCheck $stagedExe
    Invoke-AgentDisposableSemanticCheck $stagedExe
}
catch {
    Remove-Item -LiteralPath $stagedExe -Force -ErrorAction SilentlyContinue
    throw
}
$markerPayload.executable_sha256 = $stagedHash
$markerPayload.self_check_schema_version = [int]$stagedSelfCheck.schema_version
$markerPayload.agent_protocol_version = [int]$stagedSelfCheck.agent_protocol_version
$markerPayload.core_schema_version = [int]$stagedSelfCheck.core_schema_version

$priorMarkerBytes = if (Test-Path -LiteralPath $installMarker -PathType Leaf) { [System.IO.File]::ReadAllBytes($installMarker) } else { $null }
$priorLauncherBytes = if (Test-Path -LiteralPath $launcher -PathType Leaf) { [System.IO.File]::ReadAllBytes($launcher) } else { $null }
$priorBinaryPresent = Test-Path -LiteralPath $installedExe -PathType Leaf
$priorAgentProcess = Get-AgentEndpointOwnerProcess $endpoint
$priorAgentWasRunning = $null -ne $priorAgentProcess
$priorUiProcess = Get-UiEndpointOwnerProcess $uiEndpoint
$priorUiWasRunning = $null -ne $priorUiProcess
$priorWorkspaceCatalog = $null
$priorUiLauncher = $null
$priorHttpApiEnabled = $false
if ($ownedInstall) {
    if ($marker.PSObject.Properties.Name -contains "workspace_catalog") {
        $priorWorkspaceCatalogValue = [string]$marker.workspace_catalog
        if ($priorWorkspaceCatalogValue) {
            $priorWorkspaceCatalog = [System.IO.Path]::GetFullPath($priorWorkspaceCatalogValue)
        }
    }
    $priorUiLauncherValue = [string]$marker.ui_launcher
    if ($priorUiLauncherValue) {
        $priorUiLauncher = [System.IO.Path]::GetFullPath($priorUiLauncherValue)
    }
    if ($marker.PSObject.Properties.Name -contains "http_api_enabled") {
        $priorHttpApiEnabled = [bool]$marker.http_api_enabled
    }
}

$priorBinaryMoved = $false
$replacementPromoted = $false
$replacementReady = $false
$installSucceeded = $false

try {
    if ($requestedUiLauncher -and (Test-Path -LiteralPath $uiEndpoint -PathType Leaf)) {
        & $requestedUiLauncher --quit-resident --state-dir $state | Out-Null
        for ($i = 0; $i -lt 50 -and (Test-Path -LiteralPath $uiEndpoint); $i++) {
            Start-Sleep -Milliseconds 100
        }
        if (Test-Path -LiteralPath $uiEndpoint) {
            $residentProcess = Get-UiEndpointOwnerProcess $uiEndpoint
            if ($residentProcess) {
                throw "The existing Spiffy Listary UI did not stop cleanly: PID $($residentProcess.Id)"
            }
            Remove-Item -LiteralPath $uiEndpoint -Force
        }
    }

    if (Test-Path -LiteralPath $endpoint) {
        $stopBinary = if (Test-Path -LiteralPath $installedExe -PathType Leaf) { $installedExe } else { $source }
        $stopArguments = @('--state-dir', ([char]34 + $state + [char]34), '--stop')
        $stopProcess = Start-Process -FilePath $stopBinary -ArgumentList $stopArguments -WindowStyle Hidden -Wait -PassThru
        $stopExit = $stopProcess.ExitCode
        if ($stopExit -ne 0) {
            $ownerProcess = Get-AgentEndpointOwnerProcess $endpoint
            if ($ownerProcess) {
                throw "A live Listary agent (PID $($ownerProcess.Id)) owns the endpoint but did not stop cleanly: $endpoint"
            }
            Remove-Item -LiteralPath $endpoint -Force
        }
        for ($i = 0; $i -lt 50 -and (Test-Path -LiteralPath $endpoint); $i++) {
            Start-Sleep -Milliseconds 100
        }
        if (Test-Path -LiteralPath $endpoint) {
            $ownerProcess = Get-AgentEndpointOwnerProcess $endpoint
            if ($ownerProcess) {
                throw "The existing Listary agent did not release its endpoint: PID $($ownerProcess.Id), $endpoint"
            }
            Remove-Item -LiteralPath $endpoint -Force
        }
        if ($priorAgentProcess) {
            for ($i = 0; $i -lt 50 -and (Get-Process -Id $priorAgentProcess.Id -ErrorAction SilentlyContinue); $i++) {
                Start-Sleep -Milliseconds 100
            }
            if (Get-Process -Id $priorAgentProcess.Id -ErrorAction SilentlyContinue) {
                throw "The existing Listary agent released its endpoint but did not exit cleanly: PID $($priorAgentProcess.Id)"
            }
        }
    }

    if ($priorBinaryPresent) {
        Move-Item -LiteralPath $installedExe -Destination $rollbackExe
        $priorBinaryMoved = $true
    }
    Move-Item -LiteralPath $stagedExe -Destination $installedExe
    $replacementPromoted = $true
    $promotedHash = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash
    if ($promotedHash -ne $stagedHash) {
        throw "Promoted Listary agent bytes differ from the validated staged artifact."
    }

    if (-not $NoStart) {
        $replacementReady = Start-AgentAndWaitReady $installedExe $state $requestedWorkspaceCatalog $httpApiEnabled
        if (-not $replacementReady) {
            throw "Listary agent did not become ready after installation: $endpoint"
        }
    }

    Write-AtomicText $installMarker ($markerPayload | ConvertTo-Json -Compress)
    Write-AtomicText $launcher $vbs

    if (-not $NoStart) {
        $status = & $installedExe --state-dir $state --status
        if ($LASTEXITCODE -ne 0) {
            throw "Listary agent status failed after installation acceptance."
        }
        try {
            $statusPayload = ($status -join [Environment]::NewLine) | ConvertFrom-Json
        }
        catch {
            throw "Listary agent status returned unreadable JSON after installation acceptance."
        }
        if ($null -eq $statusPayload.data -or $null -eq $statusPayload.data.http_api) {
            throw "Listary agent status does not expose the required HTTP automation mode after installation acceptance."
        }
        if ([bool]$statusPayload.data.http_api.enabled -ne [bool]$httpApiEnabled) {
            throw "Listary agent HTTP automation mode does not match the installer-owned startup configuration."
        }
        $status | Write-Output
        if ($requestedUiLauncher -and $uiHotkeyEnabled) {
            $uiArguments = @('--start-hidden', '--agent-already-starting', '--state-dir', $state)
            if ($newInstall) {
                $uiArguments += '--setup-if-rootless'
                $uiArguments = @($uiArguments | Where-Object { $_ -ne '--start-hidden' })
            }
            if ($requestedWorkspaceCatalog) {
                $uiArguments += @('--workspace-catalog', $requestedWorkspaceCatalog)
            } else {
                $uiArguments += '--no-workspace-catalog'
            }
            & $requestedUiLauncher @uiArguments | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Spiffy Listary UI failed to start after agent installation."
            }
        }
    }
    if ($priorBinaryMoved -and (Test-Path -LiteralPath $rollbackExe -PathType Leaf)) {
        Remove-Item -LiteralPath $rollbackExe -Force
        if (Test-Path -LiteralPath $rollbackExe) {
            throw "Accepted Listary rollback binary could not be retired after replacement acceptance: $rollbackExe"
        }
        $priorBinaryMoved = $false
    }
    $installSucceeded = $true
}
catch {
    $originalError = $_.Exception.Message
    $rollbackErrors = [System.Collections.Generic.List[string]]::new()

    if ($requestedUiLauncher -and (Test-Path -LiteralPath $uiEndpoint -PathType Leaf)) {
        try {
            & $requestedUiLauncher --quit-resident --state-dir $state | Out-Null
            for ($i = 0; $i -lt 50 -and (Test-Path -LiteralPath $uiEndpoint); $i++) {
                Start-Sleep -Milliseconds 100
            }
            $residentProcess = Get-UiEndpointOwnerProcess $uiEndpoint
            if ($residentProcess) {
                $rollbackErrors.Add("replacement UI PID $($residentProcess.Id) did not stop")
            }
        }
        catch {
            $rollbackErrors.Add("replacement UI stop failed: $($_.Exception.Message)")
        }
    }

    if ($replacementPromoted -and (Test-Path -LiteralPath $endpoint -PathType Leaf)) {
        try {
            $stopArguments = @('--state-dir', ([char]34 + $state + [char]34), '--stop')
            $stopProcess = Start-Process -FilePath $installedExe -ArgumentList $stopArguments -WindowStyle Hidden -Wait -PassThru
            for ($i = 0; $i -lt 50 -and (Test-Path -LiteralPath $endpoint); $i++) {
                Start-Sleep -Milliseconds 100
            }
            if (Test-Path -LiteralPath $endpoint) {
                $ownerProcess = Get-AgentEndpointOwnerProcess $endpoint
                if ($ownerProcess) {
                    $rollbackErrors.Add("replacement agent PID $($ownerProcess.Id) did not stop")
                }
                else {
                    Remove-Item -LiteralPath $endpoint -Force
                }
            }
        }
        catch {
            $rollbackErrors.Add("replacement stop failed: $($_.Exception.Message)")
        }
    }

    $liveReplacement = if (Test-Path -LiteralPath $endpoint -PathType Leaf) { Get-AgentEndpointOwnerProcess $endpoint } else { $null }
    if (-not $liveReplacement) {
        try {
            if ($replacementPromoted -and (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
                Remove-Item -LiteralPath $installedExe -Force
            }
            if ($priorBinaryMoved -and (Test-Path -LiteralPath $rollbackExe -PathType Leaf)) {
                Move-Item -LiteralPath $rollbackExe -Destination $installedExe
            }
        }
        catch {
            $rollbackErrors.Add("binary restore failed: $($_.Exception.Message)")
        }
    }
    else {
        $rollbackErrors.Add("replacement endpoint is still live; prior binary was not overwritten during rollback")
    }

    try {
        if ($null -ne $priorMarkerBytes) {
            Write-AtomicBytes $installMarker $priorMarkerBytes
        }
        else {
            Remove-Item -LiteralPath $installMarker -Force -ErrorAction SilentlyContinue
        }
        if ($null -ne $priorLauncherBytes) {
            Write-AtomicBytes $launcher $priorLauncherBytes
        }
        else {
            Remove-Item -LiteralPath $launcher -Force -ErrorAction SilentlyContinue
        }
    }
    catch {
        $rollbackErrors.Add("configuration restore failed: $($_.Exception.Message)")
    }

    if ($priorAgentWasRunning -and -not (Test-Path -LiteralPath $endpoint -PathType Leaf) -and (Test-Path -LiteralPath $installedExe -PathType Leaf)) {
        try {
            if (-not (Start-AgentAndWaitReady $installedExe $state $priorWorkspaceCatalog $priorHttpApiEnabled)) {
                $rollbackErrors.Add("prior agent did not become ready after rollback")
            }
        }
        catch {
            $rollbackErrors.Add("prior agent restart failed: $($_.Exception.Message)")
        }
    }

    if ($priorUiWasRunning) {
        if ($priorUiLauncher -and (Test-Path -LiteralPath $priorUiLauncher -PathType Leaf)) {
            try {
                $priorUiArguments = @('--start-hidden', '--agent-already-starting', '--state-dir', $state)
                if ($priorWorkspaceCatalog) {
                    $priorUiArguments += @('--workspace-catalog', $priorWorkspaceCatalog)
                } else {
                    $priorUiArguments += '--no-workspace-catalog'
                }
                & $priorUiLauncher @priorUiArguments | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    $rollbackErrors.Add("prior UI launcher returned exit code $LASTEXITCODE")
                }
            }
            catch {
                $rollbackErrors.Add("prior UI restart failed: $($_.Exception.Message)")
            }
        }
        else {
            $rollbackErrors.Add("prior UI was running but its trusted launcher is unavailable")
        }
    }

    Remove-Item -LiteralPath $stagedExe -Force -ErrorAction SilentlyContinue
    if ($rollbackErrors.Count -gt 0) {
        throw "Listary installation failed and rollback was incomplete. Original error: $originalError Rollback errors: $($rollbackErrors -join '; ')"
    }
    if ($ownedInstall) {
        throw "Listary installation failed; previous installation was restored. Original error: $originalError"
    }
    throw "Listary installation failed; partial installation artifacts were removed. Original error: $originalError"
}
finally {
    if ($installSucceeded) {
        Remove-Item -LiteralPath $rollbackExe -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stagedExe -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "Installed Listary agent: $installedExe"
Write-Output "State directory: $state"
Write-Output "Login launcher: $launcher"
Write-Output $(if ($requestedWorkspaceCatalog) { "Workspace catalog: $requestedWorkspaceCatalog" } else { "Workspace catalog: disabled" })
Write-Output $(if ($httpApiEnabled) { "Local HTTP API: enabled" } else { "Local HTTP API: disabled" })
if ($requestedUiLauncher) {
    Write-Output "Resident UI launcher: $requestedUiLauncher"
    Write-Output $(if ($uiHotkeyEnabled) { "Double-Control hotkey: enabled" } else { "Double-Control hotkey: disabled" })
}
}
finally {
    if ($transactionMutexOwned) {
        $transactionMutex.ReleaseMutex()
    }
    $transactionMutex.Dispose()
}
