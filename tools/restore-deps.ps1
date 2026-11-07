<#
.SYNOPSIS
  Restore build dependencies for dwm_lut, and optionally build Release x64.

.DESCRIPTION
  Handles the three dependency groups this repository needs:
    1. MinHook (vcpkg manifest, x64-windows-static)  - native DLL only
    2. WindowsDisplayAPI 1.3.0.13 (NuGet packages.config)
    3. .NET Framework 4.8 reference assemblies (WPF GUI target)

  It also sets up the VS x64 native build environment for the current process.
  That step matters: vcpkg/CMake otherwise cannot find rc.exe and fails with
  "RC Pass 1 ... no such file or directory" / "compiler is not able to compile
  a simple test program".

  The Windows SDK location is read from the registry with a filesystem fallback,
  so it also works on machines where the SDK is not under the default path.
  A missing C++ SDK does not block the WPF GUI build. Use -GuiOnly to skip
  vcpkg/MinHook and the native build entirely.

  Everything is downloaded to user-local folders; nothing needs admin rights.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\restore-deps.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\restore-deps.ps1 -Build

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\restore-deps.ps1 -Build -GuiOnly
#>
[CmdletBinding()]
param(
    [string]$VcpkgRoot = "",
    [switch]$Build,
    [switch]$GuiOnly,
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LocalAppData = $env:LOCALAPPDATA
if (-not $LocalAppData) { $LocalAppData = Join-Path $env:USERPROFILE "AppData\Local" }

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Fail($msg)       { Write-Host "  [FAIL] $msg" -ForegroundColor Red; throw $msg }

# ---------------------------------------------------------------- VS discovery
Write-Step "Locating Visual Studio / MSVC / Windows SDK"
$pf86 = ${env:ProgramFiles(x86)}
if (-not $pf86) { $pf86 = "C:\Program Files (x86)" }
$vswhere = Join-Path $pf86 "Microsoft Visual Studio\Installer\vswhere.exe"
$candidates = @()
if (Test-Path $vswhere) {
    $candidates += @(& $vswhere -all -products * -property installationPath)
}
# vswhere can miss standalone BuildTools installs, so scan the usual roots too.
foreach ($vsBase in @((Join-Path $pf86 "Microsoft Visual Studio"),
                      (Join-Path $env:ProgramFiles "Microsoft Visual Studio"))) {
    if (-not (Test-Path $vsBase)) { continue }
    foreach ($verDir in (Get-ChildItem $vsBase -Directory -ErrorAction SilentlyContinue)) {
        $candidates += $verDir.FullName
        foreach ($edition in (Get-ChildItem $verDir.FullName -Directory -ErrorAction SilentlyContinue)) {
            $candidates += $edition.FullName
        }
    }
}
$vsRoot = $candidates |
          Where-Object { $_ -and (Test-Path (Join-Path $_ "VC\Tools\MSVC")) } |
          Select-Object -First 1
if (-not $vsRoot) { Fail "Visual Studio with the VC++ toolset was not found." }
Write-Ok "VS root: $vsRoot"

$msvcBase = Join-Path $vsRoot "VC\Tools\MSVC"
$msvcDir = Get-ChildItem $msvcBase -Directory -ErrorAction SilentlyContinue |
           Sort-Object Name -Descending | Select-Object -First 1
if (-not $msvcDir) { Fail "No MSVC toolset under $msvcBase" }
$msvc = $msvcDir.FullName
Write-Ok "MSVC:    $($msvcDir.Name)"

# The Windows SDK is NOT always under %ProgramFiles(x86)%\Windows Kits\10.
# Prefer the registered KitsRoot10, then probe the usual locations. A missing
# C++ SDK must not block the WPF GUI build, so this is a warning rather than a
# hard failure.
function Resolve-WindowsSdk {
    $roots = @()
    foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots",
                       "HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots")) {
        if (Test-Path $key) {
            $v = (Get-ItemProperty $key -ErrorAction SilentlyContinue).KitsRoot10
            if ($v) { $roots += $v }
        }
    }
    $roots += (Join-Path $pf86 "Windows Kits\10")
    $roots += (Join-Path $env:ProgramFiles "Windows Kits\10")
    $roots += (Join-Path $vsRoot "Windows Kits\10")

    foreach ($root in ($roots | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path $root)) { continue }
        $inc = Join-Path $root "Include"
        if (-not (Test-Path $inc)) { continue }
        foreach ($ver in (Get-ChildItem $inc -Directory -ErrorAction SilentlyContinue |
                          Sort-Object Name -Descending)) {
            $bin = Join-Path $root "bin\$($ver.Name)\x64"
            if ((Test-Path (Join-Path $ver.FullName "um\Windows.h")) -and
                (Test-Path (Join-Path $bin "rc.exe"))) {
                return @{ Root = $root; Version = $ver.Name; Include = $inc; Bin = $bin }
            }
        }
    }
    return $null
}

