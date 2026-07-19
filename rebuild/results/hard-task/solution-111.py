import sys
from functools import lru_cache

def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0
    INF = -10**9

    @lru_cache(maxsize=None)
    def dp(r1, c1, r2):
        c2 = r1 + c1 - r2
        if r1 >= n or c1 >= n or r2 >= n or c2 >= n:
            return INF
        if grid[r1][c1] == -1 or grid[r2][c2] == -1:
            return INF
        if r1 == n-1 and c1 == n-1:
            return grid[n-1][n-1]
        ans = max(
            dp(r1+1, c1, r2+1),
            dp(r1+1, c1, r2),
            dp(r1, c1+1, r2+1),
            dp(r1, c1+1, r2),
        )
        if ans == INF:
            return INF
        here = grid[r1][c1]
        if r1 == r2 and c1 == c2:
            ans += here
        else:
            ans += here + grid[r2][c2]
        return ans

    res = dp(0, 0, 0)
    return max(0, res)
