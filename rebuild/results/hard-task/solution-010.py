from functools import lru_cache

def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0

    @lru_cache(maxsize=None)
    def dp(r1, c1, r2, c2):
        if r1 < 0 or r1 >= n or c1 < 0 or c1 >= n: return -10**9
        if r2 < 0 or r2 >= n or c2 < 0 or c2 >= n: return -10**9
        if grid[r1][c1] == -1 or grid[r2][c2] == -1: return -10**9
        if r1 == n-1 and c1 == n-1:
            return grid[n-1][n-1]
        if r1 == r2 and c1 == c2:
            val = grid[r1][c1]
        else:
            val = grid[r1][c1] + grid[r2][c2]
        best = max(
            dp(r1+1, c1, r2+1, c2),
            dp(r1+1, c1, r2, c2+1),
            dp(r1, c1+1, r2+1, c2),
            dp(r1, c1+1, r2, c2+1),
        )
        return val + best

    result = dp(0, 0, 0, 0)
    return max(0, result)