$sdk = Resolve-WindowsSdk
$sdkAvailable = ($null -ne $sdk)
$kitRoot = ""; $sdkIncRoot = ""; $sdkVer = ""; $sdkBin = ""
if ($sdkAvailable) {
    $kitRoot = $sdk.Root
    $sdkIncRoot = $sdk.Include
    $sdkVer = $sdk.Version
    $sdkBin = $sdk.Bin
    Write-Ok "SDK:     $sdkVer  ($kitRoot)"
} else {
    Write-Warn "Windows SDK headers / rc.exe not found - native DLL build will be skipped."
    Write-Warn "The WPF GUI does not need the C++ SDK; -GuiOnly still works."
}

$msbuild = Join-Path $vsRoot "MSBuild\Current\Bin\MSBuild.exe"
if (-not (Test-Path $msbuild)) { Fail "MSBuild.exe not found at $msbuild" }
if (-not (Test-Path "${msbuild}.config")) {
    Write-Warn "${msbuild}.config is missing - MSBuild may fail to start with a System.Memory binding error."
    Write-Warn "If that happens, repair Visual Studio (Installer > Modify) or build from an x64 Native Tools prompt."
}

# ------------------------------------------------------- native build environment
Write-Step "Configuring the VS x64 native environment for this process"
$clBin = Join-Path $msvc "bin\Hostx64\x64"
$env:PATH = "$clBin;$(Join-Path $vsRoot 'MSBuild\Current\Bin');$env:PATH"
if ($sdkAvailable) {
    $env:PATH = "$sdkBin;" + $env:PATH
    $env:INCLUDE = (@(
        (Join-Path $msvc 'include'),
        (Join-Path $sdkIncRoot "$sdkVer\ucrt"),
        (Join-Path $sdkIncRoot "$sdkVer\um"),
        (Join-Path $sdkIncRoot "$sdkVer\shared"),
        (Join-Path $sdkIncRoot "$sdkVer\winrt")
    ) -join ';')
    $env:LIB = (@(
        (Join-Path $msvc 'lib\x64'),
        (Join-Path $kitRoot "Lib\$sdkVer\ucrt\x64"),
        (Join-Path $kitRoot "Lib\$sdkVer\um\x64")
    ) -join ';')
    # CMake/vcpkg otherwise probe bare "rc" / "mt" and cannot find them.
    $env:RC = Join-Path $sdkBin "rc.exe"
    $env:MT = Join-Path $sdkBin "mt.exe"
    $env:WindowsSdkDir = $kitRoot
    $env:WindowsSDKVersion = "$sdkVer\"
}
$env:VCPKG_KEEP_ENV_VARS = "PATH;INCLUDE;LIB;RC;MT;WindowsSdkDir;WindowsSDKVersion"
Write-Ok "PATH / compiler environment set"

