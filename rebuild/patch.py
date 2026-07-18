#!/usr/bin/env python3
"""
Surgical in-place patcher for the Bun-compiled claude-code binary.

Workflow:  extract (unbun) -> locate cli.js module #0 -> same-length byte
replace inside the ELF `.bun` section -> write patched binary.

Why same-length:  the module's contents are stored at a fixed (offset,length)
inside the payload blob.  Keeping the replacement byte-for-byte the same length
means NO metadata (offsets/lengths/module-table) has to change -- the patch is
purely local bytes in the `.bun` section.

Why no bytecode handling by default:  Bun re-validates the JSC bytecode against
the source and falls back to source parsing on mismatch.  We verified this
empirically (POC: "Claude" -> "Cl4ude" took effect with no bytecode touching).
--null-bytecode is available as a fallback if a future Bun version stops
falling back.

Offsets are derived dynamically from `unbun list --json` (version-agnostic),
not hardcoded -- so this works across claude-code versions.

usage:
    python3 patch.py <base_binary> <out_binary> [patches.json] [--null-bytecode]

patches.json format:
    [
      {
        "needle": "<base64 of bytes to find>",
        "replacement": "<base64 of replacement bytes (same length)>",
        "note": "human-readable description (optional)"
      }, ...
    ]
"""
import sys, os, json, base64, struct, subprocess

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_LEN = 32          # byte_count(u64)+modules_ptr(8)+entry_id(u4)+exec_argv(8)+flags(u4)


def b64(s): return base64.b64decode(s)

def unbun_modules(binary):
    """Return (payload_start, modules_list) via unbun. Offsets are version-agnostic."""
    out = subprocess.check_output(
        ["npx", "-y", "unbunjs", "list", binary, "--json"],
        stderr=subprocess.DEVNULL,
    )
    j = json.loads(out)
    return j["payload_start"], j["modules"]


def find_entry_bytecode_ptr(data, payload_start, entry_index):
    """Locate module-table entry for `entry_index`, return (file_off_of_bc_offset_field,
    file_off_of_bc_length_field).  Best-effort: assumes Extended 52-byte layout
    with bytecode StringPointer at entry+24.  Only used by --null-bytecode."""
    i = data.rfind(TRAILER)
    if i < 0: raise SystemExit("[!] Bun trailer not found -- cannot null bytecode")
    offsets_file = i - OFFSETS_LEN
    modules_off = struct.unpack_from("<I", data, offsets_file + 8)[0]   # payload-relative
    modules_len = struct.unpack_from("<I", data, offsets_file + 12)[0]
    # entry size unknown; detect via payload? unbun knows the count but we only have
    # the table here.  Use 52 (Extended) -- the layout claude-code ships with.
    entry_size = 52
    n = modules_len // entry_size
    table_file = payload_start + modules_off
    entry_file = table_file + entry_index * entry_size
    assert entry_index < n, f"entry {entry_index} >= count {n}"
    return entry_file + 24, entry_file + 28   # bytecode.offset, bytecode.length


def patch(base, out_path, patches_file, null_bytecode=False, entry_index=0):
    with open(base, "rb") as f:
        data = bytearray(f.read())

    payload_start, modules = unbun_modules(base)
    m = modules[entry_index]
    contents_file = payload_start + m["contents_offset"]
    contents_len = m["contents_length"]
    region = data[contents_file:contents_file + contents_len]
    print(f"[*] module #{entry_index} ({m['name']})")
    print(f"    contents @ file {contents_file}, length {contents_len}")

    patches = json.load(open(patches_file))
    total_changes = 0
    for p in patches:
        needle = b64(p["needle"])
        repl = b64(p["replacement"])
        if len(needle) != len(repl):
            raise SystemExit(f"[!] length mismatch ({len(needle)} != {len(repl)}): "
                             f"{p.get('note','?')}")
        cnt = region.count(needle)
        if cnt == 0:
            raise SystemExit(f"[!] needle not found: {p.get('note','?')}")
        region = region.replace(needle, repl)
        total_changes += cnt
        print(f"    [{p.get('note','patch')}] {cnt}x occurrence, {len(needle)} bytes")

    if len(region) != contents_len:
        raise SystemExit("[!] region length changed -- aborting")
    data[contents_file:contents_file + contents_len] = region
    print(f"[*] applied {total_changes} replacement(s), length preserved")

    if null_bytecode:
        off_f, len_f = find_entry_bytecode_ptr(data, payload_start, entry_index)
        old = (struct.unpack_from("<I", data, off_f)[0],
               struct.unpack_from("<I", data, len_f)[0])
        struct.pack_into("<I", data, off_f, 0)
        struct.pack_into("<I", data, len_f, 0)
        print(f"[*] bytecode ptr nulled (was offset={old[0]} len={old[1]})")

    with open(out_path, "wb") as f:
        f.write(data)
    os.chmod(out_path, 0o755)
    print(f"[+] wrote {out_path} ({len(data)} bytes)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--null-bytecode"]
    null_bc = "--null-bytecode" in sys.argv
    if len(args) < 2:
        print(__doc__); sys.exit(1)
    base, out = args[0], args[1]
    patches_file = args[2] if len(args) > 2 else "patches.json"
    patch(base, out, patches_file, null_bc)