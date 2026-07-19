def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0

    NEG = -10**9
    # dp[r1][r2] = max cherries when path1 is at (r1, c1) and path2 is at (r2, c2)
    # at step t = r1 + c1 = r2 + c2
    dp = [[NEG] * n for _ in range(n)]
    dp[0][0] = grid[0][0]

    for t in range(1, 2 * n - 1):
        new_dp = [[NEG] * n for _ in range(n)]
        for r1 in range(n):
            c1 = t - r1
            if c1 < 0 or c1 >= n or grid[r1][c1] == -1:
                continue
            for r2 in range(n):
                c2 = t - r2
                if c2 < 0 or c2 >= n or grid[r2][c2] == -1:
                    continue
                best = NEG
                # Predecessors: came from (r, c-1) [right move] or (r-1, c) [down move]
                for pr1, pc1 in ((r1, c1 - 1), (r1 - 1, c1)):
                    if pr1 < 0 or pr1 >= n or pc1 < 0 or pc1 >= n:
                        continue
                    for pr2, pc2 in ((r2, c2 - 1), (r2 - 1, c2)):
                        if pr2 < 0 or pr2 >= n or pc2 < 0 or pc2 >= n:
                            continue
                        if dp[pr1][pr2] > best:
                            best = dp[pr1][pr2]
                if best == NEG:
                    continue
                if r1 == r2 and c1 == c2:
                    new_dp[r1][r2] = max(new_dp[r1][r2], best + grid[r1][c1])
                else:
                    new_dp[r1][r2] = max(new_dp[r1][r2], best + grid[r1][c1] + grid[r2][c2])
        dp = new_dp

    return max(0, dp[n-1][n-1])
