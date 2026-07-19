def solve(heights):
    """Trapping Rain Water — two-pointer O(n) / O(1) extra space."""
    left, right = 0, len(heights) - 1
    left_max = right_max = 0
    trapped = 0
    while left < right:
        if heights[left] <= heights[right]:
            if heights[left] >= left_max:
                left_max = heights[left]
            else:
                trapped += left_max - heights[left]
            left += 1
        else:
            if heights[right] >= right_max:
                right_max = heights[right]
            else:
                trapped += right_max - heights[right]
            right -= 1
    return trapped
