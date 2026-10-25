"""Swept collision, x-buckets and shelter-cell lookups (spec §2.3).

Every test is a swept box test (`segment_hit`), so no shot tunnels through a target at any speed. Lookups never scan
every shelter cell: they visit only the integer cells a box can touch.
"""

from math import ceil, floor

BUCKET = 4  # x-bucket width in cells: player shots test only the enemies of the buckets their sweep touches
CELL = 0.48  # a shelter cell's hit box half-size (cells sit on integer coordinates and never overlap)


def segment_hit(x0, y0, x1, y1, x, y, rx, ry):
    """The first point (0..1) at which the segment (x0, y0) -> (x1, y1) enters the box x +- rx, y +- ry, or None."""
    enter, leave = 0.0, 1.0
    delta = x1 - x0
    if -1e-12 < delta < 1e-12:
        if abs(x0 - x) > rx:
            return None
    else:
        near, far = (x - rx - x0) / delta, (x + rx - x0) / delta
        if near > far:
            near, far = far, near
        enter, leave = max(enter, near), min(leave, far)
        if enter > leave:
            return None
    delta = y1 - y0
    if -1e-12 < delta < 1e-12:
        if abs(y0 - y) > ry:
            return None
    else:
        near, far = (y - ry - y0) / delta, (y + ry - y0) / delta
        if near > far:
            near, far = far, near
        enter, leave = max(enter, near), min(leave, far)
        if enter > leave:
            return None
    return enter


def buckets(aliens) -> dict:
    """Enemies by x-bucket (floor(x / BUCKET)), each bucket in id order."""
    table = {}
    for alien in aliens:
        key = floor(alien.x / BUCKET)
        entry = table.get(key)
        if entry is None:
            table[key] = [alien]
        else:
            entry.append(alien)
    return table


def near(table, x0, x1, reach) -> list:
    """The enemies of every bucket a sweep from x0 to x1, widened by reach, touches; in id order."""
    low, high = floor((min(x0, x1) - reach) / BUCKET), floor((max(x0, x1) + reach) / BUCKET)
    if low == high:
        return table.get(low, ())
    found = []
    for key in range(low, high + 1):
        entry = table.get(key)
        if entry:
            found.extend(entry)
    if high - low > 0 and len(found) > 1:
        found.sort(key=_by_id)
    return found


def _by_id(entity):
    return entity.id


def cells_in(bunkers, x0, x1, y0, y1, rx=CELL, ry=CELL) -> list:
    """The shelter cells whose boxes (cell +- rx, +- ry) a sweep's bounding box touches, by (y, x)."""
    left, right = ceil(min(x0, x1) - rx), floor(max(x0, x1) + rx)
    top, bottom = ceil(min(y0, y1) - ry), floor(max(y0, y1) + ry)
    return [(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1) if (x, y) in bunkers]


def first_cell(bunkers, x0, y0, x1, y1, sx=0.0, sy=0.0):
    """(distance, cell) of the first shelter cell a shot of half-size (sx, sy) sweeping x0,y0 -> x1,y1 enters."""
    best, found = 2.0, None
    rx, ry = CELL + sx, CELL + sy
    for cell in cells_in(bunkers, x0, x1, y0, y1, rx, ry):
        distance = segment_hit(x0, y0, x1, y1, cell[0], cell[1], rx, ry)
        if distance is not None and distance < best:
            best, found = distance, cell
    return best, found


def cells_near(bunkers, x, y, rx, ry) -> list:
    """The shelter cells within rx, ry of a point (the crush rectangle), by (y, x)."""
    return [
        (cx, cy)
        for cy in range(ceil(y - ry), floor(y + ry) + 1)
        for cx in range(ceil(x - rx), floor(x + rx) + 1)
        if (cx, cy) in bunkers
    ]
