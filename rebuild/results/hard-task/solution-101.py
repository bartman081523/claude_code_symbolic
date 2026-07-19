import sys
from functools import lru_cache

def solve(grid: list[list[int]]) -> int:
    n = len(grid)
    if n == 0 or grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0

    NEG = -10**9

    @lru_cache(maxsize=None)
    def dp(r1, c1, r2):
        c2 = r1 + c1 - r2
        if r1 >= n or c1 >= n or r2 >= n or c2 >= n:
            return NEG
        if grid[r1][c1] == -1 or grid[r2][c2] == -1:
            return NEG
        if r1 == n-1 and c1 == n-1:
            return grid[r1][c1]
        cherries = grid[r1][c1]
        if not (r1 == r2 and c1 == c2):
            cherries += grid[r2][c2]
        best = max(
            dp(r1+1, c1, r2+1),
            dp(r1+1, c1, r2),
            dp(r1, c1+1, r2+1),
            dp(r1, c1+1, r2),
        )
        if best == NEG:
            return NEG
        return cherries + best

    result = dp(0, 0, 0)
    return max(0, result)
