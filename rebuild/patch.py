#!/usr/bin/env python3
"""
Surgical patcher for the Bun-compiled claude-code binary.

Patches the **JS source** of module #0 (cli.js) inside the ELF ``.bun``
section -- it does NOT edit JSC bytecode.  Bun re-validates bytecode against
the source and falls back to source parsing on mismatch, so changing the
source (and letting the bytecode go stale / be ignored) is sufficient.

Two modes, chosen automatically per run:

* **same-length** -- every needle/replacement pair is the same byte length.
  The replacement is written in place at the module's original contents
  offset; no metadata changes.

* **length-changing (repoint)** -- some replacement is longer than its needle
  (e.g. injecting the 2-stage helper + hook edits).  The new (longer) source
  is written into the module's **stale bytecode region** (payload offset
  ``bytecode_offset``, 152 MB -- plenty of room), the module-table
  ``contents`` StringPointer is repointed to ``(bytecode_offset, new_len)``,
  and the ``bytecode`` StringPointer is nulled to ``(0,0)``.  This reuses the
  bytecode region as physical storage for the longer source; it does NOT edit
  bytecode.  Bun then parses the source and skips the now-empty bytecode.

The module-table entry is located robustly by searching the binary for the
unique ``contents`` StringPointer ``(contents_offset, contents_length)`` --
version-agnostic, no hardcoded table offset.

usage:
    python3 patch.py <base_binary> <out_binary> [patches.json] [--null-bytecode]

patches.json format:
    [
      {"needle": "<base64>", "replacement": "<base64>", "note": "..."}, ...
    ]
needle and replacement may differ in length; the repoint path is used when
the total patched length grows.
"""
import sys, os, json, base64, struct, subprocess, re

TRAILER = b"\n---- Bun! ----\n"


def b64(s): return base64.b64decode(s)


def unbun_modules(binary):
    """Return (payload_start, modules) via unbun. Version-agnostic."""
    out = subprocess.check_output(
        ["npx", "-y", "unbunjs", "list", binary, "--json"],
        stderr=subprocess.DEVNULL,
    )
    j = json.loads(out)
    return j["payload_start"], j["modules"]


def locate_entry(data, contents_offset, contents_length):
    """Find the module-table entry by searching for the unique contents
    StringPointer (offset, length) as two u32 LE.  Returns the FILE offset of
    the entry start (the name field, i.e. 8 bytes before contents.offset)."""
    needle = struct.pack("<II", contents_offset, contents_length)
    hits = [m.start() for m in re.finditer(re.escape(needle), data)]
    if len(hits) != 1:
        raise SystemExit(f"[!] contents StringPointer not unique: {len(hits)} hits")
    contents_field = hits[0]              # file offset of contents.offset field
    entry_file = contents_field - 8       # name StringPointer precedes contents
    # sanity: bytecode field (entry+24) should hold (bytecode_offset, bytecode_len)
    bc_off  = struct.unpack_from("<I", data, entry_file + 24)[0]
    bc_len  = struct.unpack_from("<I", data, entry_file + 28)[0]
    if bc_len == 0:
        raise SystemExit("[!] module already has no bytecode -- nothing to repoint into")
    print(f"[*] module-table entry @ file {entry_file}")
    print(f"    contents ptr=({contents_offset},{contents_length})  bytecode ptr=({bc_off},{bc_len})")
    return entry_file, bc_off, bc_len


def patch(base, out_path, patches_file, null_bytecode=False, entry_index=0):
    with open(base, "rb") as f:
        data = bytearray(f.read())

    payload_start, modules = unbun_modules(base)
    m = modules[entry_index]
    contents_offset = m["contents_offset"]
    contents_length = m["contents_length"]
    contents_file = payload_start + contents_offset
    region = data[contents_file:contents_file + contents_length]
    print(f"[*] module #{entry_index} ({m['name']})")
    print(f"    contents @ file {contents_file}, length {contents_length}")

    patches = json.load(open(patches_file))
    total_changes = 0
    for p in patches:
        needle = b64(p["needle"])
        repl = b64(p["replacement"])
        cnt = region.count(needle)
        if cnt == 0:
            raise SystemExit(f"[!] needle not found: {p.get('note','?')}")
        region = region.replace(needle, repl)
        total_changes += cnt
        dl = len(repl) - len(needle)
        print(f"    [{p.get('note','patch')}] {cnt}x, {len(needle)}->"
              f"{len(repl)} bytes (delta {dl:+d})")

    new_len = len(region)
    growth = new_len - contents_length
    print(f"[*] applied {total_changes} replacement(s); new contents length "
          f"{new_len} (growth {growth:+d})")

    if growth == 0:
        # same-length: in place
        data[contents_file:contents_file + contents_length] = region
        print("[*] same-length patch -- written in place")
        if null_bytecode:
            entry_file, _, _ = locate_entry(data, contents_offset, contents_length)
            struct.pack_into("<I", data, entry_file + 24, 0)  # bytecode.offset
            struct.pack_into("<I", data, entry_file + 28, 0)  # bytecode.length
            print("[*] bytecode ptr nulled (--null-bytecode)")
    elif growth > 0:
        # length-changing: repoint contents into the bytecode region
        entry_file, bc_off, bc_len = locate_entry(data, contents_offset, contents_length)
        if new_len > bc_len:
            raise SystemExit(f"[!] new source ({new_len}) larger than bytecode "
                             f"region ({bc_len}) -- cannot repoint")
        store_file = payload_start + bc_off
        print(f"[*] repoint: writing {new_len} bytes of source into bytecode "
              f"region @ file {store_file} (payload offset {bc_off})")
        # write the new source into the (soon-to-be-freed) bytecode region
        data[store_file:store_file + new_len] = region
        # repoint contents -> bytecode region; null bytecode pointer
        struct.pack_into("<I", data, entry_file + 8, bc_off)       # contents.offset
        struct.pack_into("<I", data, entry_file + 12, new_len)     # contents.length
        struct.pack_into("<I", data, entry_file + 24, 0)           # bytecode.offset
        struct.pack_into("<I", data, entry_file + 28, 0)           # bytecode.length
        # leave the old contents bytes in place (now unreferenced dead data)
        print("[*] contents StringPointer repointed; bytecode StringPointer nulled")
    else:
        raise SystemExit(f"[!] patched source shrank by {-growth} -- not supported")

    with open(out_path, "wb") as f:
        f.write(data)
    os.chmod(out_path, 0o755)
    print(f"[+] wrote {out_path} ({len(data)} bytes)")


if __name__ == "__main__":
    null_bc = "--null-bytecode" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--null-bytecode"]
    if len(args) < 2:
        print(__doc__); sys.exit(1)
    base, out = args[0], args[1]
    patches_file = args[2] if len(args) > 2 else "patches.json"
    patch(base, out, patches_file, null_bc)