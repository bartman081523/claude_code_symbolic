from functools import lru_cache

def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0

    @lru_cache(maxsize=None)
    def dp(r1, c1, r2, c2):
        if grid[r1][c1] == -1 or grid[r2][c2] == -1:
            return float('-inf')
        if r1 == n-1 and c1 == n-1 and r2 == n-1 and c2 == n-1:
            return grid[n-1][n-1]
        if (r1, c1) == (r2, c2):
            curr = grid[r1][c1]
        else:
            curr = grid[r1][c1] + grid[r2][c2]
        best = float('-inf')
        for dr1, dc1, dr2, dc2 in [(1,0,1,0), (1,0,0,1), (0,1,1,0), (0,1,0,1)]:
            nr1, nc1, nr2, nc2 = r1+dr1, c1+dc1, r2+dr2, c2+dc2
            if nr1 >= n or nc1 >= n or nr2 >= n or nc2 >= n:
                continue
            res = dp(nr1, nc1, nr2, nc2)
            if res > best:
                best = res
        if best == float('-inf'):
            return float('-inf')
        return curr + best

    result = dp(0, 0, 0, 0)
    return max(0, result)
