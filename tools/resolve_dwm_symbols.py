#!/usr/bin/env python3
"""Resolve dwm_lut's private DWM symbols from Microsoft's public PDB symbols.

A Windows cumulative update can recompile and reorder dwmcore.dll, which breaks
byte signatures (AOBs). Instead of guessing new byte patterns, this tool reads
the PE debug directory, downloads the matching dwmcore.pdb from the Microsoft
public symbol server, parses the CodeView public-symbol stream, and prints the
exact RVA of every function/global that dwm_lut hooks.

It never loads, injects or patches dwm.exe. The DLL is only read from disk;
the only network access is the symbol-server download.

Usage:
    python tools/resolve_dwm_symbols.py
    python tools/resolve_dwm_symbols.py D:\\dwm_diag\\dwmcore.dll
    python tools/resolve_dwm_symbols.py D:\\dwm_diag\\dwmcore.dll --json
    python tools/resolve_dwm_symbols.py --cache D:\\dwm_symbols

Requires Python 3.8+ and no third-party packages. The PDB is cached under
%LOCALAPPDATA%\\DwmLut\\symbols by default.

Cross-check the printed Present RVA with:
    python tools/verify_dwm_signatures.py <same-dll>
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import urllib.request

SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"
MSF7_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
S_PUB32 = 0x110E

DEFAULT_DLL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32", "dwmcore.dll")
DEFAULT_CACHE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DwmLut", "symbols")

# Must stay in sync with COverlayContext_Present_bytes_w11_25h2 in dllmain.cpp.
CURRENT_PRESENT_AOB = """
40 55 53 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 F9 48 81 EC ?? ?? ?? ??
48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 45 EF 4C 8B ?? ?? 48 8B ??
"""

KNOWN_PRESENT_RVAS = {
    0x22A230: "26300.9711 / pre-KB5127936 (SDR runtime-verified)",
    0x11E7F4: "26300.9818 / KB5127936 (PDB-verified)",
}

# (label, predicate). Present has several overloads in DWM; the hooked one is
# the COverlayContext overload taking IOverlaySwapChain*.
SYMBOL_RULES = (
    ("COverlayContext::Present",
     lambda n: n.startswith("?Present@COverlayContext@@") and "IOverlaySwapChain" in n),
    ("IsCandidateDirectFlipCompatible",
     lambda n: n.startswith("?IsCandidateDirectFlipCompatible@COverlayContext@@")),
    ("OverlaysEnabled",
     lambda n: n.startswith("?OverlaysEnabled@COverlayContext@@")),
    ("m_dwOverlayTestMode (global)",
     lambda n: "m_dwOverlayTestMode" in n),
)


def parse_pattern(text: str):
    pat = []
    for token in text.split():
        pat.append(None if token in ("??", "?") else int(token, 16))
    return pat


# --------------------------------------------------------------------------
# PE parsing (read-only)
# --------------------------------------------------------------------------
def pe_sections(blob: bytes):
    if blob[:2] != b"MZ":
        raise ValueError("not a PE file (missing MZ)")
    pe = struct.unpack_from("<I", blob, 0x3C)[0]
    if blob[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file (missing PE signature)")
    nsec = struct.unpack_from("<H", blob, pe + 6)[0]
    optsz = struct.unpack_from("<H", blob, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", blob, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    sec_off = opt + optsz
    sections = []
    for i in range(nsec):
        o = sec_off + i * 40
        name = blob[o:o + 8].rstrip(b"\0").decode("latin1")
        vsize, vaddr, rawsize, rawoff = struct.unpack_from("<IIII", blob, o + 8)
        sections.append({"name": name, "vsize": vsize, "vaddr": vaddr,
                         "rawsize": rawsize, "rawoff": rawoff})
    debug_rva, debug_size = struct.unpack_from("<II", blob, dd + 6 * 8)
    return sections, debug_rva, debug_size


def rva_to_offset(sections, rva: int, blob_len: int):
    for s in sections:
        span = max(s["vsize"], s["rawsize"])
        if s["vaddr"] <= rva < s["vaddr"] + span:
            off = s["rawoff"] + (rva - s["vaddr"])
            return off if off < blob_len else None
    return None


def codeview_identity(blob: bytes, sections, debug_rva: int, debug_size: int):
    off = rva_to_offset(sections, debug_rva, len(blob))
    if off is None:
        raise ValueError("debug directory RVA does not map to file data")
    for i in range(debug_size // 28):
        e = off + i * 28
        (_chars, _ts, _maj, _minr, typ, size, _addr, ptr) = struct.unpack_from(
            "<IIHHIIII", blob, e)
        if typ != 2 or not ptr or size < 24:
            continue
        cv = blob[ptr:ptr + size]
        if cv[:4] != b"RSDS":
            continue
        guid = cv[4:20]
        age = struct.unpack_from("<I", cv, 20)[0]
        pdbpath = cv[24:].split(b"\0")[0].decode("latin1", "replace")
        a, b, c = struct.unpack_from("<IHH", guid, 0)
        guid_hex = f"{a:08X}{b:04X}{c:04X}{guid[8:].hex().upper()}"
        name = os.path.basename(pdbpath.replace("\\", "/"))
        return {"pdb_name": name, "pdb_path": pdbpath, "guid": guid_hex,
                "age": age, "key": f"{guid_hex}{age:X}"}
    raise ValueError("no RSDS CodeView record found in the debug directory")


# --------------------------------------------------------------------------
# MSF 7.0 / PDB public symbol stream
# --------------------------------------------------------------------------
def fetch_pdb(identity, cache: str, timeout: int):
    name, key = identity["pdb_name"], identity["key"]
    dest = os.path.join(cache, key, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 32:
        with open(dest, "rb") as f:
            if f.read(32) == MSF7_MAGIC:
                return dest, "cache"
    url = f"{SYMBOL_SERVER}/{name}/{key}/{name}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Microsoft-Symbol-Server/10.0.0.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data.startswith(MSF7_MAGIC):
        raise ValueError(
            "symbol server returned a non-MSF7 payload (compressed PDB or an "
            "error page). Try again later or provide the PDB via --pdb.")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return dest, url


def pdb_public_symbols(blob: bytes, sections):
    if blob[:32] != MSF7_MAGIC:
        raise ValueError("not an MSF 7.00 PDB")
    block_size, _fbm, num_blocks, dir_bytes, _unk, block_map = struct.unpack_from(
        "<IIIIII", blob, 32)

    def read_block(n):
        return blob[n * block_size:(n + 1) * block_size]

    n_dir_blocks = (dir_bytes + block_size - 1) // block_size
    dir_blocks = struct.unpack_from(f"<{n_dir_blocks}I", read_block(block_map))
    directory = b"".join(read_block(n) for n in dir_blocks)[:dir_bytes]

    num_streams = struct.unpack_from("<I", directory, 0)[0]
    sizes = struct.unpack_from(f"<{num_streams}I", directory, 4)
    off = 4 + num_streams * 4
    streams = []
    for size in sizes:
        if size in (0, 0xFFFFFFFF):
            streams.append((0, []))
            continue
        count = (size + block_size - 1) // block_size
        blocks = struct.unpack_from(f"<{count}I", directory, off)
        off += count * 4
        streams.append((size, list(blocks)))

    def stream(idx):
        if idx >= len(streams):
            return b""
        size, blocks = streams[idx]
        return b"".join(read_block(b) for b in blocks)[:size] if blocks else b""

    dbi = stream(3)
    # <iIIHHHHHH -> verHdr, ver, age, globalIdx, build, publicIdx, pdbVer,
    #              symRecordStream, pdbDllRbld
    symrec_idx = struct.unpack_from("<iIIHHHHHH", dbi, 0)[7]
    recs = stream(symrec_idx)

    def seg_rva(seg, offset):
        if 1 <= seg <= len(sections):
            return sections[seg - 1]["vaddr"] + offset
        return None

    pubs = []
    p = 0
    while p + 4 <= len(recs):
        reclen, rectype = struct.unpack_from("<HH", recs, p)
        if reclen < 2 or p + 2 + reclen > len(recs):
            break
        body = recs[p + 4:p + 2 + reclen]
        if rectype == S_PUB32 and len(body) >= 10:
            _flags, offset, seg = struct.unpack_from("<IIH", body, 0)
            name = body[10:].split(b"\0")[0].decode("utf-8", "replace")
            rva = seg_rva(seg, offset)
            if name and rva is not None:
                pubs.append({"name": name, "rva": rva, "seg": seg})
        p += 2 + reclen
    return pubs


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dll", nargs="?", default=DEFAULT_DLL,
                    help="dwmcore.dll to inspect (default: System32\\dwmcore.dll)")
    ap.add_argument("--pdb", help="use a locally downloaded dwmcore.pdb instead of downloading")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="PDB cache directory")
    ap.add_argument("--timeout", type=int, default=90, help="symbol-server timeout in seconds")
    ap.add_argument("--json", action="store_true", help="emit a JSON report")
    args = ap.parse_args(argv)

    path = os.path.abspath(args.dll)
    if not os.path.isfile(path):
        print(f"error: DLL not found: {path}", file=sys.stderr)
        return 2
    blob = open(path, "rb").read()
    try:
        sections, debug_rva, debug_size = pe_sections(blob)
        identity = codeview_identity(blob, sections, debug_rva, debug_size)
    except (ValueError, struct.error) as exc:
        print(f"error: cannot read PE debug information: {exc}", file=sys.stderr)
        return 2

    if args.pdb:
        pdb_path, source = os.path.abspath(args.pdb), "local file"
    else:
        try:
            pdb_path, source = fetch_pdb(identity, args.cache, args.timeout)
        except Exception as exc:
            print(f"error: symbol download failed: {exc}", file=sys.stderr)
            print("The machine needs internet access to msdl.microsoft.com, or pass "
                  "--pdb <dwmcore.pdb>.", file=sys.stderr)
            return 3

    try:
        pubs = pdb_public_symbols(open(pdb_path, "rb").read(), sections)
    except (ValueError, struct.error, IndexError) as exc:
        print(f"error: cannot parse PDB {pdb_path}: {exc}", file=sys.stderr)
        return 2

    resolved = {}
    for label, predicate in SYMBOL_RULES:
        hits = sorted((p for p in pubs if predicate(p["name"])),
                      key=lambda p: p["rva"])
        resolved[label] = hits

    present_hits = resolved["COverlayContext::Present"]
    present_rva = present_hits[0]["rva"] if present_hits else None

    # Cross-check the OverlayTestMode global two independent ways: the PDB
    # symbol RVA, and the RIP-relative operand of OverlaysEnabled's
    # "cmp dword ptr [rip+rel32], 5". Agreement rules out a short-signature
    # false positive, which is what made the 9818 log look confusing.
    overlay_pdb_rva = (resolved["m_dwOverlayTestMode (global)"][0]["rva"]
                       if resolved["m_dwOverlayTestMode (global)"] else None)
    overlay_rip_rva = None
    if resolved["OverlaysEnabled"]:
        ov_rva = resolved["OverlaysEnabled"][0]["rva"]
        ov_off = rva_to_offset(sections, ov_rva, len(blob))
        if ov_off is not None and ov_off + 7 <= len(blob):
            insn = blob[ov_off:ov_off + 7]
            if insn[0] == 0x83 and insn[1] == 0x3D and insn[6] == 0x05:
                rel32 = struct.unpack_from("<i", blob, ov_off + 2)[0]
                overlay_rip_rva = ov_rva + 7 + rel32
    overlay_crosscheck = {
        "pdb_rva": f"0x{overlay_pdb_rva:X}" if overlay_pdb_rva is not None else None,
        "rip_target_rva": f"0x{overlay_rip_rva:X}" if overlay_rip_rva is not None else None,
        "consistent": (overlay_pdb_rva is not None
                       and overlay_rip_rva == overlay_pdb_rva),
    }

    aob = parse_pattern(CURRENT_PRESENT_AOB)
    aob_result = None
    if present_rva is not None:
        off = rva_to_offset(sections, present_rva, len(blob))
        if off is not None and off + len(aob) <= len(blob):
            prologue = blob[off:off + len(aob)]
            mismatches = [i for i, (want, got) in enumerate(zip(aob, prologue))
                          if want is not None and want != got]
            aob_result = {
                "prologue_hex": " ".join(f"{b:02X}" for b in prologue),
                "matches_current_aob": not mismatches,
                "mismatch_offsets": mismatches,
                "suggested_aob": " ".join(
                    "??" if i in mismatches or want is None else f"{got:02X}"
                    for i, (want, got) in enumerate(zip(aob, prologue))),
            }

    report = {
        "dll": path,
        "dll_size": len(blob),
        "pdb": {"name": identity["pdb_name"], "guid": identity["guid"],
                "age": identity["age"], "key": identity["key"],
                "original_path": identity["pdb_path"],
                "source": source, "local_path": pdb_path},
        "public_symbol_count": len(pubs),
        "symbols": {k: [{"rva": f"0x{h['rva']:X}", "name": h["name"]} for h in v]
                    for k, v in resolved.items()},
        "overlay_test_mode_crosscheck": overlay_crosscheck,
        "present_aob_check": aob_result,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if present_hits and aob_result and aob_result["matches_current_aob"] else 1

    print(f"dwmcore.dll : {path}")
    print(f"             {len(blob):,} bytes")
    print(f"PDB         : {identity['pdb_name']}  GUID {identity['guid']}  age {identity['age']}")
    print(f"              key {identity['key']}")
    print(f"              source: {source}")
    print(f"Public symbols parsed: {len(pubs):,}")
    print()
    print("Hooked symbols (authoritative RVAs from Microsoft public symbols):")
    for label, hits in resolved.items():
        if not hits:
            print(f"  {label:<38} NOT FOUND")
        for h in hits:
            print(f"  {label:<38} RVA 0x{h['rva']:X}")
            print(f"      {h['name'][:150]}")

    if present_rva is not None:
        known = KNOWN_PRESENT_RVAS.get(present_rva)
        print()
        print(f"Present RVA 0x{present_rva:X}" + (f" - known build: {known}" if known else
                                                  " - NEW RVA, not in the known-build table"))
        print("Cross-check this value with tools/verify_dwm_signatures.py and the")
        print("'DWM LUT Present RVA=' line in dwm_lut.log before trusting a hook.")
    print()
    print("OverlayTestMode cross-check (PDB symbol vs OverlaysEnabled RIP operand):")
    print(f"  PDB symbol : {overlay_crosscheck['pdb_rva'] or 'not found'}")
    print(f"  RIP target : {overlay_crosscheck['rip_target_rva'] or 'could not decode'}")
    print(f"  consistent : {overlay_crosscheck['consistent']}"
          + ("  -> short OverlaysEnabled AOB is a TRUE hit" if overlay_crosscheck["consistent"]
             else "  -> WARNING: possible short-signature false positive"))
    if aob_result:
        print()
        print(f"Current 46-byte Present AOB matches this DLL: {aob_result['matches_current_aob']}")
        print(f"  prologue: {aob_result['prologue_hex']}")
        if aob_result["mismatch_offsets"]:
            print(f"  differing byte indices: {aob_result['mismatch_offsets']}")
            print(f"  suggested relaxed AOB: {aob_result['suggested_aob']}")
            print("  -> update both dllmain.cpp and tools/verify_dwm_signatures.py,")
            print("     then re-run the red/blue SDR test before shipping.")
    print()
    print("Note: public PDBs do not contain private object-layout information.")
    print("The clip-box offset (0x7698) and the back-buffer vtable slots (24/19)")
    print("still require an actual injection + visual SDR test to confirm.")
    return 0 if present_hits and aob_result and aob_result["matches_current_aob"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
