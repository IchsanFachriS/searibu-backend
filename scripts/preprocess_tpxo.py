"""Extract the Kepulauan Seribu region from a TPXO9 NetCDF file into SQLite.

Run once locally after downloading TPXO9_atlas_v5.nc:

    python scripts/preprocess_tpxo.py \
        --tpxo-file data/TPXO9_atlas_v5.nc \
        --output-db data/tpxo_seribu.db

The generated SQLite database (tpxo_seribu.db) is committed to the repository
and used by TPXOPredictor at runtime. The source NetCDF file is NOT committed
(it is ~10 GB and listed in .gitignore).

Prerequisites:
    pip install netCDF4 numpy tqdm
"""

import argparse
import logging
import sqlite3
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Bounding box for Kepulauan Seribu (expanded slightly for full coverage)
LAT_MIN = -6.0
LAT_MAX = -5.3
LON_MIN = 106.3
LON_MAX = 107.0


class TPXOPreprocessor:
    """Extracts a regional subset of TPXO9 harmonic constants into SQLite."""

    def __init__(self, tpxo_file: Path, output_db: Path):
        self.tpxo_file = tpxo_file
        self.output_db = output_db
        self.dataset = None
        self.conn = None

    # ── Public entry point ────────────────────────────────────────────────────

    def process(self) -> None:
        """Run the full extraction pipeline."""
        try:
            self._load_tpxo()
            self._create_database()
            self._insert_constituents_metadata()
            self._find_region_indices()
            self._extract_and_save()
            self._optimize_database()
            size_mb = self.output_db.stat().st_size / (1024 * 1024)
            logger.info("Done — database size: %.2f MB", size_mb)
        finally:
            self._close()

    # ── Pipeline steps ────────────────────────────────────────────────────────

    def _load_tpxo(self) -> None:
        logger.info("Opening TPXO file: %s", self.tpxo_file)
        self.dataset = Dataset(self.tpxo_file, "r")
        dims = self.dataset.dimensions
        logger.info(
            "Global dimensions: lon=%d lat=%d constituents=%d",
            len(dims["lon"]), len(dims["lat"]), len(dims["constituents"]),
        )

    def _create_database(self) -> None:
        logger.info("Creating database: %s", self.output_db)
        if self.output_db.exists():
            self.output_db.unlink()

        self.conn = sqlite3.connect(str(self.output_db))
        cur = self.conn.cursor()
        cur.executescript("""
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

    def _insert_constituents_metadata(self) -> None:
        names = self.dataset.variables["constituents"].constituent_order.split()
        omega = self.dataset.variables["omega"][:]
        frequencies = omega * 3600.0  # rad/s → rad/hour

        descriptions = {
            "2n2": "Lunar elliptical semidiurnal second-order",
            "k1":  "Lunisolar diurnal",
            "k2":  "Lunisolar semidiurnal",
            "m2":  "Principal lunar semidiurnal",
            "m4":  "Shallow water overtide of M2",
            "mf":  "Lunisolar fortnightly",
            "mm":  "Lunar monthly",
            "mn4": "Shallow water quarter diurnal",
            "ms4": "Shallow water quarter diurnal",
            "n2":  "Larger lunar elliptic semidiurnal",
            "o1":  "Principal lunar diurnal",
            "p1":  "Principal solar diurnal",
            "q1":  "Larger lunar elliptic diurnal",
            "s1":  "Solar diurnal",
            "s2":  "Principal solar semidiurnal",
        }

        self.conn.cursor().executemany(
            "INSERT INTO constituents_metadata (id, name, frequency, description) VALUES (?, ?, ?, ?)",
            [
                (i, name.upper(), float(frequencies[i]), descriptions.get(name, f"Tidal constituent {name.upper()}"))
                for i, name in enumerate(names)
            ],
        )
        self.conn.commit()
        logger.info("Constituents inserted: %s", ", ".join(n.upper() for n in names))

    def _find_region_indices(self) -> None:
        lon = self.dataset.variables["lon"][:]
        lat = self.dataset.variables["lat"][:]

        lon_min_360 = LON_MIN if LON_MIN >= 0 else LON_MIN + 360
        lon_max_360 = LON_MAX if LON_MAX >= 0 else LON_MAX + 360

        self._lon_idx = np.where((lon >= lon_min_360) & (lon <= lon_max_360))[0]
        self._lat_idx = np.where((lat >= LAT_MIN) & (lat <= LAT_MAX))[0]

        self._lon_start = int(self._lon_idx[0])
        self._lon_end = int(self._lon_idx[-1]) + 1
        self._lat_start = int(self._lat_idx[0])
        self._lat_end = int(self._lat_idx[-1]) + 1

        self._lon_region = lon[self._lon_start:self._lon_end]
        self._lat_region = lat[self._lat_start:self._lat_end]

        logger.info(
            "Region: lon[%d:%d] lat[%d:%d] → %d×%d grid points",
            self._lon_start, self._lon_end,
            self._lat_start, self._lat_end,
            len(self._lon_idx), len(self._lat_idx),
        )

    def _extract_and_save(self) -> None:
        ny = len(self._lat_idx)
        nx = len(self._lon_idx)
        logger.info("Reading harmonic constants for %d×%d region ...", ny, nx)

        scale = self.dataset.variables["hRe"].scale_factor
        hRe = self.dataset.variables["hRe"][:, self._lat_start:self._lat_end, self._lon_start:self._lon_end] * scale
        hIm = self.dataset.variables["hIm"][:, self._lat_start:self._lat_end, self._lon_start:self._lon_end] * scale
        h = hRe + 1j * hIm
        n_const = h.shape[0]

        cur = self.conn.cursor()
        ocean_points = land_points = harmonic_count = 0

        for i in range(ny):
            for j in range(nx):
                lat_val = float(self._lat_region[i])
                lon_val = float(self._lon_region[j])
                if lon_val > 180:
                    lon_val -= 360

                # Skip land — no valid constituents
                valid = [
                    h[k, i, j] for k in range(n_const)
                    if not np.ma.is_masked(h[k, i, j]) and not np.isnan(h[k, i, j])
                ]
                if not valid:
                    land_points += 1
                    continue

                cur.execute(
                    "INSERT INTO grid_points (lon, lat, lon_index, lat_index) VALUES (?, ?, ?, ?)",
                    (lon_val, lat_val, self._lon_start + j, self._lat_start + i),
                )
                gp_id = cur.lastrowid
                ocean_points += 1

                for k in range(n_const):
                    val = h[k, i, j]
                    if np.ma.is_masked(val) or np.isnan(val):
                        continue
                    cur.execute(
                        """
                        INSERT INTO harmonic_constants
                            (grid_point_id, constituent_id, amplitude, phase, real_part, imag_part)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (gp_id, k, float(np.abs(val)), float(np.angle(val, deg=True)) % 360.0,
                         float(val.real), float(val.imag)),
                    )
                    harmonic_count += 1

            if (i + 1) % 10 == 0:
                self.conn.commit()
                logger.info(
                    "  Row %d/%d — %d ocean, %d land, %d harmonics",
                    i + 1, ny, ocean_points, land_points, harmonic_count,
                )

        self.conn.commit()
        logger.info(
            "Extraction complete: %d ocean points, %d land points, %d harmonic constants",
            ocean_points, land_points, harmonic_count,
        )

    def _optimize_database(self) -> None:
        logger.info("Optimising database ...")
        cur = self.conn.cursor()
        cur.execute("VACUUM")
        cur.execute("ANALYZE")
        self.conn.commit()

    def _close(self) -> None:
        if self.dataset:
            self.dataset.close()
        if self.conn:
            self.conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract TPXO9 regional subset for Kepulauan Seribu")
    parser.add_argument("--tpxo-file", type=Path, required=True, help="Path to TPXO9_atlas_v5.nc")
    parser.add_argument("--output-db", type=Path, required=True, help="Path to output SQLite database")
    args = parser.parse_args()

    if not args.tpxo_file.exists():
        logger.error("TPXO file not found: %s", args.tpxo_file)
        raise SystemExit(1)

    args.output_db.parent.mkdir(parents=True, exist_ok=True)
    TPXOPreprocessor(args.tpxo_file, args.output_db).process()


if __name__ == "__main__":
    main()