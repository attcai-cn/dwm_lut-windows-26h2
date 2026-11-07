# Changelog

All notable changes to this maintenance fork are documented here.

## [Unreleased]

### Known issue

- **KB5127936 (build 26300.9818 / 26200.9818) breaks the Present hook.** The
  October 2026 preview replaces `dwmcore.dll` (`imageSize 0x44B000` ->
  `0x483000`) and changes the `COverlayContext::Present` prologue, so the
  46-byte 25H2 AOB no longer matches. The DLL logs
  `DWM LUT refusing to hook: required Present AOB did not match on this build`
  (`foundPresent=0`) and installs no hooks rather than patching an unverified
  address. No replacement signature is shipped yet. Uninstalling the optional
  preview restores 26300.9711, which is runtime-verified in SDR.

### Added

- `tools/derive_present_signature.py`: locates `COverlayContext::Present` in a
  changed `dwmcore.dll` using 14 short body anchors from 26300.9711 (plus
  4-byte wildcard variants), votes on the new function start, emits a candidate
  46-byte AOB and a hex dump, and can export the DLL with a JSON report for
  offline analysis. Self-test on 26300.9711 reproduces the existing
  `dllmain.cpp` signature exactly (14/14 anchors, RVA `0x22A230`). In a
  simulated break (changed prologue plus two changed `rel32` call targets),
  `verify_dwm_signatures.py` correctly reported `NOT USABLE` while the tool
  still recovered `0x22A230` with 14/14 anchors, two of them via wildcard
  variants.
- `tools/verify_dwm_signatures.py` now points at the new tool when the
  25H2/26H2 Present AOB does not resolve.
- README: KB5127936 compatibility note, the new failing log line, a
  "Recovering after a cumulative update" procedure and a known-limitation entry.

## [4.2.1] - 2026-11-07

### Fixed

- **The Apply/Disable buttons could be invisible in a small window.** The right
  pane was a non-scrolling `StackPanel` with the "LUT setup" card stacked above
  the "Controls" card; shrinking the window clipped the whole Controls card,
  hiding **Apply LUT** and **Disable**. They now live in a **pinned bottom action
  bar** that is always visible, alongside the toggle key. The LUT setup pane
  scrolls independently and `MinHeight` was lowered to 460.
- `tools/restore-deps.ps1` no longer hardcodes
  `%ProgramFiles(x86)%\Windows Kits\10` for the Windows SDK. It now reads
  `KitsRoot10` from the registry first and falls back to several filesystem
  locations. A missing C++ SDK warns instead of aborting the whole restore.
- MSBuild startup failures such as
  `System.Memory, Version=4.0.2.0` (missing `MSBuild.exe.config`) are now
  detected and reported with an actionable message instead of a bare exit code.

### Added

- `-GuiOnly` switch for `tools/restore-deps.ps1`: restores only the GUI
  dependencies and builds `DwmLutGUI.exe`, skipping vcpkg/MinHook and the
  native C++ build. Works on machines without a Windows SDK.

### Changed

- Rebranded the fork for Windows 11 26H2 and pointed the About window, README
  badge and release links at `attcai-cn/dwm_lut-windows-26h2`.
- GUI/assembly/manifest version bumped to **4.2.1**.
- CI workflow package name corrected from `v4.1.0` / "25H2" to
  `v4.2.1` / "26H2", `actions/upload-artifact` pinned to `v4`, and the bundled
  README now explains that loading a LUT and clicking Apply are two steps.

### Validation

- `DwmLutGUI.exe` rebuilt as **4.2.1.0** with the pinned action bar compiled
  into its BAML; `dwm_lut.dll` is unchanged since 4.2.0 (no native source edits
  in this release).
- `python tools/verify_dwm_signatures.py` on build 26300.9711 still reports
  `25H2/26H2 USABLE`, exactly one Present hit, and `OverlayTestMode` at
  `0x417708` in writable `.data`.
- XAML/JSON/C# structure checks, `dllmain.cpp` syntax check (0 errors) and
  `restore-deps.ps1 -GuiOnly` all pass.
