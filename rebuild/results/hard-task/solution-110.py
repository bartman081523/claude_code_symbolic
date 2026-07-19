from functools import lru_cache

def solve(grid):
    n = len(grid)
    if n == 0:
        return 0
    if grid[0][0] == -1 or grid[n-1][n-1] == -1:
        return 0

    @lru_cache(maxsize=None)
    def f(r1, c1, r2, c2):
        # Both at end?
        if r1 == n-1 and c1 == n-1 and r2 == n-1 and c2 == n-1:
            return 1 if grid[n-1][n-1] == 1 else 0

        # bounds + obstacles
        if r1 < 0 or r1 >= n or c1 < 0 or c1 >= n or r2 < 0 or r2 >= n or c2 < 0 or c2 >= n:
            return -1
        if grid[r1][c1] == -1 or grid[r2][c2] == -1:
            return -1

        # reward for current cells (start cell handled in next step since both start at 0,0)
        if r1 == r2 and c1 == c2:
            val = 1 if grid[r1][c1] == 1 else 0
        else:
            val = (1 if grid[r1][c1] == 1 else 0) + (1 if grid[r2][c2] == 1 else 0)

        best = -1
        for dr1, dc1 in ((0,1),(1,0)):
            nr1, nc1 = r1+dr1, c1+dc1
            for dr2, dc2 in ((0,1),(1,0)):
                nr2, nc2 = r2+dr2, c2+dc2
                if nr1 >= n or nc1 >= n or nr2 >= n or nc2 >= n:
                    continue
                if (nr1+nc1) != (nr2+nc2):
                    continue
                res = f(nr1, nc1, nr2, nc2)
                if res >= 0 and res + val > best:
                    best = res + val

        return best

    res = f(0, 0, 0, 0)
    return max(0, res)
