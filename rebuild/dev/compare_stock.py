#!/usr/bin/env python3
"""dev/compare_stock.py — compare patched symbolic vs stock on a hard task.
DEV-ONLY (not in patches). Runs both through the usage_relay (127.0.0.1:11500)
to capture per-request token usage, and checks solution correctness.

Usage: python3 dev/compare_stock.py
"""
import os, re, shutil, subprocess, sys, time, pathlib

REBUILD = pathlib.Path(__file__).resolve().parents[1]
CC_SYM = REBUILD / "bin" / "cc-symbolic"
STOCK = shutil.which("claude")
RELAY = "http://127.0.0.1:11500"
WORK = pathlib.Path("/tmp/cc_cmp")
RELAY_LOG = pathlib.Path("/tmp/relay.log")

TASK = (
    "Schreibe /tmp/cc_cmp/solution.py mit cherry_pickup(grid) (LeetCode 741: "
    "max Kirschen auf Hin- und Rueckweg von (0,0) nach (n-1,n-1), nur rechts/runter, "
    "gemeinsame Zelle nur einmal gezaehlt, -1 nicht betretbar). "
    "Schreibe und ausfuehren: /tmp/cc_cmp/test.py mit Faellen "
    '[([[0,1,-1],[1,0,1],[-1,1,0]],4),([[1,1,-1],[1,-1,1],[-1,1,1]],0),([[1,1,1],[1,1,1],[1,1,1]],8),'
    '([[0,0,0],[0,0,0],[0,0,0]],0),([[1,1,1,1],[1,1,1,1],[1,1,1,1],[1,1,1,1]],12),'
    '([[1,1,1,1,0],[1,1,1,1,1],[1,1,1,1,1],[1,1,1,1,1],[1,0,0,1,1]],16),([[1]],1),([[-1]],0),'
    '([[1,1],[1,1]],4),([[1,0,1,0],[0,1,0,1],[1,0,1,0],[0,1,0,1]],6)]. '
    "Iteriere bis alle passen. Gib den test.py-Output verbatim."
)

TOK_RE = re.compile(r"in=(\d+) out=(\d+)")


def parse_relay_tokens():
    if not RELAY_LOG.exists():
        return 0, 0, 0
    inp = out = n = 0
    for line in RELAY_LOG.read_text().splitlines():
        m = TOK_RE.search(line)
        if m:
            inp += int(m.group(1)); out += int(m.group(2)); n += 1
    return inp, out, n


def run_one(label, cmd, force_model=None):
    shutil.rmtree(WORK, ignore_errors=True); WORK.mkdir(parents=True, exist_ok=True)
    RELAY_LOG.write_text("")  # truncate (NOT unlink: relay holds fd open)
    env = dict(os.environ); env["ANTHROPIC_BASE_URL"] = RELAY
    env["symbolic_thinking_debug"] = "1"
    if force_model:  # apples-to-apples: same model on stock and symbolic
        for k in ("ANTHROPIC_DEFAULT_OPUS_MODEL","ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL","ANTHROPIC_MODEL","OPUS_MODEL",
                  "SONNET_MODEL","HAIKU_MODEL"):
            env[k] = force_model
    t0 = time.time()
    try:
        p = subprocess.run(cmd + ["--dangerously-skip-permissions", "-p", TASK],
                           capture_output=True, text=True, timeout=360, env=env, cwd=str(WORK))
    except subprocess.TimeoutExpired:
        return {"label": label, "rc": -1, "dt": time.time()-t0, "in": 0, "out": 0, "reqs": 0, "correct": False, "stdout": "<TIMEOUT>"}
    inp, out, n = parse_relay_tokens()
    # correctness: run the model's test.py
    correct = False
    testpy = WORK / "test.py"; sol = WORK / "solution.py"
    if sol.exists() and testpy.exists():
        r = subprocess.run(["python3", str(testpy)], capture_output=True, text=True, timeout=30)
        correct = ("PASS" in r.stdout) or (r.returncode == 0 and "FAIL" not in r.stdout and "Error" not in r.stdout)
    return {"label": label, "rc": p.returncode, "dt": time.time()-t0, "in": inp, "out": out,
            "reqs": n, "correct": correct, "stdout": p.stdout}


def main():
    if not STOCK:
        print("stock claude not on PATH"); sys.exit(2)
    MODEL = "minimax-m3:cloud"  # same model for both -> apples-to-apples
    print(f"=== symbolic (cc-symbolic, 3-stage, model={MODEL}) ===")
    s = run_one("symbolic", [str(CC_SYM), "ollama"], force_model=MODEL)
    print(f"  rc={s['rc']} dt={s['dt']:.0f}s reqs={s['reqs']} in={s['in']} out={s['out']} correct={s['correct']}")
    print(f"  stdout tail: {s['stdout'][-200:]!r}")
    print(f"=== stock (claude 2.1.215, model={MODEL}) ===")
    k = run_one("stock", [STOCK], force_model=MODEL)
    print(f"  rc={k['rc']} dt={k['dt']:.0f}s reqs={k['reqs']} in={k['in']} out={k['out']} correct={k['correct']}")
    print(f"  stdout tail: {k['stdout'][-200:]!r}")
    print("\n=== COMPARISON ===")
    s_tot = s["in"]+s["out"]; k_tot = k["in"]+k["out"]
    print(f"{'metric':<12} {'symbolic':>12} {'stock':>12} {'sym/stock':>10}")
    print(f"{'input':<12} {s['in']:>12} {k['in']:>12} {s['in']/max(k['in'],1)*100:>9.0f}%")
    print(f"{'output':<12} {s['out']:>12} {k['out']:>12} {s['out']/max(k['out'],1)*100:>9.0f}%")
    print(f"{'total':<12} {s_tot:>12} {k_tot:>12} {s_tot/max(k_tot,1)*100:>9.0f}%")
    print(f"{'requests':<12} {s['reqs']:>12} {k['reqs']:>12}")
    print(f"{'correct':<12} {str(s['correct']):>12} {str(k['correct']):>12}")
    print(f"{'wall_s':<12} {s['dt']:>12.0f} {k['dt']:>12.0f}")
    win = s["correct"] and (not k["correct"] or s_tot < k_tot)
    print(f"\nVERDICT: symbolic {'WINS' if win else 'does not win'} (correct + (stock wrong OR tokens saved))")


if __name__ == "__main__":
    main()