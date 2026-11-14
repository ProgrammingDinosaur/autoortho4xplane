"""X-Plane's window of loaded DSF squares (A8).

X-Plane keeps 4 columns x 3 rows of DSF squares around the aircraft. It
moves a column when the aircraft is 0.15 degrees past a column's middle and
a row 0.1 degrees past a row's edge (constants measured by OrthoStudio XP
Live, reimplemented here). Files mode keeps texture names for this window
plus one ring, so X-Plane never opens a texture that does not exist.
"""

import math

XPLANE_COLUMNS = 4
XPLANE_ROWS = 3
COLUMN_MOVE_DEG = 0.15
ROW_MOVE_DEG = 0.1
JUMP_RESET_DEG = 2.0
RING = 1
KEEP = 2


def _wrap_lon(lon):
    return ((int(lon) + 180) % 360) - 180


class XPlaneWindow:
    """Follows the aircraft the way X-Plane moves its DSF window."""

    def __init__(self):
        self.west = None   # western-most column's longitude
        self.south = None  # southern-most row's latitude
        self.last = None

    def reset(self, lat, lon):
        # Four columns: the aircraft's column is the second or third,
        # whichever side of its column's middle it is on.
        column = math.floor(lon)
        self.west = column - 2 if (lon - column) < 0.5 else column - 1
        self.south = math.floor(lat) - 1
        self.last = (lat, lon)

    def follow(self, lat, lon):
        """Update for a new position; True when the window moved."""
        if self.west is None or self.last is None:
            self.reset(lat, lon)
            return True
        dlat = abs(lat - self.last[0])
        dlon = abs(((lon - self.last[1]) + 540.0) % 360.0 - 180.0)
        if dlat > JUMP_RESET_DEG or dlon > JUMP_RESET_DEG:
            self.reset(lat, lon)
            return True
        self.last = (lat, lon)
        moved = False
        lon_rel = lon
        if lon_rel - self.west > 300:
            lon_rel -= 360
        elif self.west - lon_rel > 300:
            lon_rel += 360
        # Columns: the window is centred on the boundary between its second
        # and third columns; it moves once the aircraft is past the middle of
        # the column beyond that boundary by COLUMN_MOVE_DEG.
        centre = self.west + 2
        if lon_rel > centre + 0.5 + COLUMN_MOVE_DEG:
            self.west += 1
            moved = True
        elif lon_rel < centre - 0.5 - COLUMN_MOVE_DEG:
            self.west -= 1
            moved = True
        # Rows: centred on the middle row; moves past its edge by ROW_MOVE_DEG.
        middle = self.south + 1
        if lat > middle + 1 + ROW_MOVE_DEG:
            self.south += 1
            moved = True
        elif lat < middle - ROW_MOVE_DEG:
            self.south -= 1
            moved = True
        return moved

    def tiles(self):
        """The squares X-Plane holds now: ``{(lat_sw, lon_sw)}``."""
        if self.west is None:
            return set()
        return {
            (self.south + r, _wrap_lon(self.west + c))
            for r in range(XPLANE_ROWS)
            for c in range(XPLANE_COLUMNS)
        }

    def with_ring(self, ring):
        """The window grown by ``ring`` squares on every side."""
        if self.west is None:
            return set()
        return {
            (self.south + r, _wrap_lon(self.west + c))
            for r in range(-ring, XPLANE_ROWS + ring)
            for c in range(-ring, XPLANE_COLUMNS + ring)
            if -90 <= self.south + r < 90
        }


def xplane_tiles(lat, lon):
    """Squares X-Plane may hold for an aircraft at (lat, lon), with its lag.

    Both the window centred either side of the aircraft's column count,
    since X-Plane may not have moved yet.
    """
    tiles = set()
    for dlat in (-ROW_MOVE_DEG, 0.0, ROW_MOVE_DEG):
        for dlon in (-COLUMN_MOVE_DEG - 0.5, 0.0, COLUMN_MOVE_DEG + 0.5):
            window = XPlaneWindow()
            window.reset(lat + dlat, lon + dlon)
            tiles |= window.tiles()
    return tiles


def squares_around(points, radius_squares=1):
    """Squares within ``radius_squares`` of any (lat, lon) in ``points``."""
    out = set()
    for lat, lon in points:
        base_lat = math.floor(lat)
        base_lon = math.floor(lon)
        for dlat in range(-radius_squares, radius_squares + 1):
            for dlon in range(-radius_squares, radius_squares + 1):
                sq_lat = base_lat + dlat
                if -90 <= sq_lat < 90:
                    out.add((sq_lat, _wrap_lon(base_lon + dlon)))
    return out
