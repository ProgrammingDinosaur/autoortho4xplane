import struct, re, os, math, time, datetime, atexit
import py7zr
import py7zr.io

from aoconfig import CFG
from aostats import STATS, StatTracker, inc_stat
from functools import lru_cache

import logging
log = logging.getLogger(__name__)

class AoDsfSeason():
    invalid = True      # assume invalid, e.g uninstalled scenery

    def __init__(self, lat, lon, cache_dir):
        #print(f"__init__ {lat} {lon}")
        self.demn = []
        self.demi = []
        self.dmed = []

        lat_10 = math.floor(lat / 10) * 10
        lon_10 = math.floor(lon / 10 ) * 10

        xp12_root = CFG.paths.xplane_path
        self.name_base = f"{lat:+03d}{lon:+04d}"
        self.cached_si_fn = os.path.join(cache_dir, self.name_base + ".si")
        from_cache = False

        if os.path.isfile(self.cached_si_fn):
            #print(f"Reading cached file: {self.cached_si_fn}")
            self.f = open(self.cached_si_fn, "rb")
            from_cache = True

            self.f.seek(0, os.SEEK_END)
            file_len = self.f.tell()
            #print(file_len)
            self.f.seek(0, os.SEEK_SET)
            self._decode_atom(file_len, 0)
        else:
            dsf_name = self.name_base + ".dsf"
            en_dir = f"{lat_10:+03d}{lon_10:+04d}"
            dsf_name_full = os.path.join(xp12_root, "Global Scenery/X-Plane 12 Global Scenery/Earth nav data",
                                         en_dir, dsf_name)
            if not os.path.isfile(dsf_name_full):
                dsf_name_full = os.path.join(xp12_root, "Global Scenery/X-Plane 12 Demo Areas/Earth nav data",
                                             en_dir, dsf_name)
            if not os.path.isfile(dsf_name_full):
                return

            # Use new py7zr factory API to extract DSF into memory (only the target file)
            with py7zr.SevenZipFile(dsf_name_full, mode='r') as archive:
                # Find archive member that matches our DSF (exact or suffix)
                arc_names = archive.getnames()
                target_name = None
                for n in arc_names:
                    if n == dsf_name or n.endswith("/" + dsf_name) or n.endswith("\\" + dsf_name) or n.endswith(dsf_name):
                        target_name = n
                        break

                if not target_name:
                    return

                # Determine a safe in-memory buffer limit based on file metadata
                try:
                    info = archive.getinfo(target_name)
                except Exception:
                    info = None

                uncompressed = None
                if info is not None:
                    # Try multiple attribute names to be compatible across versions
                    for attr in ("uncompressed", "uncompressed_size", "size"):
                        val = getattr(info, attr, None)
                        if isinstance(val, int) and val > 0:
                            uncompressed = val
                            break

                if not uncompressed:
                    uncompressed = 128 * 1024 * 1024  # 128 MiB conservative default

                limit = int(uncompressed) + (1 * 1024 * 1024)  # +1 MiB headroom

                factory = py7zr.io.BytesIOFactory(limit=limit)
                archive.extract(targets=[target_name], factory=factory)

                fobj = factory.products.get(target_name)
                if fobj is None:
                    return

                self.f = fobj

                # Compute file length using size() when tell() is not available
                try:
                    file_len = self.f.size()
                except Exception:
                    self.f.seek(0, os.SEEK_END)
                    file_len = self.f.tell()
                    self.f.seek(0, os.SEEK_SET)
                file_len -= 16  # footer

                buf = self.f.read(12)
                cockie, version = struct.unpack("<8sI", buf)

                self._decode_atom(file_len - 12, 0)

        try:
            self.f.close()
        except Exception:
            pass
        self.f = None   # reduce memory footprint

        i = 0
        for dn in self.demn:
            if dn == "spr1":
                break
            else:
                i += 1

        self.demn = self.demn[i:]    # reduce memory footprint
        self.demi = self.demi[i:]
        self.dmed = self.dmed[i:]
        assert len(self.demn) == 8  # according to DSF spec 8 or none

        if not from_cache:
            self._save_season()
        self.invalid = False

    def _decode_atom(self, length, level):
        verbose = False
        while length > 0:
            buf = self.f.read(8)
            atom, atom_len = struct.unpack("<4sI", buf)
            if verbose:
                print(f"{level * ' '} {atom} {atom_len}")

            length -= 8
            atom_remain = atom_len - 8
            if atom == b"NFED":
                self._decode_atom(atom_remain, level + 1)
            elif atom == b"SMED":
                self._decode_atom(atom_remain, level + 1)
            elif atom == b"NMED":
                buf = self.f.read(atom_remain)
                self.demn = [n.decode("ascii") for n in buf.split(b"\0")[:-1]]
            elif atom == b"IMED":
                buf = self.f.read(atom_remain)
                self.demi.append(struct.unpack("<BBHIIff", buf))
            elif atom == b"DMED":
                if verbose:
                    print(f"{level * ' '}   DEMD length: {atom_remain}")
                buf = self.f.read(atom_remain)
                self.dmed.append(buf)
            else:
                self.f.seek(atom_remain, 1)

            length -= atom_remain

    def _build_atom(self, code, buf):
        return code + struct.pack("<I", 8 + len(buf)) + buf

    def _save_season(self):
        """ save season info to a cache file in (abbreviated) DSF format """
        with open(self.cached_si_fn, "wb") as f:
            DEMN_buf = self._build_atom(b"NMED", bytes("\0".join(self.demn) + "\0", 'ascii'))

            DEMS_buf = b''
            for i in range(0, 8):
                buf = struct.pack("<BBHIIff", *self.demi[i])
                DEMS_buf += self._build_atom(b"IMED", buf)
                DEMS_buf += self._build_atom(b"DMED", self.dmed[i])

            # no header
            f.write(self._build_atom(b"NFED", DEMN_buf + DEMS_buf))


