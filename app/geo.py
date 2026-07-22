"""Geohash и пересчёт центра/зума карты в параметр bounds."""

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int = 6) -> str:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    result = []
    bits = 0
    ch = 0
    even = True
    while len(result) < precision:
        rng, value = (lon_range, lon) if even else (lat_range, lat)
        mid = (rng[0] + rng[1]) / 2
        if value > mid:
            ch = (ch << 1) + 1
            rng[0] = mid
        else:
            ch = ch << 1
            rng[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            result.append(_BASE32[ch])
            bits = 0
            ch = 0
    return "".join(result)


def viewport_bounds(lat: float, lon: float, zoom: int,
                    width_px: int, height_px: int, pad: float = 0.10
                    ) -> tuple[float, float, float, float]:
    """Углы вьюпорта web-mercator: (nw_lat, nw_lon, se_lat, se_lon)."""
    world_px = 256 * (2 ** zoom)
    half_w = width_px * (1 + pad) / 2
    half_h = height_px * (1 + pad) / 2

    deg_per_px = 360.0 / world_px
    nw_lon = lon - half_w * deg_per_px
    se_lon = lon + half_w * deg_per_px

    merc_per_px = 2 * math.pi / world_px
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    nw_lat = math.degrees(2 * math.atan(math.exp(y + half_h * merc_per_px)) - math.pi / 2)
    se_lat = math.degrees(2 * math.atan(math.exp(y - half_h * merc_per_px)) - math.pi / 2)
    return nw_lat, nw_lon, se_lat, se_lon


def bounds_param(lat: float, lon: float, zoom: int,
                 width_px: int, height_px: int, precision: int = 6) -> str:
    """Значение &bounds= — geohash NW-угла, затем SE-угла."""
    nw_lat, nw_lon, se_lat, se_lon = viewport_bounds(lat, lon, zoom, width_px, height_px)
    return f"{geohash_encode(nw_lat, nw_lon, precision)},{geohash_encode(se_lat, se_lon, precision)}"
