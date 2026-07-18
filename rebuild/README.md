# rebuild/ — patched claude-code binary builder

Self-contained workspace for surgically patching the **official prebuilt**
claude-code binary and producing a runnable patched binary. Lives under
`claude-code/rebuild/` and is git-ignored (see `.git/info/exclude`).

## What this is (and isn't)

- The claude-code binary is a Bun `--compile` single executable. Its JS source
  is **not published** (the GitHub repo + npm package only ship docs / a
  wrapper). So we cannot "compile from source."
- We **can** extract the embedded `cli.js` from the `.bun` ELF section and
  patch its bytes in place. Bun re-validates JSC bytecode against the source
  and falls back to source parsing on mismatch — so a same-length source patch
  takes effect with no bytecode handling. (Verified empirically.)

## Layout

```
rebuild/
  build.sh        orchestration: fetch base / extract / patch / verify
  patch.py         surgical same-length byte patcher (offsets via `unbun`)
  patches.json     list of {needle, replacement} base64 patches to apply
  claude.orig      base binary (265 MB) — copied in, or fetched by build.sh
  extracted/       unbun output for inspection (bytecode omitted to save space)
  out/             built patched binaries land here
```

## Usage

```bash
cd rebuild

# build with current patches.json
./build.sh                         # -> out/claude.patched

# inspect/search the bundled cli.js for a bug to patch
./build.sh inspect                 # (re)extracts extracted/src/entrypoints/cli.js
grep -ao 'something' extracted/src/entrypoints/cli.js

# clean outputs (keeps base + extracted)
./build.sh clean
```

`build.sh` honors env: `CLAUDE_VERSION=2.1.214 PLATFORM=linux-x64 ./build.sh`.

## Writing a patch

A patch is a same-length byte find/replace inside `cli.js`. Add to
`patches.json`:

```json
[
  { "needle": "<base64 of bytes to find>",
    "replacement": "<base64 of replacement bytes, SAME LENGTH>",
    "note": "what this fixes" }
]
```

Generate the base64 with python, e.g.:

```python
import base64; print(base64.b64encode(b"old bytes").decode())
```

### If the fix changes length

Same-length keeps all offsets/lengths valid — preferred. If a fix genuinely
needs to grow, options (in order of effort):

1. Pad the replacement to equal length with a trailing comment / whitespace.
2. Repoint the module's `contents` StringPointer to a free region of the
   payload and adjust the length field (requires extending patch.py).
3. `--null-bytecode` + recompile path (not currently wired; Bun would be needed).

`patch.py --null-bytecode` zeros the bytecode StringPointer so Bun parses the
(patched) source. Only needed if a future Bun stops auto-falling-back.

## Caveats

- A patched binary is an **unofficial, modified** claude-code. Don't report
  bugs against it to Anthropic; revert to the stock binary first.
- Byte-identical rebuild of the official artifact is **not** possible (no
  source). This produces a functionally-patched derivative.
- `npx unbunjs` is used for offset detection; first run may hit the network.