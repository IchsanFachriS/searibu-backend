"""Extract the Kepulauan Seribu region from TPXO10-atlas-v2 per-constituent NetCDF
files into a SQLite database.

TPXO10-atlas-v2 uses a per-constituent file layout (one .nc per tidal constituent),
which differs from the single-file layout of TPXO9-atlas-v5. This script handles
that structure automatically.

Usage:
    python scripts/preprocess_tpxo10.py \
        --tpxo-dir  data/tpxo10_atlas_v2 \
        --output-db data/tpxo_seribu.db

Arguments:
    --tpxo-dir   Folder containing all TPXO10-atlas-v2 .nc files, including
                 grid_tpxo10atlas_v2.nc and h_XX_tpxo10_atlas_30_v2.nc files.
    --output-db  Destination SQLite database (will be overwritten if it exists).

Prerequisites:
    pip install netCDF4 numpy
"""

import argparse
import logging
import sqlite3
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Bounding box Kepulauan Seribu (sedikit diperlebar untuk coverage penuh) ──
LAT_MIN = -6.0
LAT_MAX = -5.3
LON_MIN = 106.3
LON_MAX = 107.0

# ── 15 konstituen yang digunakan Searibu (sama dengan TPXO9) ────────────────
# Urutan ini dipakai sebagai constituent_id (0..14) di database
CONSTITUENTS = [
    ("2n2",  "Lunar elliptical semidiurnal second-order"),
    ("k1",   "Lunisolar diurnal"),
    ("k2",   "Lunisolar semidiurnal"),
    ("m2",   "Principal lunar semidiurnal"),
    ("m4",   "Shallow water overtide of M2"),
    ("mf",   "Lunisolar fortnightly"),
    ("mm",   "Lunar monthly"),
    ("mn4",  "Shallow water quarter diurnal"),
    ("ms4",  "Shallow water quarter diurnal"),
    ("n2",   "Larger lunar elliptic semidiurnal"),
    ("o1",   "Principal lunar diurnal"),
    ("p1",   "Principal solar diurnal"),
    ("q1",   "Larger lunar elliptic diurnal"),
    ("s1",   "Solar diurnal"),
    ("s2",   "Principal solar semidiurnal"),
]

# Angular speeds dari Schureman (1958) — deg/hour — dipakai untuk frequency
_s = 0.5490165
_h = 0.0410686
_p = 0.0046418
_T = 15.0

SPEED_DEG_HR: dict = {
    "2n2": 2*_T - 4*_s + 2*_h + 2*_p,
    "k1":  _T + _h,
    "k2":  2*_T + 2*_h,
    "m2":  2*_T - 2*_s + 2*_h,
    "m4":  4*_T - 4*_s + 4*_h,
    "mf":  2*_s,
    "mm":  _s - _p,
    "mn4": 4*_T - 5*_s + 4*_h + _p,
    "ms4": 4*_T - 2*_s + 4*_h,
    "n2":  2*_T - 3*_s + 2*_h + _p,
    "o1":  _T - 2*_s + _h,
    "p1":  _T - _h,
    "q1":  _T - 3*_s + _h + _p,
    "s1":  _T,
    "s2":  2*_T,
}