class AoSeasonCache():

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.cfg_saturation = [float(CFG.seasons.spr_saturation), float(CFG.seasons.sum_saturation),
                               float(CFG.seasons.fal_saturation), float(CFG.seasons.win_saturation) ]
        atexit.register(self._show_stats)

    # plane flies in a 4x3 dsf box and dsf covers >= 100km x 50km
    # so 60 is plenty
    @lru_cache(maxsize = 60)
    def _get_dsf(self, lat, lon):
        return AoDsfSeason(lat, lon, self.cache_dir)

    def _season_info_ll(self, lat, lon, day = None):
        """ return a weight vector for day in  [spr, sum, fal, win] """
        lat_i = math.floor(lat)
        lon_i = math.floor(lon)
        lat_frac = lat - lat_i
        lon_frac = lon - lon_i
        if lat_frac < 0.0:
            lat_frac = 1.0 + lat_frac

        if lon_frac < 0.0:
            lon_frac = 1.0 + lon_frac


        dsf = self._get_dsf(lat_i, lon_i)

        if dsf.invalid:
            return [0.0, 1.0, 0.0, 0.0] # eternal summer

        season_days = []
        for si in range(0, 8):
            dmed = dsf.dmed[si]
            demi = dsf.demi[si]
            ncol = demi[3]
            nrow = demi[4]
            scale = demi[5]
            row = math.floor(lat_frac * nrow)
            col = math.floor(lon_frac * ncol)
            d = math.floor(dmed[row * ncol + col] * scale)
            #print(f"{dsf.demn[si]}, {d}")
            season_days.append(d)

        if season_days[0] == 0 and season_days[1] == 0:   # seems to be the equitoral region
            return [0.0, 1.0, 0.0, 0.0]

        season_days.append(season_days[0])
        #print(season_days)

        if day is None:
            day = datetime.date.today().timetuple().tm_yday
        #print(f"day: {day}")

        weights= [0.0] * 4

        # full season
        for i in range(0, 4):
            s = season_days[2 * i]
            e = season_days[2 * i + 1]
            if ((s <= day and day < e) or               # nowrap
                (e < s and (s <= day or day < e))):     # wrap
                weights[i] = 1.0
                return weights

        # interpolate between seasons
        i = 0
        while i < 4:
            s = season_days[2 * i + 1]
            e = season_days[2 * i + 2]
            if s <= day and day < e:                    # nowrap
                d = e - s
                weights[i] = 1.0 - (day - s) / d
                break

            if (e < s):                                     # wrap
                d = (365 - s) + e
                if s <= day:
                    weights[i] = 1.0 - (day - s) / d
                    break
                if day < e:
                    weights[i] = 1.0 - (day + (365 - s)) / d
                    break

            i = i + 1

        if i < 4:
            if i == 3:
                weights[0] = 1.0 - weights[3]
            else:
                weights[i + 1] = 1.0 - weights[i]
            return weights

        log.warning(f"Oh no, could not match {day} to {season_days}")
        return [0.0, 1.0, 0.0, 0.0]

    def _season_info_rc(self, row, col, zoom, day = None):
        """ return a weight vector for day in  [spr, sum, fal, win] """

        # NW corner
        lon = col / math.pow(2, zoom) * 360 - 180
        n = math.pi - 2 * math.pi * row / math.pow(2, zoom)
        lat = 180 / math.pi * math.atan(0.5 * (math.exp(n) - math.exp(-n)))
        #print(lat, lon)
        return self._season_info_ll(lat, lon, day)

    def saturation(self, row, col, zoom, day = None):
        weights = self._season_info_rc(row, col, zoom, day)
        saturation = 0.0
        for i in range(0, 4):
            saturation += weights[i] * self.cfg_saturation[i]

        return saturation

    def _show_stats(self):
        log.info(f"AoSeasonCache stats: {self._get_dsf.cache_info()}")

if __name__ == "__main__":
    from aoconfig import AOConfig
    aoc = AOConfig()

    ao_season = AoSeasonCache(CFG.paths.cache_dir)


    while True:
        if True:
            line = input("lat lon day> ")
            if line == "":
                break
            lat, lon, day = line.split()
            lat = float(lat)
            lon = float(lon)
            day = int(day)
            if day < 0:
                day = None
            weights = ao_season._season_info_ll(lat, lon, day)
            print(weights)
        else:
            line = input("row col zoom day> ")
            row, col, zoom, day = line.split()
            row = int(row)
            col = int(col)
            day = int(day)
            zoom = int(zoom)
            if day < 0:
                day = None
            weights = ao_season._season_info_rc(row, col, zoom, day)
            print(weights)
            saturation = ao_season.saturation(row, col, zoom, day)
            print(saturation)