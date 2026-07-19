import sys
sys.path.insert(0, ".")
from solution import solve

cases = [
    ([], 0),
    ([5], 0),
    ([3, 3, 3], 0),
    ([2, 0, 2], 2),
    ([3, 0, 0, 2, 0, 4], 10),
    ([5, 2, 1, 2, 1, 5], 14),
    ([4, 2, 0, 3, 2, 5], 9),
    ([0, 1, 0, 2, 1, 0, 1, 3, 2, 1, 2, 1], 6),
    ([5, 4, 3, 2, 1], 0),
    ([1, 2, 3, 4, 5], 0),
    ([0, 0, 0, 0], 0),
    ([6, 4, 2, 0, 3, 2, 5], 14),
]

failed = 0
for i, (h, exp) in enumerate(cases):
    got = solve(list(h))
    if got != exp:
        print(f"FAIL case {i}: heights={h} expected {exp} got {got}")
        failed += 1

if failed:
    print(f"{failed} case(s) FAILED")
    sys.exit(1)
print("ALL TESTS PASSED")