class TPXO10Preprocessor:
    """Extract TPXO10-atlas-v2 regional subset into SQLite for Searibu."""

    def __init__(self, tpxo_dir: Path, output_db: Path):
        self.tpxo_dir  = tpxo_dir
        self.output_db = output_db
        self.conn      = None

        # Grid arrays — populated in _load_grid()
        self._lon: np.ndarray | None = None   # 1-D lon array (degrees, -180..180)
        self._lat: np.ndarray | None = None   # 1-D lat array (degrees)
        self._mask: np.ndarray | None = None  # 2-D ocean mask (lat × lon), 0 = land

        # Region index slices — populated in _find_region_indices()
        self._lon_start = self._lon_end = 0
        self._lat_start = self._lat_end = 0

    # ── Public entry point ────────────────────────────────────────────────────

    def process(self) -> None:
        try:
            self._validate_inputs()
            self._load_grid()
            self._find_region_indices()
            self._create_database()
            self._insert_constituents_metadata()
            self._extract_and_save()
            self._optimize_database()
            size_mb = self.output_db.stat().st_size / (1024 * 1024)
            logger.info("Done — database: %s (%.2f MB)", self.output_db, size_mb)
        finally:
            if self.conn:
                self.conn.close()

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_inputs(self) -> None:
        if not self.tpxo_dir.exists():
            raise FileNotFoundError(f"TPXO directory not found: {self.tpxo_dir}")

        grid_file = self.tpxo_dir / "grid_tpxo10atlas_v2.nc"
        if not grid_file.exists():
            raise FileNotFoundError(
                f"Grid file not found: {grid_file}\n"
                f"Make sure grid_tpxo10atlas_v2.nc is in {self.tpxo_dir}"
            )

        missing = []
        for name, _ in CONSTITUENTS:
            h_file = self.tpxo_dir / f"h_{name}_tpxo10_atlas_30_v2.nc"
            if not h_file.exists():
                missing.append(str(h_file.name))
        if missing:
            raise FileNotFoundError(
                f"Missing constituent files:\n" + "\n".join(f"  {f}" for f in missing)
            )

        logger.info("Input validation OK — all 15 constituent files found in %s", self.tpxo_dir)

    # ── Grid loading ──────────────────────────────────────────────────────────

    def _load_grid(self) -> None:
        """Load lon/lat/mask from the grid file.

        TPXO10-atlas-v2 grid variables vary slightly between releases:
          - Longitude: 'lon_z' or 'lon'
          - Latitude:  'lat_z' or 'lat'
          - Mask:      'mz' (integer) or 'hz' (bathymetry, nonzero=ocean)
        This method tries all known variants automatically.
        """
        grid_path = self.tpxo_dir / "grid_tpxo10atlas_v2.nc"
        logger.info("Loading grid: %s", grid_path)

        with Dataset(str(grid_path), "r") as ds:
            avail = list(ds.variables.keys())
            logger.info("Grid variables found: %s", avail)

            # Longitude
            for lon_name in ("lon_z", "lon"):
                if lon_name in ds.variables:
                    lon_raw = np.array(ds.variables[lon_name][:], dtype=float)
                    logger.info("Longitude: '%s' shape=%s", lon_name, lon_raw.shape)
                    break
            else:
                raise KeyError(f"No longitude variable found. Available: {avail}")

            # Latitude
            for lat_name in ("lat_z", "lat"):
                if lat_name in ds.variables:
                    lat_raw = np.array(ds.variables[lat_name][:], dtype=float)
                    logger.info("Latitude:  '%s' shape=%s", lat_name, lat_raw.shape)
                    break
            else:
                raise KeyError(f"No latitude variable found. Available: {avail}")

            # Ocean mask — mz (0=land) or hz (bathymetry depth, 0=land)
            for mask_name in ("mz", "hz"):
                if mask_name in ds.variables:
                    mz_raw = np.array(ds.variables[mask_name][:], dtype=float)
                    logger.info("Mask/bath: '%s' shape=%s (nonzero=ocean)", mask_name, mz_raw.shape)
                    break
            else:
                logger.warning("No mask variable found — treating all points as ocean")
                mz_raw = np.ones((len(lat_raw), len(lon_raw)), dtype=float)

        # Normalise longitudes to -180..180
        lon_raw = np.where(lon_raw > 180, lon_raw - 360, lon_raw)

        self._lon  = lon_raw
        self._lat  = lat_raw
        self._mask = mz_raw  # 0 = land, nonzero = ocean

        logger.info(
            "Grid: %d lon × %d lat, lon range [%.3f, %.3f], lat range [%.3f, %.3f]",
            len(self._lon), len(self._lat),
            self._lon.min(), self._lon.max(),
            self._lat.min(), self._lat.max(),
        )

    # ── Region slicing ────────────────────────────────────────────────────────

    def _find_region_indices(self) -> None:
        """Determine the lat/lon index slices for the Seribu bounding box."""
        lon_idx = np.where((self._lon >= LON_MIN) & (self._lon <= LON_MAX))[0]
        lat_idx = np.where((self._lat >= LAT_MIN) & (self._lat <= LAT_MAX))[0]

        if len(lon_idx) == 0 or len(lat_idx) == 0:
            raise ValueError(
                f"No grid points found in bounding box "
                f"lon[{LON_MIN},{LON_MAX}] lat[{LAT_MIN},{LAT_MAX}]. "
                f"Check that the TPXO10 files cover the Indonesian region."
            )

        self._lon_start = int(lon_idx[0])
        self._lon_end   = int(lon_idx[-1]) + 1
        self._lat_start = int(lat_idx[0])
        self._lat_end   = int(lat_idx[-1]) + 1

        ny = self._lat_end - self._lat_start
        nx = self._lon_end - self._lon_start
        logger.info(
            "Seribu region: lon[%d:%d] lat[%d:%d] → %d×%d grid cells",
            self._lon_start, self._lon_end,
            self._lat_start, self._lat_end,
            ny, nx,
        )

    # ── Database creation ─────────────────────────────────────────────────────

    def _create_database(self) -> None:
        if self.output_db.exists():
            self.output_db.unlink()
        self.output_db.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.output_db))
        self.conn.executescript("""
            CREATE TABLE grid_points (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                lon       REAL    NOT NULL,
                lat       REAL    NOT NULL,
                lon_index INTEGER NOT NULL,
                lat_index INTEGER NOT NULL
            );

            CREATE TABLE constituents_metadata (
                id          INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL UNIQUE,
                frequency   REAL    NOT NULL,
                description TEXT
            );

            CREATE TABLE harmonic_constants (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                grid_point_id  INTEGER NOT NULL REFERENCES grid_points(id),
                constituent_id INTEGER NOT NULL REFERENCES constituents_metadata(id),
                amplitude      REAL    NOT NULL,
                phase          REAL    NOT NULL,
                real_part      REAL    NOT NULL,
                imag_part      REAL    NOT NULL
            );

            CREATE INDEX idx_grid_lon_lat   ON grid_points(lon, lat);
            CREATE INDEX idx_harmonic_grid  ON harmonic_constants(grid_point_id);
            CREATE INDEX idx_harmonic_const ON harmonic_constants(constituent_id);
        """)
        self.conn.commit()
        logger.info("Database schema created: %s", self.output_db)

    # ── Constituents metadata ─────────────────────────────────────────────────

    def _insert_constituents_metadata(self) -> None:
        rows = [
            (i, name.upper(), SPEED_DEG_HR.get(name, 0.0), desc)
            for i, (name, desc) in enumerate(CONSTITUENTS)
        ]
        self.conn.executemany(
            "INSERT INTO constituents_metadata (id, name, frequency, description) VALUES (?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        logger.info(
            "Inserted %d constituents: %s",
            len(rows),
            ", ".join(n.upper() for n, _ in CONSTITUENTS),
        )

    # ── Main extraction ───────────────────────────────────────────────────────

    def _extract_and_save(self) -> None:
        """
        For each ocean grid point in the Seribu region:
          1. Insert a grid_points row.
          2. For each of the 15 constituents, load hRe/hIm from the per-constituent
             file, compute amplitude/phase, and insert a harmonic_constants row.
        """
        ny = self._lat_end - self._lat_start
        nx = self._lon_end - self._lon_start

        # ── Step 1: load all 15 constituent arrays into memory ──────────────
        # Each entry: (hRe region, hIm region) — shape (ny, nx)
        logger.info("Loading %d constituent files ...", len(CONSTITUENTS))
        constituent_data: list[tuple[np.ndarray, np.ndarray]] = []

        for const_name, _ in CONSTITUENTS:
            fpath = self.tpxo_dir / f"h_{const_name}_tpxo10_atlas_30_v2.nc"
            with Dataset(str(fpath), "r") as ds:
                # TPXO10-atlas-v2: hRe/hIm stored as int32 in MILLIMETRES (no scale_factor)
                # Must divide by 1000 to convert to metres for harmonic prediction.
                hRe_full = ds.variables["hRe"]
                hIm_full = ds.variables["hIm"]

                units = getattr(hRe_full, "units", "millimeter").lower()
                to_metres = 1.0 / 1000.0 if "milli" in units else 1.0

                # TPXO10-atlas-v2 layout: hRe/hIm shape is (lon, lat) — i.e. (x, y)
                # NOT (lat, lon) as in TPXO9. Slice accordingly then transpose to (lat, lon).
                hRe_region = np.array(
                    hRe_full[self._lon_start:self._lon_end, self._lat_start:self._lat_end],
                    dtype=float,
                ).T * to_metres  # .T → (lat, lon) for consistent downstream indexing

                hIm_region = np.array(
                    hIm_full[self._lon_start:self._lon_end, self._lat_start:self._lat_end],
                    dtype=float,
                ).T * to_metres  # .T → (lat, lon)

            constituent_data.append((hRe_region, hIm_region))
            logger.info("  Loaded %-5s  hRe range [%.4f, %.4f] m  (converted from %s)",
                        const_name, float(hRe_region.min()), float(hRe_region.max()), units)

        # ── Step 2: get ocean mask for region ───────────────────────────────
        # grid hz is also (lon, lat) — slice and transpose to (lat, lon)
        mask_region = self._mask[
            self._lon_start:self._lon_end,
            self._lat_start:self._lat_end,
        ].T
        lon_region = self._lon[self._lon_start:self._lon_end]
        lat_region = self._lat[self._lat_start:self._lat_end]

        # ── Step 3: iterate grid points, insert data ─────────────────────────
        logger.info("Extracting ocean grid points and inserting harmonic constants ...")
        cur = self.conn.cursor()
        ocean_points = land_points = harmonic_count = 0

        for i in range(ny):
            for j in range(nx):
                lat_val = float(lat_region[i])
                lon_val = float(lon_region[j])

                # Skip land points (mask == 0)
                if mask_region[i, j] == 0:
                    land_points += 1
                    continue

                # Check: is there at least one non-zero constituent?
                has_data = False
                for hRe_r, hIm_r in constituent_data:
                    val = complex(hRe_r[i, j], hIm_r[i, j])
                    if not (np.isnan(val.real) or np.isnan(val.imag)) and abs(val) > 1e-7:
                        has_data = True
                        break

                if not has_data:
                    land_points += 1
                    continue

                # Insert grid point
                cur.execute(
                    "INSERT INTO grid_points (lon, lat, lon_index, lat_index) VALUES (?, ?, ?, ?)",
                    (lon_val, lat_val, self._lon_start + j, self._lat_start + i),
                )
                gp_id = cur.lastrowid
                ocean_points += 1

                # Insert harmonic constants for all 15 constituents
                for const_id, (hRe_r, hIm_r) in enumerate(constituent_data):
                    re_val = float(hRe_r[i, j])
                    im_val = float(hIm_r[i, j])

                    if np.isnan(re_val) or np.isnan(im_val):
                        continue

                    # Complex phasor: h = hRe + i*hIm
                    amplitude = float(np.sqrt(re_val**2 + im_val**2))
                    if amplitude < 1e-7:
                        continue

                    # Phase = Greenwich phase lag kappa (degrees)
                    # Convention: h(t) = A*cos(omega*t + V0 + u - kappa)
                    # With h = hRe + i*hIm = A*exp(i*phi):
                    # phi = atan2(hIm, hRe), kappa = -phi (mod 360)
                    # TPXO convention: h(t) = hRe*cos(ωt) - hIm*sin(ωt) = A*cos(ωt + κ)
                    # → κ = atan2(-hIm, hRe)   (note: NEGATIVE hIm)
                    phase = float(np.degrees(np.arctan2(-im_val, re_val)) % 360.0)

                    cur.execute(
                        """INSERT INTO harmonic_constants
                           (grid_point_id, constituent_id, amplitude, phase, real_part, imag_part)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (gp_id, const_id, amplitude, phase, re_val, im_val),
                    )
                    harmonic_count += 1

            # Commit every 10 rows and log progress
            if (i + 1) % 10 == 0:
                self.conn.commit()
                logger.info(
                    "  Row %d/%d — %d ocean, %d land, %d harmonics so far",
                    i + 1, ny, ocean_points, land_points, harmonic_count,
                )

        self.conn.commit()
        logger.info(
            "Extraction complete: %d ocean points, %d land/invalid, %d harmonic constants",
            ocean_points, land_points, harmonic_count,
        )

    # ── Optimize ──────────────────────────────────────────────────────────────

    def _optimize_database(self) -> None:
        logger.info("Optimizing database (VACUUM + ANALYZE) ...")
        self.conn.execute("VACUUM")
        self.conn.execute("ANALYZE")
        self.conn.commit()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preprocess TPXO10-atlas-v2 for the Kepulauan Seribu region",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/preprocess_tpxo10.py \\
      --tpxo-dir  data/tpxo10_atlas_v2 \\
      --output-db data/tpxo_seribu.db

The folder tpxo10_atlas_v2 must contain:
  grid_tpxo10atlas_v2.nc
  h_2n2_tpxo10_atlas_30_v2.nc
  h_k1_tpxo10_atlas_30_v2.nc
  ... (15 constituent files total)
        """,
    )
    parser.add_argument(
        "--tpxo-dir",
        type=Path,
        required=True,
        help="Directory containing TPXO10-atlas-v2 .nc files",
    )
    parser.add_argument(
        "--output-db",
        type=Path,
        default=Path("data/tpxo_seribu.db"),
        help="Output SQLite database path (default: data/tpxo_seribu.db)",
    )
    args = parser.parse_args()

    logger.info("TPXO10-atlas-v2 preprocessor for Kepulauan Seribu")
    logger.info("Input directory : %s", args.tpxo_dir)
    logger.info("Output database : %s", args.output_db)
    logger.info("Bounding box    : lon [%.1f, %.1f], lat [%.1f, %.1f]",
                LON_MIN, LON_MAX, LAT_MIN, LAT_MAX)

    TPXO10Preprocessor(args.tpxo_dir, args.output_db).process()


if __name__ == "__main__":
    main()