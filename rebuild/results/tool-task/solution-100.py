def solve(heights: list[int]) -> int:
    if not heights:
        return 0
    l, r = 0, len(heights) - 1
    lmax, rmax = 0, 0
    ans = 0
    while l <= r:
        if heights[l] <= heights[r]:
            if heights[l] >= lmax:
                lmax = heights[l]
            else:
                ans += lmax - heights[l]
            l += 1
        else:
            if heights[r] >= rmax:
                rmax = heights[r]
            else:
                ans += rmax - heights[r]
            r -= 1
    return ans
