$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"

function Import-DotEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($Line in Get-Content -LiteralPath $Path) {
        $Trimmed = $Line.Trim()

        if ($Trimmed.Length -eq 0 -or $Trimmed.StartsWith("#")) {
            continue
        }

        if ($Trimmed -notmatch '^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            continue
        }

        $Name = $Matches[1]
        $Value = $Matches[2].Trim()

        if (
            ($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
            ($Value.StartsWith("'") -and $Value.EndsWith("'"))
        ) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Get-EnvOrDefault {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,

        [Parameter(Mandatory = $true)]
        [string] $Default
    )

    $Value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $Default
    }

    return $Value
}

function Stop-DevProcess {
    param(
        [System.Diagnostics.Process] $Process
    )

    if ($null -ne $Process -and -not $Process.HasExited) {
        $Taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
        if ($null -ne $Taskkill) {
            & $Taskkill.Source /PID $Process.Id /T /F 2>$null | Out-Null
        }
        else {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-PortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [string] $HostName,

        [Parameter(Mandatory = $true)]
        [int] $Port
    )

    $Address = [System.Net.Dns]::GetHostAddresses($HostName)[0]
    $Listener = [System.Net.Sockets.TcpListener]::new($Address, $Port)

    try {
        $Listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $Listener.Stop()
    }
}

function Assert-PortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,

        [Parameter(Mandatory = $true)]
        [string] $HostName,

        [Parameter(Mandatory = $true)]
        [int] $Port
    )

    if (-not (Test-PortAvailable -HostName $HostName -Port $Port)) {
        throw "$Name port is already in use: ${HostName}:${Port}. Stop the existing process or set ${Name}_PORT before running this script."
    }
}

function Add-PathIfDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $Parts = $env:PATH -split [System.IO.Path]::PathSeparator
    if ($Parts -notcontains $Path) {
        $env:PATH = "$Path$([System.IO.Path]::PathSeparator)$env:PATH"
    }
}

function Get-DevCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Names
    )

    foreach ($Name in $Names) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($null -ne $Command) {
            return $Command
        }
    }

    return $null
}

function Install-UvIfMissing {
    $UvInstallDir = Join-Path $env:USERPROFILE ".local\bin"
    Add-PathIfDirectory -Path $UvInstallDir
    Add-PathIfDirectory -Path (Join-Path $env:USERPROFILE ".cargo\bin")

    $Command = Get-DevCommand -Names @("uv.exe", "uv")
    if ($null -ne $Command) {
        return $Command
    }

    Write-Host "uv not found. Installing uv with the official installer..."
    New-Item -ItemType Directory -Force -Path $UvInstallDir | Out-Null

    $PreviousUvInstallDir = [Environment]::GetEnvironmentVariable("UV_INSTALL_DIR", "Process")
    $PreviousNoModifyPath = [Environment]::GetEnvironmentVariable("INSTALLER_NO_MODIFY_PATH", "Process")

    try {
        [Environment]::SetEnvironmentVariable("UV_INSTALL_DIR", $UvInstallDir, "Process")
        [Environment]::SetEnvironmentVariable("INSTALLER_NO_MODIFY_PATH", "1", "Process")
        $InstallScript = Invoke-RestMethod https://astral.sh/uv/install.ps1
        Invoke-Expression $InstallScript | Out-Host
    }
    finally {
        [Environment]::SetEnvironmentVariable("UV_INSTALL_DIR", $PreviousUvInstallDir, "Process")
        [Environment]::SetEnvironmentVariable("INSTALLER_NO_MODIFY_PATH", $PreviousNoModifyPath, "Process")
    }

    Add-PathIfDirectory -Path $UvInstallDir
    Add-PathIfDirectory -Path (Join-Path $env:USERPROFILE ".cargo\bin")

    $Command = Get-DevCommand -Names @("uv.exe", "uv")
    if ($null -eq $Command) {
        throw "uv was installed, but it is not available in PATH. Expected uv.exe under: $UvInstallDir"
    }

    return $Command
}

function Install-NodeIfMissing {
    Add-PathIfDirectory -Path "C:\Program Files\nodejs"
    Add-PathIfDirectory -Path (Join-Path $env:APPDATA "npm")

    $Command = Get-DevCommand -Names @("npm.cmd", "npm.exe", "npm")
    if ($null -ne $Command) {
        return $Command
    }

    $Winget = Get-DevCommand -Names @("winget.exe", "winget")
    if ($null -eq $Winget) {
        throw "npm is not installed and winget is not available to install Node.js LTS automatically."
    }

    Write-Host "npm not found. Installing Node.js LTS with winget..."
    & $Winget.Source install --source winget --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements | Out-Host

    Add-PathIfDirectory -Path "C:\Program Files\nodejs"
    Add-PathIfDirectory -Path (Join-Path $env:APPDATA "npm")

    $Command = Get-DevCommand -Names @("npm.cmd", "npm.exe", "npm")
    if ($null -eq $Command) {
        throw "Node.js was installed, but npm is not available in PATH. Open a new PowerShell window, then run this script again."
    }

    return $Command
}