- **26H2 SDR path runtime-verified** on build 26300.9711 (single-monitor SDR
  desktop). The injected `dwm_lut.dll` was byte-identical to the v4.2.1 release
  binary. Over 27 injection sessions the log confirmed `26h2=1 modernOverlay=1`,
  `Present RVA=0x22A230`, successful `OverlayTestMode` write, and the
  `overlaySwapChain->vt[24]()->vt2[19]()->QI` back-buffer chain - the two
  vtable slots that an AOB scan cannot verify. Shaders compiled, `RenderLUT`
  ran, and `Matched LUT ... hdr=0 lutSize=2` / `lutSize=65` appeared with no
  failures or exceptions. The red-blue swap LUT was visibly applied.

### Known limitations

- **HDR was not tested** on 26H2 (no HDR-capable display available). BT.2020/ST
  2084 LUT data, `hdr=1` LUT matching and the primary-HDR-context fallback are
  inherited from 25H2 but remain unverified on build 26300.
- **Multi-monitor matching is unverified** on 26H2: the logged clip-box read as
  all zeros (`left=0 top=0`), which matched the single monitor at (0,0).
  Displays at non-zero offsets may not find their LUT.

## [4.2.0] - 2026-11-06

### Added

- **Windows 11 26H2 (build 26300) support.** 24H2, 25H2 and 26H2 share the
  `ge_release` servicing branch through enablement package KB5121794, so 26H2
  reuses the verified 25H2 `COverlayContext::Present` layout, clip-box offset
  and back-buffer vtable chain.
- The GUI header shows the detected Windows version, build/UBR and whether that
  servicing family is supported (red text for rejected builds such as 26H1).
- `tools/verify_dwm_signatures.py`: a read-only PE/AOB verifier for
  `dwmcore.dll`. Reports hit counts and RVAs for every known servicing family
  and validates the `OverlayTestMode` RIP target. Run it after every cumulative
  update before injecting.
- Startup logging of the build/UBR, dwmcore image bounds, selected servicing
  family and the resolved Present RVA, so a stale signature is visible in
  `dwm_lut.log` before any hook is installed.
- Modernized WPF dashboard UI with dark cards, clearer controls, and a status
  pill.
- **Load included SDR test LUT** assigns the bundled red/blue test LUT without
  browsing manually.
- **Open Log** opens `dwm_lut.log` from the executable folder.
- `tools/restore-deps.ps1`: one-step dependency restore (vcpkg/MinHook,
  WindowsDisplayAPI, .NET Framework 4.8 reference assemblies) with an optional
  Release x64 build. It sets up the VS x64 native environment so CMake can find
  `rc.exe`, and auto-selects `v143`/`v145` for the installed toolset. README now
  documents the required VS components and the common setup failures.

### Changed

- Replaced `buildNumber > 26200 -> reject` with an explicit servicing-family
  allowlist (26100 / 26200 / 26300 / legacy). Windows 11 26H1 (28000) and every
  other unverified build are still rejected.
- Consolidated the 25H2 runtime checks into `isModernOverlayPath`, so 26H2 uses
  the same Present hook ABI, back-buffer path, HDR-context fallback and
  single-LUT fallback as 25H2.
- Replaced the hardcoded `0x7698` clip-box literal with its named constant and
  moved the two back-buffer vtable slot indices into named constants.
- Renamed the GUI status text from `Active`/`Inactive` to
  `Injected`/`Disabled` to better describe what the status check actually means.
- Log messages now say `25H2/26H2` where both families share the same path.

### Fixed

- Monitor coordinates on the modern path now decode both the float and the
  32-bit integer representation of the DWM clip box. Previously an integer
  origin was read back as a denormal float and collapsed to 0, so non-primary
  displays could fail LUT matching.
- The `OverlayTestMode` global is only written when its RIP-relative target
  resolves inside dwmcore's image. A stale AOB can no longer write `5` into
  arbitrary memory.
- The 24H2 AOB scan now tests the 24H2-ABI pointers it actually fills in.
  Previously it tested legacy pointers, so the loop never stopped early and
  could overwrite a resolved address with a later match.
- Reject obviously invalid vtable function pointers before calling into them on
  the modern back-buffer path.
- Removed the misleading `README` phrase "GUI says Active" (the status is
  `Injected`/`Disabled`).

### Validation

- Statically verified on Windows 11 26H2 build 26300.9711 with
  `dwmcore.dll` 10.0.26100.9711 (4,476,928 bytes): 25H2 Present = 1 hit at
  `0x22A230`, `IsCandidateDirectFlipCompatible` = 1 hit at `0x18497C`,
  `OverlaysEnabled` = 1 hit at `0x1C3E78`; the 24H2 Present AOB does not match.
  `OverlayTestMode` resolves to `0x417708` in the writable `.data` section.
