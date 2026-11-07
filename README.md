# dwm_lut for Windows 11 26H2

[![Build Windows package](https://github.com/attcai-cn/dwm_lut-windows-26h2/actions/workflows/build-windows.yml/badge.svg)](https://github.com/attcai-cn/dwm_lut-windows-26h2/actions/workflows/build-windows.yml)

Repository: **https://github.com/attcai-cn/dwm_lut-windows-26h2**

> [!WARNING]
> **26H2 (build 26300) is supported and runtime-verified in SDR.** On Windows
> 11 26H2 build 26300.9711 the hook resolves at `Present RVA=0x22A230`, the
> overlay swap-chain vtable chain returns a usable D3D11 back buffer, and the
> SDR test LUT visibly reaches the desktop. **HDR was not tested** on this build
> because no HDR-capable display was available: HDR LUTs, the HDR context
> fallback and multi-monitor coordinate matching remain unverified. DWM
> internals can still change in cumulative updates - re-run the signature
> verifier after each one.

See [CHANGELOG.md](CHANGELOG.md) for the v4.2.x compatibility and safety changes.

Maintenance fork of [ledoge/dwm_lut](https://github.com/ledoge/dwm_lut), based on
the lauralex 24H2 port, updated for the Windows 11 25H2 / 26H2 servicing
families (builds 26200 and 26300) and maintained for **Windows 11 26H2**.

The 25H2/26H2 path uses the current DWM `COverlayContext::Present` layout and
obtains the D3D11 back buffer through the overlay swap-chain vtable.
DirectFlip/MPO helper hooks are intentionally optional: a cumulative update that
changes one of those private functions no longer prevents LUT injection from
initializing.

> [!CAUTION]
> This software injects a DLL into `dwm.exe` and hooks undocumented Windows
> internals. A future cumulative update can still break it. Disable it before a
> Windows feature update and keep a normal desktop recovery path available.

## Dependencies
- Visual C++ runtime (https://www.techpowerup.com/download/visual-c-redistributable-runtime-package-all-in-one/)

# About
This tool applies 3D LUTs to the Windows desktop by hooking into DWM. It works in both SDR and HDR modes, and uses tetrahedral interpolation on the LUT data. In SDR, blue-noise dithering is applied to the output to reduce banding.

## How it works

The GUI injects `dwm_lut.dll` into `dwm.exe`, the Windows Desktop Window
Manager. DWM composites application windows into the final desktop image. The
injected DLL hooks DWM's Direct3D rendering path and applies the configured 3D
LUT before the composed frame reaches the display.

The DLL does **not** inject into game processes. However, anti-cheat software may
still detect or reject modification of the Windows graphics pipeline, and some
games prohibit color filters that can improve visibility. Disable and close
dwm_lut before launching competitive or anti-cheat-protected games.

## Compatibility targets

The DLL matches **exact servicing families**. It refuses any other newer build
instead of assuming "the latest known layout", because these offsets point into
undocumented DWM objects and a wrong guess can crash the compositor.

| Windows version | Build | DWM path | Status |
| --- | --- | --- | --- |
| Windows 11 26H2 | 26300 | 25H2 shared layout | Supported (**runtime-verified in SDR** on 26300.9711; HDR untested) |
| Windows 11 25H2 | 26200 | 25H2 layout | Supported (runtime-tested on 26200.8457) |
| Windows 11 24H2 | 26100 | 24H2 layout | Supported |
| Windows 11 21H2-23H2 | 22000-26099 | Legacy Win11 signatures | Supported by upstream fork |
| Windows 10 20H2/21H1 | < 22000 | Legacy signatures | Supported by upstream fork |
| Windows 11 26H1 | 28000 | Separate branch | **Rejected** - DWM layout unverified |
| Any other future build | - | - | **Rejected** until verified |

> [!IMPORTANT]
> **KB5127936 (build 26300.9818 / 26200.9818, October 2026 preview) breaks the
> Present hook.** That update replaces `dwmcore.dll` with a larger image
> (`imageSize 0x483000` vs `0x44B000` on 26300.9711) and changes the
> `COverlayContext::Present` prologue, so the 46-byte AOB no longer matches.
> The DLL detects this and safely refuses to hook, logging
> `DWM LUT refusing to hook: required Present AOB did not match on this build`.
> See [Recovering after a cumulative update](#recovering-after-a-cumulative-update).
> KB5127936 is an optional preview; its contents are normally folded into the
> following month's security update, so other 25H2/26H2 systems will hit this.

**Why 26H2 works with the 25H2 layout.** Microsoft delivers 26H2 as an
enablement package (KB5121794): 24H2, 25H2 and 26H2 share the `ge_release`
servicing branch and an identical set of system files. Only the build number
changes. On the test system, `dwmcore.dll` still reports file version
`10.0.26100.9711` while the OS reports `26300.9711`.

Static verification on Windows 11 26H2 build 26300.9711
(`dwmcore.dll`, 4,476,928 bytes):

| Signature | Length | Hits | RVA |
| --- | --- | --- | --- |
| `COverlayContext::Present` (25H2) | 46 | 1 | `0x22A230` |
| `IsCandidateDirectFlipCompatible` (25H2) | 27 | 1 | `0x18497C` |
| `OverlaysEnabled` (25H2) | 17 | 1 | `0x1C3E78` |
| `COverlayContext::Present` (24H2) | 6 | 0 | - |

All three 25H2 signatures resolve exactly once, and the `OverlayTestMode`
global derived from `OverlaysEnabled` lands at `0x417708` inside the writable
`.data` section. The 24H2 Present signature does **not** match, confirming 26H2
must take the 25H2 branch. The GUI header shows the detected build and whether
it is supported.

### Runtime verification on Windows 11 26H2 build 26300.9711 (SDR)

A real injection test on a single-monitor SDR desktop confirmed the full SDR
path, using a `dwm_lut.dll` byte-identical to the one shipped with v4.2.1. The
test used both `examples/TEST-red-blue-swap.cube` (2^3, visibly obvious) and a
65^3 calibration LUT. Across 27 injection sessions the log (`dwm_lut.log`)
showed:

- `DWM LUT loading: build=26300.9711` and
  `servicing family: 24h2=0 25h2=0 26h2=1 modernOverlay=1 legacyWin11=0` -
  the exact-family whitelist selects 26H2.
- `DWM LUT Present RVA=0x22A230` - identical to the static AOB scan.
- `foundPresent=1`, all three 25H2 signatures resolved, and
  `Set OverlayTestMode global to 5 in DWM memory` succeeded.
- `25H2/26H2: Got texture via overlaySwapChain->vt[24]()->vt2[19]()->QI` -
  **the two back-buffer vtable slots, which cannot be checked by an AOB scan,
  are valid on 26H2**. This was the last open runtime question.
- `Initializing from texture device`, successful vertex/pixel shader compilation
  and `RenderLUT: format=87 ...`.
- `Matched LUT: monitor left=0 top=0 hdr=0 lutSize=2` (and `lutSize=65` for the
  calibration LUT) - the LUT was selected for the presented frame.
- No `failed`, `FAILED`, `error` or `exception` lines in any session.

The red-blue swap was visible on screen. HDR could not be exercised on this
system; see [Known limitations](#known-limitations) below.

# Usage
Use DisplayCAL or similar to generate .cube LUT files of any size, run `DwmLutGUI.exe`, assign them to monitors and then click Apply. Note that LUTs cannot be applied to monitors that are in "Duplicate" mode.

**Assigning a LUT is a separate step from applying it.** `Browse`, `Clear` and
**Load included SDR test LUT** only write the file path into the SDR LUT field
and save `config.xml`. They do **not** inject into DWM, do **not** change the
screen, and do **not** create `dwm_lut.log`. After the path appears in the
field, click **Apply LUT** (with the GUI running as administrator). That is the
step that copies `dwm_lut.dll` into `%SYSTEMROOT%\Temp\`, writes the log-path
pointer, and loads the DLL into `dwm.exe`. The log file only appears after a
successful Apply.

**Apply LUT** and **Disable** sit in a pinned bar at the bottom of the window,
so they stay visible even when the window is resized small. The LUT setup pane
scrolls independently, and the toggle key lives in the same bar.

For a quick SDR functionality check, use
[`examples/TEST-red-blue-swap.cube`](examples/TEST-red-blue-swap.cube). It
deliberately swaps red and blue so activation is visually obvious. It is not a
calibration profile.

For ColourSpace users with HT license level, 65^3 eeColor LUT .txt files are also supported.

HDR LUTs must use BT.2020 + SMPTE ST 2084 values as input and output.

Minimizing the GUI will make it disappear from the taskbar, and you can use the context menu of the tray icon to quickly apply or disable all LUTs. For automation, you can start the exe with any (sensible) combination of `-apply`,  `-disable`, `-minimize` and `-exit` as arguments.

Closing the window or choosing **Exit** disables the LUT and unloads the DLL from
DWM first. If unloading fails, shutdown is cancelled and a warning is shown.
Minimizing to the tray keeps the LUT active. Consequently, `-apply -exit` applies
and then immediately disables the LUT; use `-apply -minimize` for a persistent
automated session.

Note: DirectFlip and MPO get force disabled on monitors with an active LUT. These features are designed to improve performance for some windowed applications by allowing them to bypass DWM (and therefore also the LUT). This ensures that LUTs get applied properly to all applications (except exclusive fullscreen ones).

## Troubleshooting / diagnostics

Current diagnostic builds write a one-time DWM hook log beside the GUI:

`dwm_lut.log` in the same folder as `DwmLutGUI.exe`

If there is **no `dwm_lut.log` at all**, no injection has happened yet. Loading
or browsing a LUT only edits `config.xml`; click **Apply LUT**. A quick check:
`%SYSTEMROOT%\Temp\dwm_lut_log_path.txt` is rewritten on every Apply - if it
still points at an older folder or an older timestamp, Apply never ran. Also
check that **Apply LUT** is not greyed out and the status pill does not read
`???`: both mean `Process.EnterDebugMode()` failed, i.e. the GUI was not started
as administrator.

If the GUI says **Injected** but the display color does not change, that only
means `dwm_lut.dll` loaded into `dwm.exe`; it does not prove the LUT shader
reached the visible desktop frame. Reproduce the issue, then check the log for
whether the 25H2/26H2 Present hook, back-buffer lookup, and monitor LUT lookup
succeeded.

Startup log lines to look for:

- `DWM LUT loading: build=26300.9711 dwmcore=0x... imageSize=0x...`
- `DWM LUT servicing family: ... 26h2=1 modernOverlay=1 ...`
- `DWM LUT Present RVA=0x22A230` - must match the RVA reported by the verifier
  below. A different value means DWM changed and the hook is misplaced.
- `25H2/26H2: Got texture via overlaySwapChain->vt[24]()->vt2[19]()->QI` -
  the back-buffer vtable chain worked. On 26H2 this was confirmed at runtime.
- `Matched LUT: monitor left=... top=... hdr=... lutSize=...` - a configured LUT
  was selected for the frame. Without this line (and without a fallback line)
  no LUT is being applied even though the DLL is injected.
- `DWM LUT refusing unsupported build ...` - the DLL deliberately did nothing.
- `DWM LUT refusing to hook: required Present AOB did not match on this build`
  - `foundPresent=0`: a cumulative update changed `dwmcore.dll`. The DLL did
  the safe thing. See [Recovering after a cumulative update](#recovering-after-a-cumulative-update).

If the log contains **only** `DWM LUT loading: build=26300` and nothing after
it, you are running an older v4.1.0 binary: that version rejects every build
above 26200 and returns from `DllMain` before installing any hook. Use the
v4.2.x binaries (the About window shows `v4.2.1`). Also make sure
`DwmLutGUI.exe`, `dwm_lut.dll` and the LUT files all come from the same build;
the injector copies `dwm_lut.dll` from the GUI's own folder into
`%SYSTEMROOT%\Temp\`, so mixing folders silently loads a stale DLL.

### Verifying a new Windows build (after every cumulative update)

The included script scans `dwmcore.dll` on disk. It only reads the PE file - it
never loads, injects or patches `dwm.exe`:

```
python tools/verify_dwm_signatures.py
python tools/verify_dwm_signatures.py C:\Windows\System32\dwmcore.dll --json
```

Check that the row `25H2/26H2  *COverlayContext::Present` shows exactly **1**
hit and that the family status is `USABLE`. If the hit count is 0 or greater
than 1, do not inject: re-derive the signatures/offsets in
`lutdwm/dllmain.cpp` first. The two vtable slots used for back-buffer discovery
(`IOverlaySwapChain_BackBuffer_vtable_index` and the nested one) cannot be
verified by an AOB scan and need runtime testing when DWM changes.

### Recovering after a cumulative update

`COverlayContext::Present` is located by a 46-byte **function-prologue** AOB
(fixed stack size, register saves, stack-cookie reference). A DWM recompile can
change any of those bytes without changing the function's behavior, so the
signature goes stale even though the code is still there. The DLL treats a
missing Present as fatal: `foundPresent=0` means it installs no hooks at all.
**Do not disable that guard** - hooking an unverified address can crash the
compositor.

Diagnose on the affected machine. This needs Python 3.9+ (no third-party
packages), needs no administrator rights and is read-only - both scripts
only parse the DLL from disk, they never load or patch `dwm.exe`. The
scripts can be copied to the machine on their own and given an explicit
DLL path if the repository is not present there:

```
python tools/verify_dwm_signatures.py
python tools/derive_present_signature.py
python tools/derive_present_signature.py --export D:\dwm_diag
```

`derive_present_signature.py` carries 14 short anchors from the *body* of
Present on build 26300.9711 (mostly register/stack instructions with no
relative addresses), also tried with a 4-byte wildcard hole to tolerate a
changed `rel32`. Anchors that match exactly once vote for the new function
start (`hit_rva - offset_in_old_function`); the tool prints a candidate 46-byte
AOB and a hex dump. On 26300.9711 it reproduces the existing `dllmain.cpp`
signature byte-for-byte (14/14 anchors, RVA `0x22A230`). `--export` also copies
`dwmcore.dll` and writes `report.json` for offline analysis when no anchor
survives - send those two files when opening an issue.

A replacement Present AOB is **necessary but not sufficient**. The clip-box
offset (`0x7698`), the `OverlayTestMode` write target and the two back-buffer
vtable slots (24/19) are also private to this DWM build and cannot be checked
by an AOB scan. After updating the signature, rebuild, inject, and confirm
`Got texture ...`, `Matched LUT ...` and a visible red-blue swap before
trusting the new build. Also treat the short DirectFlip/OverlaysEnabled hits
with suspicion: on 26300.9818 they resolve to RVAs `0xF637C` and
`0x1F49B0` (versus `0x18497C` and `0x1C3E78` on 26300.9711) - wildly
different deltas for one recompiled binary, which is typical of false
positives. Because the Present guard fails first, no hook is installed
and the derived `OverlayTestMode` address is never written to.

Until a fixed build is available, **uninstalling the optional KB5127936
preview** restores 26300.9711, which is runtime-verified in SDR. Pause Windows
Update if you need to stay on that build.

### Adding a new servicing family

Adding a build number to the exact-match list in `DllMain` is only safe after:

1. `tools/verify_dwm_signatures.py` reports a unique Present hit for that build.
2. The `OverlayTestMode` RIP target resolves into a writable section.
3. A visual test with `examples/TEST-red-blue-swap.cube` confirms the LUT
   actually reaches the desktop in SDR on that build. Test HDR as well when an
   HDR-capable display is available; if it cannot be tested, say so explicitly
   in the compatibility table (as for 26H2) instead of claiming full support.

## Known limitations

- **Builds 26300.9818 / 26200.9818 (KB5127936) are not usable.** The Present AOB
  does not match the updated `dwmcore.dll`; see the note in Compatibility
  targets and the recovery procedure above.
- **HDR is unverified on 26H2.** No HDR-capable display was available for the
  runtime test. The HDR code path (BT.2020 + SMPTE ST 2084 LUT data, `hdr=1`
  LUT matching and the primary-HDR-context fallback) is carried over from 25H2
  and has not been exercised on build 26300. HDR users should validate with a
  known-good HDR LUT before relying on it.
- **Multi-monitor setups are unverified on 26H2.** In the logged sessions the
  clip-box read as `floats={0.0,0.0,0.0,0.0} ints={0,0}`, giving `left=0
  top=0`, which matched the single monitor at (0,0) exactly. A second display
  at a non-zero offset may therefore fail to match its LUT. The single-LUT
  fallback can cover a one-display configuration but not a multi-LUT setup.
  Check the `Matched LUT` line in `dwm_lut.log` on multi-monitor systems.
- Static signature verification still cannot validate the back-buffer vtable
  slots; only an injection test can. Re-run the verifier after every cumulative
  update and repeat a visual test if the Present RVA changes.


# Compiling

## Required toolchain

Install these with the Visual Studio Installer:

| Workload / component | Needed for |
| --- | --- |
| Desktop development with C++ (MSVC) | `dwm_lut.dll` |
| Windows 11 SDK (10.0.26100.0 or newer) | D3D11 headers and `rc.exe` |
| C++ CMake tools for Windows (Ninja) | vcpkg building MinHook |
| .NET Framework 4.8 targeting pack | WPF GUI (`net48`) |
| .NET desktop development / WPF | `DwmLutGUI.exe` |

`lutdwm.vcxproj` declares `PlatformToolset v143` (VS 2022). On a machine that
only has the VS 2026 `v145` toolset, pass `/p:PlatformToolset=v145` or retarget
the project in Visual Studio.

## Build dependencies

| Package | Manager | Restored into |
| --- | --- | --- |
| MinHook 1.3.4, triplet `x64-windows-static` | vcpkg manifest `lutdwm/vcpkg.json` | `lutdwm/vcpkg_installed/` |
| WindowsDisplayAPI 1.3.0.13 | NuGet `packages.config` | `DwmLutGUI/packages/` |

Both folders are git-ignored and are recreated by the restore step. Neither
package needs to be committed.

## One-step setup (recommended)

`vcpkg` itself must exist once (default `%USERPROFILE%\vcpkg`, or pass
`-VcpkgRoot <path>` / set `VCPKG_ROOT`):

```
git clone https://github.com/microsoft/vcpkg.git %USERPROFILE%\vcpkg
%USERPROFILE%\vcpkg\bootstrap-vcpkg.bat
```

Then, from any ordinary PowerShell window (no admin rights, no VS developer
prompt required):

```
powershell -ExecutionPolicy Bypass -File tools\restore-deps.ps1 -Build
```

The script locates VS/MSVC/Windows SDK (registry `KitsRoot10` first, with a
filesystem fallback), sets up the x64 native environment for the build,
integrates vcpkg, installs MinHook, restores WindowsDisplayAPI, supplies .NET
Framework 4.8 reference assemblies when the targeting pack is missing, and
builds Release x64 to:

- `build\native\dwm_lut.dll`
- `build\package\DwmLutGUI.exe`

Use `-Configuration Debug` for a Debug build. Omit `-Build` to only restore
dependencies. Add `-GuiOnly` to skip vcpkg/MinHook and the native C++ build —
useful when you only need `DwmLutGUI.exe` or have no Windows SDK installed;
the script then reuses an existing `build\native\dwm_lut.dll`. Downloads are
cached under `%LOCALAPPDATA%\NuGet` and `%LOCALAPPDATA%\DwmLut`.

## Manual steps

Use an **x64 Native Tools Command Prompt for VS** (see the `rc.exe` note below):

```
vcpkg integrate install
vcpkg install --x-manifest-root=<repo>\lutdwm --triplet=x64-windows-static
nuget restore DwmLutGUI\DwmLutGUI.sln -PackagesDirectory DwmLutGUI\packages

msbuild lutdwm.sln /m /p:Configuration=Release /p:Platform=x64 /p:VcpkgRoot=<vcpkg root>\
msbuild DwmLutGUI\DwmLutGUI.sln /m /p:Configuration=Release /p:Platform=x64
```

Alternatively, open the two solutions in Visual Studio and build x64 Release;
VS restores the packages automatically once vcpkg integration is enabled.

Keep `dwm_lut.dll`, `DwmLutGUI.exe`, `DwmLutGUI.exe.config` and
`WindowsDisplayAPI.dll` together in one folder. Do not copy the DLL into
`System32`.

## Common setup failures

| Symptom | Cause / fix |
| --- | --- |
| `RC Pass 1 ... no such file or directory` / "C compiler is not able to compile a simple test program" during `vcpkg install` | vcpkg/CMake ran without the VS x64 environment and cannot resolve bare `rc`. Use the x64 Native Tools prompt, or `restore-deps.ps1` (it sets `RC`/`MT` to absolute SDK paths). |
| `error MSB3644: reference assemblies for .NETFramework,Version=v4.8 were not found` | Install the .NET Framework 4.8 targeting pack in VS Installer, or pass `/p:TargetFrameworkRootPath=<...>\build\` pointing at the `Microsoft.NETFramework.ReferenceAssemblies.net48` package. The script does the latter. |
| `MSB8020: The build tools for v143 ... cannot be found` | Only a newer toolset is installed. Pass `/p:PlatformToolset=v145` or retarget the project. |
| `LNK4098: defaultlib 'LIBCMT' conflicts` | Harmless warning from MinHook's static CRT; the DLL still builds. |
| `MSBuild could not start: ... System.Memory, Version=4.0.2.0` | The VS installation is incomplete — `MSBuild.exe.config` is missing, so its assembly binding redirects are gone. Repair Visual Studio via Installer > Modify, or build from an **x64 Native Tools Command Prompt for VS**. Also check `PATH` for a stray `System.Memory.dll` (some bundled Git/CLI tools ship one). |
| `Windows SDK headers not found` | Install a Windows 11 SDK, or use `-GuiOnly` to build just the GUI. The script no longer aborts the whole restore in this case. |