function Sync-Backend {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Directory
    )

    $UvCommand = Install-UvIfMissing

    Write-Host "Syncing backend dependencies..."
    Push-Location $Directory
    try {
        & $UvCommand.Source sync | Out-Host
    }
    finally {
        Pop-Location
    }
}

function Sync-Frontend {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Directory
    )

    $NpmCommand = Install-NodeIfMissing
    $NodeModules = Join-Path $Directory "node_modules"
    $PackageJson = Join-Path $Directory "package.json"
    $PackageLock = Join-Path $Directory "package-lock.json"
    $ShouldInstall = -not (Test-Path -LiteralPath $NodeModules)

    if (-not $ShouldInstall -and (Test-Path -LiteralPath $PackageJson)) {
        $ShouldInstall = (Get-Item -LiteralPath $PackageJson).LastWriteTimeUtc -gt (Get-Item -LiteralPath $NodeModules).LastWriteTimeUtc
    }

    if (-not $ShouldInstall -and (Test-Path -LiteralPath $PackageLock)) {
        $ShouldInstall = (Get-Item -LiteralPath $PackageLock).LastWriteTimeUtc -gt (Get-Item -LiteralPath $NodeModules).LastWriteTimeUtc
    }

    if ($ShouldInstall) {
        Write-Host "Installing frontend dependencies..."
        Push-Location $Directory
        try {
            & $NpmCommand.Source install | Out-Host
        }
        finally {
            Pop-Location
        }
    }

    return $NpmCommand
}

function Install-PlaywrightChromium {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Directory,

        [Parameter(Mandatory = $true)]
        $NpmCommand
    )

    Write-Host "Ensuring Playwright Chromium is installed..."
    Push-Location $Directory
    try {
        & $NpmCommand.Source exec -- playwright install chromium | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install Playwright Chromium."
        }
    }
    finally {
        Pop-Location
    }
}

function Test-BundledDrawioUrl {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Url
    )

    try {
        $Uri = [System.Uri] $Url
    }
    catch {
        return $false
    }

    return (
        $Uri.Scheme -eq "http" -and
        ($Uri.Host -eq "127.0.0.1" -or $Uri.Host -eq "localhost") -and
        $Uri.Port -eq 8081
    )
}

function Test-DrawioReady {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Url
    )

    try {
        $Response = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec 2 -UseBasicParsing
        return $Response.StatusCode -lt 400
    }
    catch {
        return $false
    }
}

function Test-DockerDaemon {
    param(
        [Parameter(Mandatory = $true)]
        [string] $DockerPath
    )

    $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $DockerPath
    $StartInfo.Arguments = "info"
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Process = [System.Diagnostics.Process]::new()
    $Process.StartInfo = $StartInfo

    try {
        if (-not $Process.Start()) {
            return $false
        }

        if (-not $Process.WaitForExit(8000)) {
            $Process.Kill()
            $Process.WaitForExit()
            return $false
        }

        return $Process.ExitCode -eq 0
    }
    catch {
        return $false
    }
    finally {
        $Process.Dispose()
    }
}

function Start-Drawio {
    param(
        [Parameter(Mandatory = $true)]
        [string] $EmbedUrl,

        [Parameter(Mandatory = $true)]
        [string] $ComposeFile
    )

    $LocalUrl = "http://127.0.0.1:8081/"
    if (-not (Test-BundledDrawioUrl -Url $EmbedUrl)) {
        Write-Host "Using externally managed Draw.io: $EmbedUrl"
        return
    }

    if (Test-DrawioReady -Url $LocalUrl) {
        Write-Host "Draw.io is already running on $LocalUrl"
        return
    }

    $Docker = Get-DevCommand -Names @("docker.exe", "docker")
    if ($null -eq $Docker) {
        throw "Docker is required to start the local Draw.io service. Install and start Docker Desktop, then run this script again."
    }

    & $Docker.Source compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose v2 is required to start the local Draw.io service."
    }

    if (-not (Test-DockerDaemon -DockerPath $Docker.Source)) {
        throw "Docker is installed, but the Docker daemon is not running. Start Docker Desktop, then run this script again."
    }

    Write-Host "Starting local Draw.io on $LocalUrl"
    & $Docker.Source compose -f $ComposeFile up -d | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start the local Draw.io service."
    }

    for ($Attempt = 1; $Attempt -le 60; $Attempt++) {
        if (Test-DrawioReady -Url $LocalUrl) {
            Write-Host "Draw.io is ready."
            return
        }
        Start-Sleep -Seconds 1
    }

    & $Docker.Source compose -f $ComposeFile ps | Out-Host
    throw "Draw.io did not become ready within 60 seconds."
}

