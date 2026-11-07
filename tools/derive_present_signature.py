#!/usr/bin/env python3
"""Locate COverlayContext::Present in a dwmcore.dll whose AOB signature broke.

Windows cumulative updates recompile DWM. The Present hook is found by a
46-byte function-prologue AOB, so even a harmless change to the stack frame or
register saves makes it stop matching. When that happens the DLL deliberately
refuses to hook (see "DWM LUT refusing to hook" in dwm_lut.log).

This script tries to re-locate Present without the old prologue. It carries
short "anchors" taken from the *body* of Present on build 26300.9711 - mostly
register/stack instructions with no relative addresses, which tend to survive
recompilation. Each anchor is also tried with a 4-byte window wildcarded, to
tolerate a changed rel32 call/jump operand. Anchors that match exactly once
vote for a candidate function start (hit_rva - offset_in_old_function).

Usage (run on the affected machine):
    python tools/derive_present_signature.py
    python tools/derive_present_signature.py C:\\Windows\\System32\\dwmcore.dll
    python tools/derive_present_signature.py --export D:\\dwm_diag
    python tools/derive_present_signature.py --json

--export copies dwmcore.dll and writes a JSON report, so the file can be sent
to a maintainer for full offline analysis if no anchor survives.

Read-only by default. Never loads, injects or patches dwm.exe.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import shutil
import struct
import sys
from collections import Counter
from dataclasses import dataclass

# Body windows from COverlayContext::Present on 26H2 build 26300.9711,
# Present RVA 0x22A230, dwmcore.dll 4,476,928 bytes, PE timestamp 0x9233E01A.
# (byte offset from the function start, 20-byte hex window)
REFERENCE = {
    "build": "26300.9711",
    "present_rva": 0x22A230,
    "file_size": 4476928,
    "pe_timestamp": 0x9233E01A,
}
ANCHORS = [
    (0x030, "09 4D 8B F9 48 83 C1 08 45 8B F0 48 8B F2 48 8B 01 48 8B 40"),
    (0x040, "01 48 8B 40 20 E8 96 0D 0D 00 48 8B BB 28 4C 00 00 4C 8B E8"),
    (0x050, "00 4C 8B E8 C6 44 24 30 00 48 85 FF 0F 85 BD 02 00 00 40 88"),
    (0x060, "00 00 40 88 BB 72 4C 00 00 40 38 7D 7F 0F 85 E2 00 00 00 48"),
    (0x070, "00 00 00 48 8B CB E8 B1 B2 FA FF 84 C0 0F 85 D2 00 00 00 48"),
    (0x080, "00 00 00 48 8D 44 24 30 89 7C 24 48 48 89 44 24 28 4D 8B CF"),
    (0x090, "28 4D 8B CF 48 8D 44 24 48 45 8B C6 48 8B D6 48 89 44 24 20"),
    (0x0A0, "89 44 24 20 E8 4F 04 00 00 8B F0 85 C0 79 1D 48 89 7C 24 28"),
    (0x0B0, "89 7C 24 28 8D 4F 14 44 8B C8 C7 44 24 20 F6 0B 00 00 45 33"),
    (0x0C0, "00 00 45 33 C0 33 D2 E8 24 99 E8 FF F6 05 7D BD 1E 00 01 0F"),
    (0x0D0, "1E 00 01 0F 84 CF 01 00 00 0F B6 44 24 30 48 8D 15 43 BC 16"),
    (0x0E0, "15 43 BC 16 00 89 44 24 40 48 8D 0D E0 A7 1E 00 8B 44 24 48"),
    (0x0F0, "8B 44 24 48 41 B9 05 00 00 00 89 44 24 38 48 8D 44 24 60 48"),
    (0x100, "44 24 60 48 89 45 9F 48 8D 44 24 38 48 89 45 AF 48 8D 44 24"),
]

# The current required signature, for a before/after sanity check.
PRESENT_AOB = """
40 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 F9 48 81 EC F8 00 00 00
48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 45 EF 4C 8B 65 ?? 48 8B D9
"""

# Short signatures that often still match after an update. A hit here does NOT
# prove the pointer is correct - verify the RVA moves plausibly.
DIRECTFLIP_AOB = "48 8B C4 48 89 58 08 48 89 68 10 48 89 70 18 48 89 78 20 41 56 48 83 EC 20 33 DB"
OVERLAYS_AOB = "83 3D ?? ?? ?? ?? 05 74 09 83 79 28 01 0F 97 C0 C3"


@dataclass
class Section:
    name: str
    vaddr: int
    vsize: int
    raw_off: int
    raw_size: int
    executable: bool
    writable: bool


def parse_pe(blob: bytes):
    if blob[:2] != b"MZ":
        raise ValueError("not a PE image (missing MZ header)")
    pe_off = struct.unpack_from("<I", blob, 0x3C)[0]
    if blob[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("not a PE image (missing PE signature)")
    coff = pe_off + 4
    machine, nsec, timestamp, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", blob, coff)
    if machine != 0x8664:
        raise ValueError(f"expected x64 PE, got machine 0x{machine:04X}")
    opt = coff + 20
    image_base = struct.unpack_from("<Q", blob, opt + 24)[0]
    size_of_image = struct.unpack_from("<I", blob, opt + 56)[0]
    table = opt + opt_size
    sections = []
    for i in range(nsec):
        o = table + i * 40
        name = blob[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rsize, roff = struct.unpack_from("<IIII", blob, o + 8)
        flags = struct.unpack_from("<I", blob, o + 36)[0]
        sections.append(Section(name, vaddr, vsize, roff, rsize,
                                bool(flags & 0x20000000), bool(flags & 0x80000000)))
    return sections, image_base, size_of_image, timestamp


def parse_pattern(text: str):
    data, mask = bytearray(), bytearray()
    for tok in text.split():
        if tok in ("?", "??"):
            data.append(0)
            mask.append(0)
        else:
            data.append(int(tok, 16))
            mask.append(1)
    return bytes(data), bytes(mask)


def rva_to_file(sections, rva: int):
    for s in sections:
        if s.vaddr <= rva < s.vaddr + max(s.vsize, s.raw_size):
            return s.raw_off + (rva - s.vaddr)
    return None


def scan(blob: bytes, sections, pattern: str, limit_hits=8):
    """Return RVAs of matches inside executable sections."""
    data, mask = parse_pattern(pattern)
    n = len(data)
    anchor = next(i for i, bit in enumerate(mask) if bit)
    anchor_byte = data[anchor]
    hits = []
    for sec in sections:
        if not sec.executable:
            continue
        start, end = sec.raw_off, min(sec.raw_off + sec.raw_size, len(blob))
        buf = blob[start:end]
        limit = len(buf) - n
        if limit < 0:
            continue
        pos = buf.find(bytes([anchor_byte]), anchor, limit + anchor + 1)
        while pos != -1:
            cand = pos - anchor
            if 0 <= cand <= limit and all(
                not m or buf[cand + i] == data[i] for i, m in enumerate(mask)
            ):
                hits.append(sec.vaddr + cand)
                if len(hits) >= limit_hits:
                    return hits
            pos = buf.find(bytes([anchor_byte]), pos + 1, limit + anchor + 1)
    return hits


def wildcard_variants(pattern: str):
    """Yield (label, pattern). Original plus each possible 4-byte wildcard hole."""
    yield "exact", pattern
    toks = pattern.split()
    for i in range(len(toks) - 3):
        variant = list(toks)
        variant[i:i + 4] = ["??"] * 4
        yield f"wild+{i}", " ".join(variant)


def file_version(path: str) -> str:
    try:
        v = ctypes.windll.version
        v.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        v.GetFileVersionInfoSizeW.restype = ctypes.wintypes.DWORD
        v.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.wintypes.DWORD,
                                          ctypes.wintypes.DWORD, ctypes.c_void_p]
        v.GetFileVersionInfoW.restype = ctypes.wintypes.BOOL
        v.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                     ctypes.c_void_p, ctypes.c_void_p]
        v.VerQueryValueW.restype = ctypes.wintypes.BOOL
        size = v.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not v.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, length = ctypes.c_void_p(), ctypes.c_uint()
        if not v.VerQueryValueW(buf, "\\VS_FIXEDFILEINFO", ctypes.byref(ptr), ctypes.byref(length)):
            if not v.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
                return ""
        if length.value < 40 or not ptr.value:
            return ""
        class FFI(ctypes.Structure):
            _fields_ = [("sig", ctypes.wintypes.DWORD), ("sv", ctypes.wintypes.DWORD),
                        ("ms", ctypes.wintypes.DWORD), ("ls", ctypes.wintypes.DWORD)] + [
                        ("f%d" % i, ctypes.wintypes.DWORD) for i in range(9)]
        f = ctypes.cast(ptr, ctypes.POINTER(FFI)).contents
        return f"{f.ms >> 16}.{f.ms & 0xFFFF}.{f.ls >> 16}.{f.ls & 0xFFFF}"
    except Exception:
        return ""


def windows_info():
    out = {}
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        for n in ("ProductName", "DisplayVersion", "CurrentBuild", "UBR", "BuildLabEx"):
            try:
                out[n] = winreg.QueryValueEx(k, n)[0]
            except OSError:
                pass
        winreg.CloseKey(k)
    except Exception as e:
        out["error"] = str(e)
    return out


def hexdump(blob: bytes, start: int, before=0, length=128):
    s = max(0, start - before)
    lines = []
    for o in range(s, min(s + length, len(blob)), 16):
        chunk = blob[o:o + 16]
        lines.append(f"  {o:08X}  " + " ".join(f"{b:02X}" for b in chunk))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dll", nargs="?",
                    default=os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                         "System32", "dwmcore.dll"),
                    help="path to dwmcore.dll (default: System32)")
    ap.add_argument("--export", metavar="DIR",
                    help="also copy dwmcore.dll and write report.json into DIR")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    path = os.path.abspath(args.dll)
    if not os.path.isfile(path):
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    blob = open(path, "rb").read()
    sections, image_base, image_size, timestamp = parse_pe(blob)

    os_info = windows_info()
    sha = hashlib.sha256(blob).hexdigest()
    report = {
        "dll": path,
        "file_size": len(blob),
        "sha256": sha,
        "file_version": file_version(path),
        "pe_timestamp": f"0x{timestamp:08X}",
        "image_size": f"0x{image_size:X}",
        "os": os_info,
        "reference": REFERENCE,
        "sections": [{"name": s.name, "rva": f"0x{s.vaddr:X}",
                      "raw_size": s.raw_size, "executable": s.executable,
                      "writable": s.writable} for s in sections],
        "known_signatures": {},
        "anchors": [],
        "candidates": [],
    }

    known = {
        "Present (current AOB)": PRESENT_AOB,
        "IsCandidateDirectFlipCompatible": DIRECTFLIP_AOB,
        "OverlaysEnabled": OVERLAYS_AOB,
    }
    for name, pat in known.items():
        hits = scan(blob, sections, pat, limit_hits=16)
        report["known_signatures"][name] = {
            "hits": len(hits),
            "rvas": [f"0x{h:X}" for h in hits],
        }

    votes = Counter()
    anchor_results = []
    for off, pat in ANCHORS:
        best = None
        for label, variant in wildcard_variants(pat):
            hits = scan(blob, sections, variant, limit_hits=2)
            if len(hits) == 1:
                best = (label, hits[0])
                break
        if best:
            label, rva = best
            candidate = rva - off
            votes[candidate] += 1
            anchor_results.append({
                "offset": f"0x{off:X}", "variant": label,
                "hit_rva": f"0x{rva:X}", "implied_start": f"0x{candidate:X}",
            })
        else:
            label = "no unique hit"
            anchor_results.append({
                "offset": f"0x{off:X}", "variant": label,
                "hit_rva": None, "implied_start": None,
            })
    report["anchors"] = anchor_results

    ranked = []
    for rva, count in votes.most_common(8):
        fo = rva_to_file(sections, rva)
        if fo is None:
            continue
        prologue = blob[fo:fo + 46]
        # wildcard the 4-byte stack-cookie RIP operand after 48 8B 05,
        # and the single-byte 4C 8B 65 ?? displacement.
        toks = [f"{b:02X}" for b in prologue]
        for i in range(len(toks) - 5):
            if toks[i:i + 3] == ["48", "8B", "05"]:
                toks[i + 3:i + 7] = ["??"] * 4
            if toks[i:i + 3] == ["4C", "8B", "65"]:
                toks[i + 3] = "??"
        ranked.append({
            "rva": f"0x{rva:X}",
            "votes": count,
            "suggested_aob": " ".join(toks),
            "bytes_before_and_at_start": hexdump(blob, fo, before=64, length=128),
        })
    report["candidates"] = ranked

    if args.export:
        os.makedirs(args.export, exist_ok=True)
        dst = os.path.join(args.export, "dwmcore.dll")
        shutil.copy2(path, dst)
        with open(os.path.join(args.export, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        report["export_dir"] = os.path.abspath(args.export)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("=== dwmcore.dll ===")
    print(f"  path        : {path}")
    print(f"  OS          : {os_info.get('DisplayVersion', '?')} "
          f"(build {os_info.get('CurrentBuild', '?')}.{os_info.get('UBR', '?')})")
    print(f"  file version: {report['file_version'] or '?'}")
    print(f"  size        : {len(blob):,} bytes   imageSize 0x{image_size:X}")
    print(f"  sha256      : {sha}")
    print(f"  PE timestamp: 0x{timestamp:08X}")
    print(f"  reference   : build {REFERENCE['build']}, {REFERENCE['file_size']:,} bytes, "
          f"Present RVA 0x{REFERENCE['present_rva']:X}")
    changed = (len(blob) != REFERENCE["file_size"] or timestamp != REFERENCE["pe_timestamp"])
    print(f"  vs reference: {'CHANGED (a cumulative update replaced DWM)' if changed else 'same binary'}")
    print()
    print("=== current signatures ===")
    for name, r in report["known_signatures"].items():
        rvas = ", ".join(r["rvas"][:4]) or "-"
        print(f"  {name:<38} hits={r['hits']:<3} {rvas}")
    print()
    print("=== body anchors (vote for the new Present start) ===")
    for a in anchor_results:
        print(f"  +{a['offset']:<6} {a['variant']:<12} "
              f"hit={a['hit_rva'] or '-':<12} implied start={a['implied_start'] or '-'}")
    survived = sum(1 for a in anchor_results if a["hit_rva"])
    print(f"\n  {survived}/{len(ANCHORS)} anchors produced a unique hit.")
    if not votes:
        print("\n  No anchor survived: Present was substantially rewritten.")
        print("  This is expected on KB5127936 / 26300.9818, which reordered and")
        print("  recompiled the function (0/14 anchors on that DLL).")
        print("  Use the authoritative PDB route instead:")
        print("      python tools/resolve_dwm_symbols.py <dwmcore.dll>")
        print("  If the symbol server is unreachable, re-run with --export DIR and")
        print("  send dwmcore.dll + report.json to the maintainer.")
        return 1
    print(f"\n=== candidate Present start(s) ===")
    for c in ranked[:3]:
        print(f"  RVA {c['rva']}  votes={c['votes']}")
        print(f"  suggested AOB ({len(c['suggested_aob'].split())} bytes):")
        print(f"    {c['suggested_aob']}")
        print("  surrounding bytes (64 before / 64 after):")
        print(c["bytes_before_and_at_start"])
        print()
    print("NOTES")
    print("  * A unique anchor hit is evidence, not proof. Check the suggested AOB")
    print("    against a disassembler before putting it into dllmain.cpp, and verify")
    print("    the hook's back-buffer vtable chain at runtime.")
    print("  * Short DirectFlip/OverlaysEnabled hits after an update can look like")
    print("    false positives but may be genuine - DWM reorders functions by")
    print("    hundreds of KB. Cross-check with tools/resolve_dwm_symbols.py, which")
    print("    compares the OverlaysEnabled RIP target with the PDB global symbol.")
    print("  * While broken, the DLL correctly refuses to hook. Do NOT relax that")
    print("    guard: hooking the wrong address can crash dwm.exe.")
    if args.export:
        print(f"\n  Exported dwmcore.dll and report.json to {report['export_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
