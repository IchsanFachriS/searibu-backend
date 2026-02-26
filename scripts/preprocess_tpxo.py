#!/usr/bin/env python3
"""
Preprocess TPXO9 Atlas v5 NetCDF file for Kepulauan Seribu region.

This script extracts a subset of the global TPXO tidal model covering
Kepulauan Seribu and saves it to an SQLite database for faster querying.

Memory-efficient version: Reads only the regional subset instead of loading
the entire 10GB global dataset into memory.

Usage:
    python preprocess_tpxo.py --tpxo-file data/TPXO9_atlas_v5.nc --output-db data/tpxo_seribu.db
"""

import argparse
import logging
import sqlite3
from pathlib import Path
import numpy as np
from netCDF4 import Dataset

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger(__name__)

# Region bounds for Kepulauan Seribu
# Expanded slightly to ensure full coverage
LAT_MIN = -6.0   # Southern boundary
LAT_MAX = -5.3   # Northern boundary
LON_MIN = 106.3  # Western boundary
LON_MAX = 107.0  # Eastern boundary


class TPXOPreprocessor:
    """Preprocesses TPXO9 data for Kepulauan Seribu region"""
    
    def __init__(self, tpxo_file: Path, output_db: Path):
        """
        Initialize preprocessor
        
        Args:
            tpxo_file: Path to TPXO9_atlas_v5.nc file
            output_db: Path to output SQLite database
        """
        self.tpxo_file = tpxo_file
        self.output_db = output_db
        self.dataset = None
        self.conn = None
        
    def load_tpxo(self):
        """Load TPXO NetCDF file"""
        logger.info(f"Loading TPXO file: {self.tpxo_file}")
        self.dataset = Dataset(self.tpxo_file, 'r')
        logger.info("TPXO file loaded successfully")
        
        # Log some basic info
        logger.info(f"Global dimensions: lon={len(self.dataset.dimensions['lon'])}, lat={len(self.dataset.dimensions['lat'])}, constituents={len(self.dataset.dimensions['constituents'])}")
        
    def create_database(self):
        """Create SQLite database structure"""
        logger.info(f"Creating database: {self.output_db}")
        
        # Remove existing database if it exists
        if self.output_db.exists():
            self.output_db.unlink()
            
        self.conn = sqlite3.connect(str(self.output_db))
        cursor = self.conn.cursor()
        
        # Create tables
        cursor.execute('''
            CREATE TABLE grid_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lon REAL NOT NULL,
                lat REAL NOT NULL,
                lon_index INTEGER NOT NULL,
                lat_index INTEGER NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE harmonic_constants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grid_point_id INTEGER NOT NULL,
                constituent_id INTEGER NOT NULL,
                amplitude REAL NOT NULL,
                phase REAL NOT NULL,
                real_part REAL NOT NULL,
                imag_part REAL NOT NULL,
                FOREIGN KEY (grid_point_id) REFERENCES grid_points(id),
                FOREIGN KEY (constituent_id) REFERENCES constituents_metadata(id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE constituents_metadata (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                frequency REAL NOT NULL,
                description TEXT
            )
        ''')
        
        # Create indices for faster queries
        cursor.execute('CREATE INDEX idx_grid_lon_lat ON grid_points(lon, lat)')
        cursor.execute('CREATE INDEX idx_harmonic_grid ON harmonic_constants(grid_point_id)')
        cursor.execute('CREATE INDEX idx_harmonic_const ON harmonic_constants(constituent_id)')
        
        self.conn.commit()
        logger.info("Database structure created")
        
    def insert_constituents_metadata(self):
        """Insert tidal constituent metadata from TPXO file"""
        # Read constituent names and frequencies from the file
        constituent_order = self.dataset.variables['constituents'].constituent_order
        constituent_names = constituent_order.split()
        
        # Read omega (frequency in 1/s, convert to rad/hour)
        omega = self.dataset.variables['omega'][:]
        frequencies_rad_per_hour = omega * 3600.0  # Convert from 1/s to rad/hour
        
        # Descriptions for each constituent
        descriptions = {
            '2n2': 'Lunar elliptical semidiurnal second-order',
            'k1': 'Lunisolar diurnal',
            'k2': 'Lunisolar semidiurnal',
            'm2': 'Principal lunar semidiurnal',
            'm4': 'Shallow water overtide of M2',
            'mf': 'Lunisolar fortnightly',
            'mm': 'Lunar monthly',
            'mn4': 'Shallow water quarter diurnal',
            'ms4': 'Shallow water quarter diurnal',
            'n2': 'Larger lunar elliptic semidiurnal',
            'o1': 'Principal lunar diurnal',
            'p1': 'Principal solar diurnal',
            'q1': 'Larger lunar elliptic diurnal',
            's1': 'Solar diurnal',
            's2': 'Principal solar semidiurnal'
        }
        
        constituents = []
        for i, name in enumerate(constituent_names):
            name_upper = name.upper()
            freq = float(frequencies_rad_per_hour[i])
            desc = descriptions.get(name, f'Tidal constituent {name_upper}')
            constituents.append((i, name_upper, freq, desc))
        
        cursor = self.conn.cursor()
        cursor.executemany(
            'INSERT INTO constituents_metadata (id, name, frequency, description) VALUES (?, ?, ?, ?)',
            constituents
        )
        self.conn.commit()
        logger.info(f"Inserted {len(constituents)} constituent metadata entries")
        logger.info(f"Constituents: {', '.join([c[1] for c in constituents])}")
        
    def find_region_indices(self):
        """Find lat/lon indices for Kepulauan Seribu region"""
        lon = self.dataset.variables['lon'][:]
        lat = self.dataset.variables['lat'][:]
        
        # Find indices for the region
        # TPXO9 uses 0-360 longitude convention
        lon_min_360 = LON_MIN if LON_MIN >= 0 else LON_MIN + 360
        lon_max_360 = LON_MAX if LON_MAX >= 0 else LON_MAX + 360
        
        lon_mask = (lon >= lon_min_360) & (lon <= lon_max_360)
        lat_mask = (lat >= LAT_MIN) & (lat <= LAT_MAX)
        
        self.lon_indices = np.where(lon_mask)[0]
        self.lat_indices = np.where(lat_mask)[0]
        
        self.lon_start = self.lon_indices[0]
        self.lon_end = self.lon_indices[-1] + 1
        self.lat_start = self.lat_indices[0]
        self.lat_end = self.lat_indices[-1] + 1
        
        self.nx_region = len(self.lon_indices)
        self.ny_region = len(self.lat_indices)
        
        # Store regional coordinates
        self.lon_region = lon[self.lon_start:self.lon_end]
        self.lat_region = lat[self.lat_start:self.lat_end]
        
        logger.info(f"Region indices: lon[{self.lon_start}:{self.lon_end}], lat[{self.lat_start}:{self.lat_end}]")
        logger.info(f"Region size: {self.nx_region} x {self.ny_region} grid points")
        logger.info(f"Longitude range: {self.lon_region[0]:.4f} to {self.lon_region[-1]:.4f}")
        logger.info(f"Latitude range: {self.lat_region[0]:.4f} to {self.lat_region[-1]:.4f}")
        
    def extract_and_save(self):
        """Extract Kepulauan Seribu region and save to database"""
        logger.info(f"Processing {self.ny_region} x {self.nx_region} grid points")
        
        # Get scale factor for the data
        hRe_var = self.dataset.variables['hRe']
        hIm_var = self.dataset.variables['hIm']
        scale_factor = hRe_var.scale_factor
        logger.info(f"Using scale factor: {scale_factor}")
        
        # Read harmonic constants ONLY for the region (not entire global grid)
        # This prevents memory issues by using array slicing
        logger.info(f"Reading hRe for region: constituents x lat[{self.lat_start}:{self.lat_end}] x lon[{self.lon_start}:{self.lon_end}]")
        hRe_raw = hRe_var[:, self.lat_start:self.lat_end, self.lon_start:self.lon_end]
        
        logger.info(f"Reading hIm for region: constituents x lat[{self.lat_start}:{self.lat_end}] x lon[{self.lon_start}:{self.lon_end}]")
        hIm_raw = hIm_var[:, self.lat_start:self.lat_end, self.lon_start:self.lon_end]
        
        # Apply scale factor to convert from int16 to actual values
        hRe_region = hRe_raw * scale_factor
        hIm_region = hIm_raw * scale_factor
        
        # Combine into complex array
        h_region = hRe_region + 1j * hIm_region
        
        logger.info(f"Harmonic constants loaded: shape = {h_region.shape}")
        
        # Get number of constituents
        n_constituents = h_region.shape[0]
        
        cursor = self.conn.cursor()
        
        # Process each grid point
        grid_point_count = 0
        harmonic_count = 0
        land_points = 0
        
        for i in range(self.ny_region):
            for j in range(self.nx_region):
                lat = float(self.lat_region[i])
                lon = float(self.lon_region[j])
                
                # Convert longitude to -180 to 180 convention if needed
                if lon > 180:
                    lon = lon - 360
                
                # Check if this is a valid ocean point (at least one constituent has data)
                has_data = False
                for const_id in range(n_constituents):
                    h_value = h_region[const_id, i, j]
                    if not np.ma.is_masked(h_value) and not np.isnan(h_value):
                        has_data = True
                        break
                
                if not has_data:
                    land_points += 1
                    continue
                
                # Insert grid point
                cursor.execute(
                    'INSERT INTO grid_points (lon, lat, lon_index, lat_index) VALUES (?, ?, ?, ?)',
                    (lon, lat, self.lon_start + j, self.lat_start + i)
                )
                grid_point_id = cursor.lastrowid
                grid_point_count += 1
                
                # Insert harmonic constants for this grid point
                for const_id in range(n_constituents):
                    h_value = h_region[const_id, i, j]
                    
                    # Skip if masked (land points) or NaN
                    if np.ma.is_masked(h_value) or np.isnan(h_value):
                        continue
                    
                    amplitude = float(np.abs(h_value))
                    phase = float(np.angle(h_value, deg=True)) % 360.0  # normalkan ke 0–360°
                    real_part = float(h_value.real)   # tetap simpan untuk referensi
                    imag_part = float(h_value.imag)
                    
                    cursor.execute(
                        '''INSERT INTO harmonic_constants
                        (grid_point_id, constituent_id, amplitude, phase, real_part, imag_part)
                        VALUES (?, ?, ?, ?, ?, ?)''',
                        (grid_point_id, const_id, amplitude, phase, real_part, imag_part)
                    )
                    harmonic_count += 1
            
            # Commit every 10 rows to avoid huge transaction
            if (i + 1) % 10 == 0:
                self.conn.commit()
                logger.info(f"Processed {i + 1}/{self.ny_region} rows ({grid_point_count} ocean points, {land_points} land points, {harmonic_count} harmonics)")
        
        # Final commit
        self.conn.commit()
        logger.info(f"Total: {grid_point_count} ocean grid points, {land_points} land points, {harmonic_count} harmonic constants")
        
    def optimize_database(self):
        """Optimize database after data insertion"""
        logger.info("Optimizing database...")
        cursor = self.conn.cursor()
        cursor.execute("VACUUM")
        cursor.execute("ANALYZE")
        self.conn.commit()
        logger.info("Database optimized")
        
    def close(self):
        """Close dataset and database connections"""
        if self.dataset:
            self.dataset.close()
            logger.info("TPXO dataset closed")
            
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")
            
    def process(self):
        """Run complete preprocessing workflow"""
        try:
            self.load_tpxo()
            self.create_database()
            self.insert_constituents_metadata()
            self.find_region_indices()
            self.extract_and_save()
            self.optimize_database()
            logger.info("Preprocessing completed successfully")
            
            # Print database file size
            db_size_mb = self.output_db.stat().st_size / (1024 * 1024)
            logger.info(f"Database size: {db_size_mb:.2f} MB")
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser(
        description='Preprocess TPXO9 data for Kepulauan Seribu region'
    )
    parser.add_argument(
        '--tpxo-file',
        type=Path,
        required=True,
        help='Path to TPXO9_atlas_v5.nc file'
    )
    parser.add_argument(
        '--output-db',
        type=Path,
        required=True,
        help='Path to output SQLite database'
    )
    
    args = parser.parse_args()
    
    # Validate input file exists
    if not args.tpxo_file.exists():
        logger.error(f"TPXO file not found: {args.tpxo_file}")
        return 1
        
    # Create output directory if needed
    args.output_db.parent.mkdir(parents=True, exist_ok=True)
    
    # Run preprocessing
    preprocessor = TPXOPreprocessor(args.tpxo_file, args.output_db)
    preprocessor.process()
    
    return 0


if __name__ == '__main__':
    exit(main())