# ------------------------------------------------------------------- vcpkg setup
$skipNative = $GuiOnly -or (-not $sdkAvailable)
if ($skipNative) {
    $reason = if ($GuiOnly) { "-GuiOnly requested" } else { "Windows SDK unavailable" }
    Write-Step "Skipping vcpkg / MinHook ($reason)"
} else {
    Write-Step "Locating vcpkg"
    if (-not $VcpkgRoot) { $VcpkgRoot = $env:VCPKG_ROOT }
    if (-not $VcpkgRoot) { $VcpkgRoot = $env:VCPKG_INSTALLATION_ROOT }
    if (-not $VcpkgRoot) { $VcpkgRoot = Join-Path $env:USERPROFILE "vcpkg" }
    $vcpkg = Join-Path $VcpkgRoot "vcpkg.exe"
    if (-not (Test-Path $vcpkg)) {
        Write-Warn "vcpkg.exe not found at $VcpkgRoot"
        Write-Host "        Install it first:"
        Write-Host "        git clone https://github.com/microsoft/vcpkg.git $VcpkgRoot"
        Write-Host "        & '$VcpkgRoot\bootstrap-vcpkg.bat'"
        Fail "vcpkg is missing."
    }
    $env:VCPKG_ROOT = $VcpkgRoot
    Write-Ok "vcpkg:   $vcpkg"

    Write-Step "Integrating vcpkg with MSBuild"
    & $vcpkg integrate install | Out-String | Write-Host
    if ($LASTEXITCODE -ne 0) { Fail "vcpkg integrate install failed" }

    Write-Step "Installing MinHook (manifest, x64-windows-static)"
    & $vcpkg install "--x-manifest-root=$(Join-Path $ProjectRoot 'lutdwm')" --triplet=x64-windows-static | Out-String | Write-Host
    if ($LASTEXITCODE -ne 0) { Fail "vcpkg install failed; see <vcpkg root>\buildtrees\minhook\*.log" }
    $minhookHeader = Join-Path $ProjectRoot "lutdwm\vcpkg_installed\x64-windows-static\include\MinHook.h"
    if (-not (Test-Path $minhookHeader)) { Fail "MinHook.h missing after install" }
    Write-Ok "MinHook: $minhookHeader"
}

# -------------------------------------------------------------------- NuGet setup
Write-Step "Locating nuget.exe"
$nugetDir = Join-Path $LocalAppData "NuGet"
$nuget = Join-Path $nugetDir "nuget.exe"
if (-not (Test-Path $nuget)) {
    New-Item -ItemType Directory -Force $nugetDir | Out-Null
    Write-Host "        Downloading https://dist.nuget.org/win-x86-commandline/v6.14.0/nuget.exe"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    (New-Object Net.WebClient).DownloadFile(
        "https://dist.nuget.org/win-x86-commandline/v6.14.0/nuget.exe", $nuget)
}
Write-Ok "nuget:   $nuget"

Write-Step "Restoring WindowsDisplayAPI (packages.config)"
& $nuget restore (Join-Path $ProjectRoot "DwmLutGUI\DwmLutGUI.sln") `
    -PackagesDirectory (Join-Path $ProjectRoot "DwmLutGUI\packages") `
    -NonInteractive | Out-String | Write-Host
$wda = Join-Path $ProjectRoot "DwmLutGUI\packages\WindowsDisplayAPI.1.3.0.13\lib\net45\WindowsDisplayAPI.dll"
if (-not (Test-Path $wda)) { Fail "WindowsDisplayAPI.dll missing after restore" }
Write-Ok "WindowsDisplayAPI: $wda"