Import-DotEnv -Path (Join-Path $RepoRoot ".env")

$BackendHost = Get-EnvOrDefault -Name "BACKEND_HOST" -Default "127.0.0.1"
$BackendPort = Get-EnvOrDefault -Name "BACKEND_PORT" -Default "8000"
$FrontendHost = Get-EnvOrDefault -Name "FRONTEND_HOST" -Default "127.0.0.1"
$FrontendPort = Get-EnvOrDefault -Name "FRONTEND_PORT" -Default "5173"
$ViteApiBaseUrl = Get-EnvOrDefault -Name "VITE_API_BASE_URL" -Default "http://${BackendHost}:${BackendPort}"
$DrawioEmbedUrl = Get-EnvOrDefault -Name "PATENT_CREATOR_DRAWIO_EMBED_URL" -Default "http://127.0.0.1:8081/"
[Environment]::SetEnvironmentVariable("VITE_API_BASE_URL", $ViteApiBaseUrl, "Process")
[Environment]::SetEnvironmentVariable("PATENT_CREATOR_DRAWIO_EMBED_URL", $DrawioEmbedUrl, "Process")

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("PATENT_CREATOR_CORS_ALLOW_ORIGINS", "Process"))) {
    $CorsAllowOrigins = "http://${FrontendHost}:${FrontendPort},http://localhost:${FrontendPort}"
    [Environment]::SetEnvironmentVariable("PATENT_CREATOR_CORS_ALLOW_ORIGINS", $CorsAllowOrigins, "Process")
}

$BackendPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
Sync-Backend -Directory $BackendDir

if (-not (Test-Path -LiteralPath $BackendPython)) {
    throw "Backend Python not found after uv sync: $BackendPython"
}

$BackendCommand = $BackendPython
$BackendArguments = @("-m", "uvicorn", "app.api.app:app", "--reload", "--host", $BackendHost, "--port", $BackendPort)
Start-Drawio -EmbedUrl $DrawioEmbedUrl -ComposeFile (Join-Path $RepoRoot "compose.drawio.yaml")
$NpmCommand = Sync-Frontend -Directory $FrontendDir
Install-PlaywrightChromium -Directory $FrontendDir -NpmCommand $NpmCommand
Write-Host "Checking Draw.io rendering with the canonical smoke fixture..."
& $BackendPython (Join-Path $RepoRoot "scripts\drawio_render_preflight.py") --drawio-url $DrawioEmbedUrl
if ($LASTEXITCODE -ne 0) {
    throw "Draw.io render preflight failed. Resolve the reported service, Node.js, Playwright, or Chromium problem and retry."
}

Assert-PortAvailable -Name "BACKEND" -HostName $BackendHost -Port ([int] $BackendPort)
Assert-PortAvailable -Name "FRONTEND" -HostName $FrontendHost -Port ([int] $FrontendPort)

$BackendProcess = $null
$FrontendProcess = $null

try {
    Write-Host "Starting backend on http://${BackendHost}:${BackendPort}"
    $BackendProcess = Start-Process `
        -FilePath $BackendCommand `
        -ArgumentList $BackendArguments `
        -WorkingDirectory $BackendDir `
        -NoNewWindow `
        -PassThru

    Write-Host "Starting frontend on http://${FrontendHost}:${FrontendPort}"
    $FrontendProcess = Start-Process `
        -FilePath $NpmCommand.Source `
        -ArgumentList @("run", "dev", "--", "--host", $FrontendHost, "--port", $FrontendPort, "--strictPort") `
        -WorkingDirectory $FrontendDir `
        -NoNewWindow `
        -PassThru

    Write-Host ""
    Write-Host "Patent Creator dev stack is starting..."
    Write-Host "Frontend: http://${FrontendHost}:${FrontendPort}"
    Write-Host "Backend:  http://${BackendHost}:${BackendPort}"
    if (Test-BundledDrawioUrl -Url $DrawioEmbedUrl) {
        Write-Host "Draw.io:  http://127.0.0.1:8081/"
    }
    Write-Host "Press Ctrl+C to stop both processes."
    Write-Host ""

    while ($true) {
        if ($BackendProcess.HasExited) {
            Write-Error "Backend process exited."
            exit 1
        }

        if ($FrontendProcess.HasExited) {
            Write-Error "Frontend process exited."
            exit 1
        }

        Start-Sleep -Seconds 1
    }
}
finally {
    Stop-DevProcess -Process $BackendProcess
    Stop-DevProcess -Process $FrontendProcess
}
