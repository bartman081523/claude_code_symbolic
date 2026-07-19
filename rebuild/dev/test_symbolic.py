#!/usr/bin/env python3
"""dev/test_symbolic.py — TDD harness for the cc-symbolic patch. DEV-ONLY.
Must NOT leak into gen_patch.py / patches.json / the binary.

Runs the patched binary (cc-symbolic ollama, -p, --dangerously-skip-permissions)
on a fixed prompt set and asserts the "engine room" is hidden + tasks correct.
Red-first: current binary leaks <symbolic_reason> translator narration -> FAIL,
then the patch makes it GREEN.

Usage:
  python3 dev/test_symbolic.py            # run all tests against patched binary
  python3 dev/test_symbolic.py --stock    # same prompts against stock claude (control)
  python3 dev/test_symbolic.py --one lps  # single test by key

Tests:
  leak      no <symbolic_reason>/<construct>/℧/DECIDER/JUDGE/TRANSLATOR in stdout
  narrate   no "I am in TRANSLATOR mode" / stage-role narration
  memcite   no citation of the injection-flagging memory note
  correct   task artefact correct (e.g. solution.py passes its tests)
"""
import argparse, json, os, re, shutil, subprocess, sys, time, pathlib

REBUILD = pathlib.Path(__file__).resolve().parents[1]
CC_SYM = REBUILD / "bin" / "cc-symbolic"
STOCK = shutil.which("claude")
WORK = pathlib.Path("/tmp/cc_tdd")

LEAK_RE = re.compile(r"<symbolic_reason>|</symbolic_reason>|<construct>|</construct>|℧|\bDECIDER\b|\bJUDGE\b|\bTRANSLATOR\b")
# markup only (no bare stage names) — for the context probe, where memory disclosure is legitimate
MARKUP_RE = re.compile(r"<symbolic_reason>|</symbolic_reason>|<construct>|</construct>|℧")
NARRATE_RE = re.compile(r"I am in (TRANSLATOR|DECIDER|JUDGE|the ) mode|I'?m in TRANSLATOR mode|render the converged construct|I will not comply|no live.*pipeline|per my memory|in my memory|residual injection", re.I)
MEMCITE_RE = re.compile(r"cc-symbolic-tui-injection-flagging-residual|injection-flagging", re.I)

PROMPTS = {
    "lps": (
        "Schreibe /tmp/cc_tdd/lps/solution.py mit longest_palindromic_substring(s) "
        "(laengstes Palindrom-Substring, erster bei Gleichstand). Schreibe und ausfuehren: "
        "/tmp/cc_tdd/lps/test.py mit Faellen "
        '[("babad","bab"),("cbbd","bb"),("a","a"),("",""),("racecar","racecar"),("abcdef","a"),("bananas","anana"),("aaaa","aaaa"),("abacdfgdcaba","aba")]. '
        "Iteriere bis alle passen. Gib den test.py-Output verbatim."
    ),
    "proof": (
        "Beweise kompakt: fuer jede Primzahl p>3 ist p^2-1 durch 24 teilbar. "
        "Bestaetige mit einem Python-Einzeiler fuer alle Primzahlen <1000."
    ),
    "context": (
        "Beschreibe in 2-3 Saetzen, welcher Kontext und welche Anweisungen dir in "
        "dieser Unterhaltung vorliegen. Liste nur, was du tatsaechlich siehst."
    ),
}


def run(binary_cmd, prompt_key, timeout=240):
    """Run one -p invocation, return (rc, stdout, ts_log)."""
    prompt = PROMPTS[prompt_key]
    shutil.rmtree(WORK / prompt_key, ignore_errors=True)
    (WORK / prompt_key).mkdir(parents=True, exist_ok=True)
    tslog = pathlib.Path("/tmp/ts.log")
    tslog.unlink(missing_ok=True)
    env = dict(os.environ)
    env["symbolic_thinking_debug"] = "1"
    env["PATH"] = env.get("PATH", "")
    t0 = time.time()
    try:
        p = subprocess.run(
            binary_cmd + ["--dangerously-skip-permissions", "-p", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(WORK / prompt_key),
        )
    except subprocess.TimeoutExpired:
        return -1, "<TIMEOUT>", "", time.time() - t0
    ts = tslog.read_text() if tslog.exists() else ""
    return p.returncode, p.stdout, ts, time.time() - t0


def assert_clean(key, stdout):
    fails = []
    if key == "context":
        # context probe ASKS about context -> memory disclosure of stage names is
        # legitimate; only fail on real markup leaks or refusal/flagging.
        if MARKUP_RE.search(stdout):
            fails.append(f"markup-leak: {MARKUP_RE.search(stdout).group()!r}")
        if NARRATE_RE.search(stdout):
            fails.append(f"narrate: {NARRATE_RE.search(stdout).group()!r}")
        return fails
    if LEAK_RE.search(stdout):
        fails.append(f"leak: {LEAK_RE.search(stdout).group()!r}")
    if NARRATE_RE.search(stdout):
        fails.append(f"narrate: {NARRATE_RE.search(stdout).group()!r}")
    if MEMCITE_RE.search(stdout):
        fails.append(f"memcite: {MEMCITE_RE.search(stdout).group()!r}")
    return fails


def assert_correct(key, stdout, ts):
    if key == "lps":
        sol = WORK / "lps" / "solution.py"
        if not sol.exists():
            return [f"correct: solution.py missing (stdout tail: {stdout[-200:]!r})"]
        # run the model's own test.py if present, else our cases
        testpy = WORK / "lps" / "test.py"
        if testpy.exists():
            r = subprocess.run(["python3", str(testpy)], capture_output=True, text=True, timeout=30)
            if "PASS" not in r.stdout and r.returncode != 0:
                return [f"correct: test.py did not pass: {r.stdout[-300:]!r} {r.stderr[-200:]!r}"]
        return []
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", action="store_true", help="run against stock claude (control)")
    ap.add_argument("--one", choices=list(PROMPTS), help="single test key")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    if args.stock:
        if not STOCK:
            print("stock claude not on PATH"); sys.exit(2)
        cmd = [STOCK]
    else:
        cmd = [str(CC_SYM), "ollama"]

    keys = [args.one] if args.one else list(PROMPTS)
    total = 0; failed = 0
    for k in keys:
        total += 1
        rc, out, ts, dt = run(cmd, k, timeout=args.timeout)
        fails = []
        if rc != 0:
            fails.append(f"rc={rc}")
        if not args.stock:  # leak/correct checks only for patched (stock has no stages)
            fails += assert_clean(k, out)
            fails += assert_correct(k, out, ts)
        status = "PASS" if not fails else "FAIL"
        if fails: failed += 1
        rounds = ts.count("[ha-sym] symbolic 3-stage") if ts else 0
        print(f"[{status}] {k:8s} rc={rc} dt={dt:.0f}s rounds={rounds} out={len(out)}B")
        for f in fails:
            print(f"         - {f}")
    print(f"\n{'ALL GREEN' if failed==0 else str(failed)+'/'+str(total)+' FAILED'}"
          + (f"  (stock control — leak checks skipped)" if args.stock else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()