# -------------------------------------------- .NET Framework 4.8 reference assemblies
Write-Step "Checking .NET Framework 4.8 reference assemblies"
$net48Installed = Join-Path $pf86 "Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8\mscorlib.dll"
$targetRoot = ""
if (Test-Path $net48Installed) {
    Write-Ok "Targeting pack installed system-wide"
} else {
    Write-Warn "Targeting pack not installed (MSB3644 would occur). Using NuGet reference assemblies."
    $refRoot = Join-Path $LocalAppData "DwmLut\refasm"
    $mscorlib = Join-Path $refRoot "build\.NETFramework\v4.8\mscorlib.dll"
    if (-not (Test-Path $mscorlib)) {
        New-Item -ItemType Directory -Force $refRoot | Out-Null
        $nupkg = Join-Path $refRoot "net48.nupkg"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        (New-Object Net.WebClient).DownloadFile(
            "https://api.nuget.org/v3-flatcontainer/microsoft.netframework.referenceassemblies.net48/1.0.3/microsoft.netframework.referenceassemblies.net48.1.0.3.nupkg",
            $nupkg)
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($nupkg)
        try {
            foreach ($entry in $zip.Entries) {
                if ($entry.FullName -like "build/*") {
                    $dest = Join-Path $refRoot $entry.FullName
                    $destDir = Split-Path -Parent $dest
                    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force $destDir | Out-Null }
                    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
                }
            }
        } finally { $zip.Dispose() }
    }
    if (-not (Test-Path $mscorlib)) { Fail "Could not materialize net48 reference assemblies" }
    $targetRoot = Join-Path $refRoot "build"
    Write-Ok "TargetFrameworkRootPath: $targetRoot"
}

# ------------------------------------------------------------------------ Build
$toolset = "v143"
if ($msvcDir.Name -match "^14\.(\d+)") {
    if ([int]$Matches[1] -ge 50) { $toolset = "v145" }
}
Write-Step "Summary"
Write-Host "  Platform toolset : $toolset (project declares v143; override when needed)"
Write-Host "  TargetFrameworkRootPath : $(if ($targetRoot) { $targetRoot } else { '(not needed)' })"
Write-Host "  Native DLL build : $(if ($skipNative) { 'skipped' } else { 'enabled' })"
Write-Host "  Dependencies are ready. To build manually:"
Write-Host "    msbuild lutdwm.sln /p:Configuration=$Configuration /p:Platform=x64 /p:PlatformToolset=$toolset /p:VcpkgRoot=$VcpkgRoot\"
$guiExample = "    msbuild DwmLutGUI\DwmLutGUI.sln /p:Configuration=$Configuration /p:Platform=x64"
if ($targetRoot) { $guiExample += " /p:TargetFrameworkRootPath=$targetRoot\" }
Write-Host $guiExample

function Invoke-MsBuild($label, $buildArgs) {
    Write-Step $label
    $out = (& $msbuild @buildArgs 2>&1 | Out-String)
    $out | Write-Host
    if ($LASTEXITCODE -ne 0) {
        if ($out -match "MSBuild could not start" -or $out -match "System\.Memory") {
            Fail "MSBuild itself failed to start (a missing MSBuild.exe.config or a broken VS installation). Repair Visual Studio via Installer > Modify, or build from an 'x64 Native Tools Command Prompt for VS'."
        }
        Fail "$label failed"
    }
}

if ($Build) {
    $buildRoot = Join-Path $ProjectRoot "build"
    if (-not $skipNative) {
        Invoke-MsBuild "Building native DLL ($Configuration x64)" @(
            (Join-Path $ProjectRoot "lutdwm.sln"), "/m", "/nologo", "/v:minimal",
            "/p:Configuration=$Configuration", "/p:Platform=x64",
            "/p:PlatformToolset=$toolset",
            "/p:VcpkgRoot=$VcpkgRoot\",
            "/p:OutDir=$(Join-Path $buildRoot 'native')\")
        Write-Ok (Join-Path $buildRoot "native\dwm_lut.dll")
    } else {
        Write-Warn "Skipping native DLL build; reuse an existing build\native\dwm_lut.dll or install the Windows SDK."
    }

    $guiArgs = @((Join-Path $ProjectRoot "DwmLutGUI\DwmLutGUI.sln"), "/m", "/nologo", "/v:minimal",
                 "/p:Configuration=$Configuration", "/p:Platform=x64",
                 "/p:OutDir=$(Join-Path $buildRoot 'package')\")
    if ($targetRoot) { $guiArgs += "/p:TargetFrameworkRootPath=$targetRoot\" }
    Invoke-MsBuild "Building WPF GUI ($Configuration x64)" $guiArgs
    Write-Ok (Join-Path $buildRoot "package\DwmLutGUI.exe")
}

Write-Host "`nDone." -ForegroundColor Green
