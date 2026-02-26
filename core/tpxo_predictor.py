"""
TPXO Tide Predictor — Harmonic Analysis (15 Constituents)

Formula: h(t) = Σ  f_k · A_k · cos( ω_k·t  +  V₀_k  +  u_k  -  κ_k )

Referensi:
  Schureman (1958) Manual of Harmonic Analysis and Prediction of Tides
  Foreman (1977)   Manual for Tidal Heights Analysis and Prediction
"""

import sqlite3
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import math


# ══════════════════════════════════════════════════════════════
# KONSTANTA KONSTITUEN
# ══════════════════════════════════════════════════════════════

CONS_LIST = ['2n2', 'k1', 'k2', 'm2', 'm4', 'mf', 'mm', 'mn4', 'ms4',
             'n2', 'o1', 'p1', 'q1', 's1', 's2']

# Kecepatan sudut dasar (°/jam) — Schureman (1958)
TAU = 14.49205211
S   =  0.54901653
H   =  0.04106864
P   =  0.00464183

FREQ = {
    '2n2': 2*TAU - 2*S + 2*P,
    'k1' : TAU  + S,
    'k2' : 2*TAU + 2*S,
    'm2' : 2*TAU,
    'm4' : 4*TAU,
    'mf' : 2*S,
    'mm' : S - P,
    'mn4': 4*TAU - S + P,
    'ms4': 4*TAU + 2*S - 2*H,
    'n2' : 2*TAU - S + P,
    'o1' : TAU  - 2*S + H,
    'p1' : TAU  + S - 2*H,
    'q1' : TAU  - 3*S + H + P,
    's1' : 15.0000000,
    's2' : 30.0000000,
}


# ══════════════════════════════════════════════════════════════
# FUNGSI ASTRONOMIS
# ══════════════════════════════════════════════════════════════

def julian_day(dt: datetime) -> float:
    """Julian Day Number — Meeus (1991)."""
    y, m, d = dt.year, dt.month, dt.day
    hf = (dt.hour + dt.minute / 60 + dt.second / 3600) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + A // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5 + hf


def astronomical_arguments(dt: datetime) -> Tuple[float, float, float, float, float]:
    """
    Mean longitudes (°) pada waktu dt (UTC).
    T = abad Julian sejak J1900 (JD 2415020.0).
    Schureman (1958) Tabel 1.
    """
    T  = (julian_day(dt) - 2415020.0) / 36524.25
    s  = (277.0247 + 481267.8906 * T) % 360
    h  = (280.1895 +  36000.7689 * T) % 360
    p  = (334.3853 +   4069.0340 * T) % 360
    N  = (259.1561 -   1934.1423 * T) % 360
    p1 = (281.2209 +      1.7192 * T) % 360
    return s, h, p, N, p1


def compute_V0(s: float, h: float, p: float) -> Dict[str, float]:
    """Argumen astronomis V₀ tiap konstituen pada epoch t₀ (°)."""
    return {
        '2n2': (2*h - 4*s + 2*p) % 360,
        'k1' : (h  + 90)         % 360,
        'k2' : (2*h)             % 360,
        'm2' : (2*h - 2*s)       % 360,
        'm4' : (4*h - 4*s)       % 360,
        'mf' : (-2*s)            % 360,
        'mm' : (s - p)           % 360,
        'mn4': (4*h - 5*s + p)   % 360,
        'ms4': (2*h - 2*s)       % 360,
        'n2' : (2*h - 3*s + p)   % 360,
        'o1' : (h  - 2*s - 90)   % 360,
        'p1' : (-h + 90)         % 360,
        'q1' : (h  - 3*s + p - 90) % 360,
        's1' : 90.0,
        's2' : 0.0,
    }


