def solve(heights: list[int]) -> int:
    l, r = 0, len(heights) - 1
    lmax = rmax = 0
    total = 0
    while l <= r:
        if heights[l] <= heights[r]:
            if heights[l] >= lmax:
                lmax = heights[l]
            else:
                total += lmax - heights[l]
            l += 1
        else:
            if heights[r] >= rmax:
                rmax = heights[r]
            else:
                total += rmax - heights[r]
            r -= 1
    return total
