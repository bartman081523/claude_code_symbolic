def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0

    neg_inf = float('-inf')
    # dp[r1][r2] = best value when both walkers are at step k
    # step k means r1+c1 = k and r2+c2 = k
    dp = [[neg_inf] * n for _ in range(n)]
    total_steps = 2 * (n - 1)

    for k in range(total_steps + 1):
        new = [[neg_inf] * n for _ in range(n)]
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
                if k == 0:
                    if r1 == 0 and r2 == 0:
                        new[r1][r2] = grid[0][0]
                    continue
                # Try all four move combinations (each walker comes from up, left, or both — actually from {down, right})
                # Moves: (dr1, dc1) in {(0,0), (0,1), (1,0)} means previous was (r1-dr1, c1-dc1)
                for dr1 in (0, 1):
                    for dc1 in (0, 1):
                        if not (dr1 == 0 or dc1 == 0):
                            continue
                        pr1 = r1 - dr1
                        pc1 = c1 - dc1
                        if pr1 < 0 or pc1 < 0:
                            continue
                        if grid[pr1][pc1] == -1:
                            continue
                        for dr2 in (0, 1):
                            for dc2 in (0, 1):
                                if not (dr2 == 0 or dc2 == 0):
                                    continue
                                pr2 = r2 - dr2
                                pc2 = c2 - dc2
                                if pr2 < 0 or pc2 < 0:
                                    continue
                                if grid[pr2][pc2] == -1:
                                    continue
                                if dp[pr1][pr2] == neg_inf:
                                    continue
                                val = dp[pr1][pr2] + grid[r1][c1]
                                if (r1, c1) != (r2, c2):
                                    val += grid[r2][c2]
                                if val > new[r1][r2]:
                                    new[r1][r2] = val
        dp = new

    ans = neg_inf
    for r1 in range(n):
        c1 = total_steps - r1
        if c1 < 0 or c1 >= n or grid[r1][c1] == -1:
            continue
        for r2 in range(n):
            c2 = total_steps - r2
            if c2 < 0 or c2 >= n or grid[r2][c2] == -1:
                continue
            if dp[r1][r2] > ans:
                ans = dp[r1][r2]
    if ans == neg_inf:
        return 0
    return ans