def nodal_factors(N_deg: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Faktor nodal f (amplitudo) dan u (koreksi fase, °).
    N_deg: longitude simpul menaik Bulan (°).
    Schureman (1958) & Foreman (1977).
    """
    Nr  = math.radians(N_deg)
    N2r = math.radians(2 * N_deg)

    f_M2 = math.sqrt(
        (1 - 0.03731*math.cos(Nr) + 0.00052*math.cos(N2r))**2 +
        (    0.03731*math.sin(Nr) - 0.00052*math.sin(N2r))**2)
    f_K2 = math.sqrt(
        (1 + 0.2852*math.cos(Nr) + 0.0324*math.cos(N2r))**2 +
        (    0.3108*math.sin(Nr) + 0.0324*math.sin(N2r))**2)
    f_K1 = math.sqrt(
        (1 + 0.1158*math.cos(Nr) - 0.0029*math.cos(N2r))**2 +
        (    0.1554*math.sin(Nr) - 0.0029*math.sin(N2r))**2)
    f_O1 = math.sqrt(
        (1 - 0.10980*math.cos(Nr) + 0.00148*math.cos(N2r))**2 +
        (    0.10980*math.sin(Nr) - 0.00148*math.sin(N2r))**2)
    f_Mf = math.sqrt(
        (1 - 0.15636*math.cos(Nr))**2 +
        (    0.15636*math.sin(Nr))**2)
    f_Mm = 1.0 - 0.13023*math.cos(Nr)

    f = {
        '2n2': f_M2, 'k1': f_K1, 'k2': f_K2, 'm2': f_M2,
        'm4': f_M2**2, 'mf': f_Mf, 'mm': f_Mm, 'mn4': f_M2**2,
        'ms4': f_M2, 'n2': f_M2, 'o1': f_O1, 'p1': 1.0,
        'q1': f_O1, 's1': 1.0, 's2': 1.0,
    }

    u_M2 = math.degrees(math.atan2(
        -0.03731*math.sin(Nr) + 0.00052*math.sin(N2r),
         1 - 0.03731*math.cos(Nr) + 0.00052*math.cos(N2r)))
    u_K2 = math.degrees(math.atan2(
        -(0.3108*math.sin(Nr) + 0.0324*math.sin(N2r)),
          1 + 0.2852*math.cos(Nr) + 0.0324*math.cos(N2r)))
    u_K1 = math.degrees(math.atan2(
        -(0.1554*math.sin(Nr) - 0.0029*math.sin(N2r)),
          1 + 0.1158*math.cos(Nr) - 0.0029*math.cos(N2r)))
    u_O1 = math.degrees(math.atan2(
         0.10980*math.sin(Nr) - 0.00148*math.sin(N2r),
         1 - 0.10980*math.cos(Nr) + 0.00148*math.cos(N2r)))
    u_Mf = math.degrees(math.atan2(
        -0.15636*math.sin(Nr),
         1 - 0.15636*math.cos(Nr)))

    u = {
        '2n2': u_M2, 'k1': u_K1, 'k2': u_K2, 'm2': u_M2,
        'm4': 2*u_M2, 'mf': u_Mf, 'mm': 0.0, 'mn4': 2*u_M2,
        'ms4': u_M2, 'n2': u_M2, 'o1': u_O1, 'p1': 0.0,
        'q1': u_O1, 's1': 0.0, 's2': 0.0,
    }

    return f, u


def predict_harmonic(
    t_hours: np.ndarray,
    amp: Dict[str, float],
    kappa: Dict[str, float],
    V0_dict: Dict[str, float],
    f_dict: Dict[str, float],
    u_dict: Dict[str, float],
) -> np.ndarray:
    """
    h(t) = Σ  f_k · A_k · cos( ω_k·t  +  V₀_k  +  u_k  -  κ_k )
    """
    h = np.zeros(len(t_hours))
    for nama in CONS_LIST:
        A = amp.get(nama, 0.0)
        k = kappa.get(nama, 0.0)
        if math.isnan(A) or math.isnan(k) or A < 1e-6:
            continue
        omega = FREQ[nama]
        arg = np.deg2rad(omega * t_hours + V0_dict[nama] + u_dict[nama] - k)
        h += f_dict[nama] * A * np.cos(arg)
    return h


# ══════════════════════════════════════════════════════════════
# KELAS PREDICTOR UTAMA
# ══════════════════════════════════════════════════════════════

class TPXOPredictor:
    """Prediksi pasang surut dari SQLite database TPXO9 (15 konstituen)."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database tidak ditemukan: {self.db_path}")
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()

    def find_nearest_grid(self, lon: float, lat: float) -> Dict:
        """Cari titik grid terdekat (jarak Haversine)."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, lon, lat FROM grid_points')
        rows = cursor.fetchall()
        if not rows:
            raise ValueError("Tidak ada grid points di database")

        best = None
        best_dist = float('inf')
        for row in rows:
            dist = self._haversine(lon, lat, row['lon'], row['lat'])
            if dist < best_dist:
                best_dist = dist
                best = dict(row)
                best['distance_km'] = dist
        return best

    def get_harmonics(self, grid_point_id: int) -> Dict[str, Dict[str, float]]:
        """Ambil amplitude (m) dan phase (°) dari database."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT cm.name, hc.amplitude, hc.phase
            FROM harmonic_constants hc
            JOIN constituents_metadata cm ON hc.constituent_id = cm.id
            WHERE hc.grid_point_id = ?
            ORDER BY cm.id
        ''', (grid_point_id,))

        result = {}
        for row in cursor.fetchall():
            name_lower = row['name'].lower()
            result[name_lower] = {
                'amplitude': float(row['amplitude']),
                'phase': float(row['phase']),
            }
        return result

    def predict(
        self,
        lon: float,
        lat: float,
        start_dt: datetime,
        end_dt: datetime,
        interval_hours: int = 1,
    ) -> Dict:
        """
        Prediksi pasut dari start_dt sampai end_dt.

        Args:
            lon, lat       : koordinat target
            start_dt       : awal prediksi (UTC)
            end_dt         : akhir prediksi (UTC)
            interval_hours : resolusi output dalam jam (default 1)

        Returns:
            dict dengan predictions, statistics, grid, metadata
        """
        # Normalisasi ke UTC
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        if end_dt <= start_dt:
            raise ValueError("end_dt harus setelah start_dt")

        # Grid terdekat & harmonik
        grid = self.find_nearest_grid(lon, lat)
        harmonics = self.get_harmonics(grid['id'])
        if not harmonics:
            raise ValueError(f"Tidak ada data harmonik untuk grid {grid['id']}")

        # Siapkan amp & kappa
        amp   = {n: harmonics.get(n, {}).get('amplitude', 0.0) for n in CONS_LIST}
        kappa = {n: harmonics.get(n, {}).get('phase', 0.0)     for n in CONS_LIST}

        # Array waktu
        total_hours = (end_dt - start_dt).total_seconds() / 3600.0
        n_steps = int(total_hours / interval_hours) + 1
        times = [start_dt + timedelta(hours=i * interval_hours) for i in range(n_steps)]
        t_jam = np.arange(n_steps, dtype=float) * interval_hours

        # Argumen astronomis di epoch t₀ (awal prediksi)
        t0_naive = start_dt.replace(tzinfo=None)
        s0, h0, p0, N0, _ = astronomical_arguments(t0_naive)
        V0_dict = compute_V0(s0, h0, p0)

        # Faktor nodal di pertengahan periode
        t_mid = t0_naive + timedelta(hours=total_hours / 2)
        _, _, _, N_mid, _ = astronomical_arguments(t_mid)
        f_dict, u_dict = nodal_factors(N_mid)

        # Hitung prediksi
        h_pred = predict_harmonic(t_jam, amp, kappa, V0_dict, f_dict, u_dict)

        predictions = [
            {
                'time': t.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'height': round(float(h), 4),
            }
            for t, h in zip(times, h_pred)
        ]

        heights = [p['height'] for p in predictions]
        stats = {
            'max': round(max(heights), 4),
            'min': round(min(heights), 4),
            'mean': round(float(np.mean(heights)), 4),
            'range': round(max(heights) - min(heights), 4),
        }

        return {
            'request': {
                'lon': lon,
                'lat': lat,
                'start_time': start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'end_time':   end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'interval_hours': interval_hours,
            },
            'grid': {
                'id': grid['id'],
                'lon': round(grid['lon'], 6),
                'lat': round(grid['lat'], 6),
                'distance_km': round(grid['distance_km'], 3),
            },
            'predictions': predictions,
            'statistics': stats,
            'metadata': {
                'model': 'TPXO9-atlas-v5',
                'method': 'Harmonic Analysis (Schureman 1958, Foreman 1977)',
                'datum': 'MSL (Mean Sea Level)',
                'timezone': 'UTC',
                'constituents': CONS_LIST,
                'n_constituents': len(CONS_LIST),
            },
        }

    @staticmethod
    def _haversine(lon1, lat1, lon2, lat2) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat/2)**2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon/2)**2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()