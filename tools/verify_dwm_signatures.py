#!/usr/bin/env python3
"""Verify dwm_lut AOB signatures against a local dwmcore.dll.

The DLL hooks undocumented DWM internals by scanning dwmcore.dll for byte
signatures. A cumulative update can silently invalidate them. Run this after
every Windows update (and on every new feature update) to check which
servicing-family signatures still resolve, before injecting anything.

Usage:
    python tools/verify_dwm_signatures.py
    python tools/verify_dwm_signatures.py C:\\Windows\\System32\\dwmcore.dll
    python tools/verify_dwm_signatures.py --json

Only reads the PE file from disk. It never loads, injects or patches dwm.exe.
Signatures must stay in sync with the arrays in lutdwm/dllmain.cpp.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import struct
import sys
from dataclasses import dataclass, field

# '??' is a one-byte wildcard, matching the '?' entries in dllmain.cpp.
PRESENT_25H2 = """
40 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 F9 48 81 EC F8 00 00 00
48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 45 EF 4C 8B 65 ?? 48 8B D9
"""
DIRECTFLIP_25H2 = """
48 8B C4 48 89 58 08 48 89 68 10 48 89 70 18 48 89 78 20 41 56 48 83 EC 20 33 DB
"""
OVERLAYS_25H2 = "83 3D ?? ?? ?? ?? 05 74 09 83 79 28 01 0F 97 C0 C3"

PRESENT_24H2 = "4C 8B DC 56 41 56"
DIRECTFLIP_24H2 = """
48 8B C4 48 89 58 ?? 48 89 68 ?? 48 89 70 ?? 48 89 78 ?? 41 56 48 83 EC 20 33 DB
"""
OVERLAYS_24H2 = "E8 ?? ?? ?? ?? 84 C0 B8 04 00 00 00"

PRESENT_W11 = """
40 53 55 56 57 41 56 41 57 48 81 EC 88 00 00 00 48 8B 05 ?? ?? ?? ??
48 33 C4 48 89 44 24 78 48
"""
DIRECTFLIP_W11 = "40 55 53 56 57 41 54 41 55 41 56 41 57 48 8B EC 48 83 EC 68 48"
OVERLAYS_W11 = "83 3D ?? ?? ?? ?? ?? 75 04"

PRESENT_W10 = """
48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 40 48 8B B1 20 2C 00 00
45 8B D0 48 8B FA 48 8B D9 48 85 F6 0F 85
"""
DIRECTFLIP_W10 = "48 89 7C 24 20 55 41 54 41 55 41 56 41 57 48 8B EC 48 83 EC 40"
OVERLAYS_W10 = "75 04 32 C0 C3 CC 83 79 30 01 0F 97 C0 C3"


@dataclass(frozen=True)
class Signature:
    family: str
    name: str
    pattern: str
    required: bool = False


SIGNATURES = [
    Signature("25H2/26H2", "COverlayContext::Present", PRESENT_25H2, True),
    Signature("25H2/26H2", "IsCandidateDirectFlipCompatible", DIRECTFLIP_25H2),
    Signature("25H2/26H2", "OverlaysEnabled", OVERLAYS_25H2),
    Signature("24H2", "COverlayContext::Present", PRESENT_24H2, True),
    Signature("24H2", "IsCandidateDirectFlipCompatible", DIRECTFLIP_24H2),
    Signature("24H2", "OverlaysEnabled (call site)", OVERLAYS_24H2),
    Signature("W11 21H2-23H2", "COverlayContext::Present", PRESENT_W11, True),
    Signature("W11 21H2-23H2", "IsCandidateDirectFlipCompatible", DIRECTFLIP_W11),
    Signature("W11 21H2-23H2", "OverlaysEnabled", OVERLAYS_W11),
    Signature("W10", "COverlayContext::Present", PRESENT_W10, True),
    Signature("W10", "IsCandidateDirectFlipCompatible", DIRECTFLIP_W10),
    Signature("W10", "OverlaysEnabled", OVERLAYS_W10),
]


@dataclass
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    executable: bool
    writable: bool


@dataclass
class Match:
    rva: int
    file_offset: int
    section: str


@dataclass
class Result:
    signature: Signature
    length: int
    matches: list[Match] = field(default_factory=list)


def parse_pattern(text: str) -> tuple[bytes, bytes]:
    """Return (bytes, mask) where mask bit 1 means 'must match'."""
    data = bytearray()
    mask = bytearray()
    for token in text.split():
        if token in ("?", "??"):
            data.append(0)
            mask.append(0)
        else:
            data.append(int(token, 16))
            mask.append(1)
    return bytes(data), bytes(mask)


def pe_sections(blob: bytes) -> tuple[list[Section], int, int]:
    if blob[:2] != b"MZ":
        raise ValueError("not a PE image (missing MZ header)")
    pe_off = struct.unpack_from("<I", blob, 0x3C)[0]
    if blob[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("not a PE image (missing PE signature)")
    coff = pe_off + 4
    machine, num_sections, timestamp, _, _, opt_size, characteristics = struct.unpack_from(
        "<HHIIIHH", blob, coff
    )
    if machine != 0x8664:
        raise ValueError(f"expected x64 PE, got machine 0x{machine:04X}")
    opt = coff + 20
    image_base = struct.unpack_from("<Q", blob, opt + 24)[0]
    section_table = opt + opt_size
    sections = []
    for i in range(num_sections):
        off = section_table + i * 40
        name = blob[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, raw_size, raw_off = struct.unpack_from("<IIII", blob, off + 8)
        flags = struct.unpack_from("<I", blob, off + 36)[0]
        sections.append(Section(name, vaddr, vsize, raw_off, raw_size,
                                bool(flags & 0x20000000), bool(flags & 0x80000000)))
    return sections, image_base, timestamp


def scan(blob: bytes, sections: list[Section], pattern: str) -> tuple[int, list[Match]]:
    data, mask = parse_pattern(pattern)
    n = len(data)
    if n == 0:
        raise ValueError("empty pattern")
    # Fast path: anchor on the first non-wildcard byte.
    anchor = next(i for i, bit in enumerate(mask) if bit)
    anchor_byte = data[anchor]
    matches: list[Match] = []
    for sec in sections:
        if not sec.executable:
            continue
        start = sec.raw_offset
        end = min(start + sec.raw_size, len(blob))
        buf = blob[start:end]
        limit = len(buf) - n
        if limit < 0:
            continue
        pos = buf.find(bytes([anchor_byte]), anchor, limit + anchor + 1)
        while pos != -1:
            candidate = pos - anchor
            if candidate >= 0 and candidate <= limit:
                ok = True
                for i, bit in enumerate(mask):
                    if bit and buf[candidate + i] != data[i]:
                        ok = False
                        break
                if ok:
                    file_off = start + candidate
                    rva = sec.virtual_address + candidate
                    matches.append(Match(rva, file_off, sec.name))
            pos = buf.find(bytes([anchor_byte]), pos + 1, limit + anchor + 1)
    return n, matches


def windows_info() -> dict:
    info = {}
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        )
        for name in ("ProductName", "DisplayVersion", "CurrentBuild", "UBR", "BuildLabEx"):
            try:
                value, _ = winreg.QueryValueEx(key, name)
                info[name] = value
            except OSError:
                pass
        winreg.CloseKey(key)
    except Exception as exc:  # pragma: no cover - non-Windows / restricted
        info["error"] = str(exc)
    return info


def file_version(path: str) -> str:
    try:
        version = ctypes.windll.version
        version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        version.GetFileVersionInfoSizeW.restype = ctypes.wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.wintypes.DWORD,
                                               ctypes.wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = ctypes.wintypes.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                           ctypes.c_void_p, ctypes.c_void_p]
        version.VerQueryValueW.restype = ctypes.wintypes.BOOL

        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr = ctypes.c_void_p()
        length = ctypes.c_uint()
        # Some system binaries answer "\\" (the VS_VERSIONINFO root value) but
        # not the "\\VS_FIXEDFILEINFO" sub-query, so try both.
        if not version.VerQueryValueW(buf, "\\VS_FIXEDFILEINFO", ctypes.byref(ptr), ctypes.byref(length)):
            if not version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
                return ""
        if length.value < 40 or not ptr.value:
            return ""
        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.wintypes.DWORD),
                ("dwStrucVersion", ctypes.wintypes.DWORD),
                ("dwFileVersionMS", ctypes.wintypes.DWORD),
                ("dwFileVersionLS", ctypes.wintypes.DWORD),
                ("dwProductVersionMS", ctypes.wintypes.DWORD),
                ("dwProductVersionLS", ctypes.wintypes.DWORD),
                ("dwFileFlagsMask", ctypes.wintypes.DWORD),
                ("dwFileFlags", ctypes.wintypes.DWORD),
                ("dwFileOS", ctypes.wintypes.DWORD),
                ("dwFileType", ctypes.wintypes.DWORD),
                ("dwFileSubtype", ctypes.wintypes.DWORD),
                ("dwFileDateMS", ctypes.wintypes.DWORD),
                ("dwFileDateLS", ctypes.wintypes.DWORD),
            ]
        fixed = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = fixed.dwFileVersionMS, fixed.dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "dll", nargs="?",
        default=os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "dwmcore.dll"),
        help="path to dwmcore.dll (default: the one in System32)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    path = os.path.abspath(args.dll)
    if not os.path.isfile(path):
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    with open(path, "rb") as fh:
        blob = fh.read()

    sections, image_base, timestamp = pe_sections(blob)
    results = []
    for sig in SIGNATURES:
        length, matches = scan(blob, sections, sig.pattern)
        results.append(Result(sig, length, matches))

    # dllmain.cpp derives DWM's OverlayTestMode global from the 25H2
    # OverlaysEnabled instruction: 83 3D <rel32> 05 (cmp [rip+rel32], 5).
    # Validate that the computed RIP target lands inside a writable section.
    overlay_target = None
    overlay_hit = next(
        (r.matches[0] for r in results
         if r.signature.family == "25H2/26H2" and r.signature.name == "OverlaysEnabled" and len(r.matches) == 1),
        None,
    )
    if overlay_hit is not None:
        rel32 = struct.unpack_from("<i", blob, overlay_hit.file_offset + 2)[0]
        target_rva = overlay_hit.rva + 7 + rel32
        owner = next(
            (s for s in sections
             if s.virtual_address <= target_rva < s.virtual_address + max(s.virtual_size, s.raw_size)),
            None,
        )
        overlay_target = {
            "rel32": rel32,
            "target_rva": f"0x{target_rva:X}",
            "section": owner.name if owner else None,
            "writable": owner.writable if owner else False,
            "plausible": owner is not None,
        }

    os_info = windows_info()
    report = {
        "dll": path,
        "overlay_test_mode_target": overlay_target,
        "os": os_info,
        "file_version": file_version(path),
        "file_size": len(blob),
        "image_base": f"0x{image_base:X}",
        "pe_timestamp": timestamp,
        "sections": [
            {"name": s.name, "rva": f"0x{s.virtual_address:X}", "raw_size": s.raw_size,
             "executable": s.executable, "writable": s.writable}
            for s in sections
        ],
        "results": [
            {
                "family": r.signature.family,
                "name": r.signature.name,
                "required": r.signature.required,
                "length": r.length,
                "hits": len(r.matches),
                "matches": [
                    {"rva": f"0x{m.rva:X}", "file_offset": f"0x{m.file_offset:X}", "section": m.section}
                    for m in r.matches[:16]
                ],
            }
            for r in results
        ],
    }

    # A family is usable when its required Present signature resolves exactly once.
    family_status = {}
    for r in results:
        if r.signature.required:
            required_total = sum(1 for x in results if x.signature.family == r.signature.family and x.signature.required)
            family_status.setdefault(r.signature.family, {"required": required_total, "resolved": 0, "hits": 0})
            family_status[r.signature.family]["hits"] += len(r.matches)
            if len(r.matches) == 1:
                family_status[r.signature.family]["resolved"] += 1
    report["family_status"] = family_status

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(f"dwmcore.dll : {path}")
    print(f"OS          : {os_info.get('DisplayVersion', '?')} "
          f"(build {os_info.get('CurrentBuild', '?')}.{os_info.get('UBR', '?')})")
    print(f"File version: {report['file_version'] or '?'}   Size: {len(blob):,} bytes")
    print(f"Image base  : 0x{image_base:X}   PE timestamp: 0x{timestamp:08X}")
    print()
    header = f"{'Family':<16} {'Signature':<38} {'Len':>4} {'Hits':>5}  First RVA"
    print(header)
    print("-" * len(header))
    for r in results:
        first = f"0x{r.matches[0].rva:X}" if r.matches else "-"
        flag = "*" if r.signature.required else " "
        print(f"{r.signature.family:<16} {flag + r.signature.name:<38} {r.length:>4} {len(r.matches):>5}  {first}")
    print()
    print("Family status (required = unique Present signature):")
    for family, st in family_status.items():
        verdict = "USABLE" if st["resolved"] == st["required"] else "NOT USABLE"
        print(f"  {family:<16} {verdict:<12} resolved {st['resolved']}/{st['required']}, hits {st['hits']}")
    modern = family_status.get("25H2/26H2")
    if modern and modern["resolved"] != modern["required"]:
        print()
        print("  25H2/26H2 Present AOB did not match - a cumulative update most likely")
        print("  replaced dwmcore.dll. Do NOT force the hook. To locate the new Present")
        print("  and/or collect a diagnostic bundle, run:")
        print("      python tools/derive_present_signature.py --export D:\\dwm_diag")
    if overlay_target:
        print(f"\nOverlayTestMode global (from OverlaysEnabled RIP-relative operand):")
        print(f"  target {overlay_target['target_rva']} in {overlay_target['section']} "
              f"(writable={overlay_target['writable']}, plausible={overlay_target['plausible']})")
    print()
    print("* = required hook. More than one hit means the AOB is ambiguous and unsafe.")
    print("25H2/26H2 share the ge_release servicing branch; a USABLE 25H2/26H2 result")
    print("means the existing 25H2 signatures and offsets can be reused on 26H2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
