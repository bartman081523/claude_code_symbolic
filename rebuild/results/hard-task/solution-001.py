def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n - 1][n - 1] == -1:
        return 0

    NEG = float('-inf')
    # dp[r1][c1][r2] with implicit c2 = k - r2 where k = r1 + c1
    dp = [[[NEG] * n for _ in range(n)] for _ in range(n)]
    dp[0][0][0] = grid[0][0]

    for k in range(1, 2 * n - 1):
        new_dp = [[[NEG] * n for _ in range(n)] for _ in range(n)]
        for r1 in range(n):
            c1 = k - r1
            if c1 < 0 or c1 >= n:
                continue
            if grid[r1][c1] == -1:
                continue
            for r2 in range(n):
                c2 = k - r2
                if c2 < 0 or c2 >= n:
                    continue
                if grid[r2][c2] == -1:
                    continue
                best = NEG
                for pr1 in (r1 - 1, r1):
                    if pr1 < 0:
                        continue
                    for pc1 in (c1 - 1, c1):
                        if pc1 < 0:
                            continue
                        if pr1 + pc1 != k - 1:
                            continue
                        for pr2 in (r2 - 1, r2):
                            if pr2 < 0:
                                continue
                            for pc2 in (c2 - 1, c2):
                                if pc2 < 0:
                                    continue
                                if pr2 + pc2 != k - 1:
                                    continue
                                v = dp[pr1][pc1][pr2]
                                if v > best:
                                    best = v
                if best == NEG:
                    continue
                cherries = grid[r1][c1] + grid[r2][c2]
                if r1 == r2 and c1 == c2:
                    cherries = grid[r1][c1]
                new_dp[r1][c1][r2] = best + cherries
        dp = new_dp

    ans = dp[n - 1][n - 1][n - 1]
    return max(0, ans)