- `lutdwm/dllmain.cpp` passes a syntax-only MSVC x64 check with 0 errors and no
  new warnings.

### Known limitations

- 26H2 verification is static (signature/RVA + PE structure). A visual SDR/HDR
  test with `examples/TEST-red-blue-swap.cube` on the target GPU/display is
  still required.
- A full Release build was not produced in this environment (MinHook/vcpkg
  package not installed); compile the x64 Release target in Visual Studio as
  described in the README.
- The two back-buffer vtable slots cannot be verified by AOB scanning and need
  runtime testing when DWM changes.
- Windows 11 26H1 / build 28000 remains unsupported.
- Anti-cheat-protected games may block DWM injection or prohibit color filters.
- Disabling DirectFlip/MPO can affect VRR, G-Sync, latency, and GPU usage.

## [4.1.0] - 2026-06-22

### Added

- Release-build diagnostics written from inside `dwm.exe` to `dwm_lut.log`
  beside `DwmLutGUI.exe`.
- 25H2 logging for Present hook entry, back-buffer lookup, texture format,
  monitor coordinate lookup, and configured LUT matching.
- A lean Windows package containing only the runtime files required to use the
  app, the visible test LUT, and a short readme.
- Windows 11 25H2 support for the build 26200 servicing family.
- Updated DWM signatures for `COverlayContext::Present`,
  `IsCandidateDirectFlipCompatible`, and `OverlaysEnabled`.
- D3D11 back-buffer discovery through the 25H2 overlay swap-chain vtable.
- Updated per-monitor coordinate lookup for the 25H2 DWM object layout.
- A transparent, click-through compositor overlay to keep LUTs active when
  DirectFlip would otherwise bypass DWM.

### Fixed

- Corrected Windows 11 25H2 monitor-position lookup. On tested build
  26200.8457, the previous 25H2 path read the composed surface size
  (`1366,768`) as the monitor position, so it could not match the configured
  primary display LUT at (`0,0`).
- Added a 25H2 single-LUT fallback for single-monitor/single-mode setups. If
  exact coordinate matching fails but only one SDR or HDR LUT is configured for
  the current mode, that LUT is applied instead of silently doing nothing.

### Known limitations

- Runtime validation has been performed on a Windows 11 25H2 build 26200.8457
  test system with the included SDR test LUT. Other GPU/display combinations
  should still be tested.

### Changed

- Made the private DirectFlip and MPO helper hooks optional. A cumulative
  update changing an optional signature no longer prevents the DLL from
  initializing when the required Present hook is still available.
- Restored DWM's original `OverlayTestMode` value when unloading instead of
  assuming it was zero.
- Replaced broad future-version detection with exact Windows servicing-family
  checks. Unverified builds newer than 26200 are rejected safely.
- Updated the GUI and package version to 4.1.0.

### Safety

- Closing the window or choosing **Exit** now disables the LUT and unloads the
  DLL from `dwm.exe` first.
- If unloading fails, the close is cancelled and the GUI warns that the LUT may
  still be active.
- Minimizing to the notification area continues to keep the LUT active.

### Known limitations

- Runtime validation must be performed on the target Windows build and GPU.
- Windows 11 26H1/build 28000 is not supported because its private DWM layout
  has not been verified.
- Anti-cheat-protected games may block DWM injection or prohibit color filters.
- Disabling DirectFlip/MPO can affect VRR, G-Sync, latency, and GPU usage.

## Previous releases

Earlier release history is available from the
[lauralex maintenance fork](https://github.com/lauralex/dwm_lut/releases) and
the [original ledoge project](https://github.com/ledoge/dwm_lut/releases).

[Unreleased]: https://github.com/attcai-cn/dwm_lut-windows-26h2/compare/v4.2.1...HEAD
[4.2.1]: https://github.com/attcai-cn/dwm_lut-windows-26h2/releases/tag/v4.2.1
[4.2.0]: https://github.com/attcai-cn/dwm_lut-windows-26h2/releases/tag/v4.2.0
[4.1.0]: https://github.com/attcai-cn/dwm_lut-windows-26h2/releases/tag/v4.1.0
