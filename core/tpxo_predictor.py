"""TPXO10 harmonic tide predictor.

Implements the standard tidal harmonic summation formula:

    h(t) = Σ_k  f_k(t) · A_k · cos[ ω_k·(t−t₀) + V₀_k(t₀) + u_k(t) − κ_k ]

where
    ω_k   = angular speed of constituent k (°/hour)
    A_k   = amplitude (m) from the TPXO10 database
    κ_k   = Greenwich phase lag (°) from the TPXO10 database
    f_k   = nodal amplitude correction factor
    V₀_k  = astronomical equilibrium argument at reference epoch t₀
    u_k   = nodal phase correction (°)

Angular speed fundamentals (Schureman 1958, Table 2, degrees/hour):
    T  = 15.0       (Earth rotation relative to Sun)
    s  = 0.5490165  (mean Moon longitude)
    h  = 0.0410686  (mean Sun longitude)
    p  = 0.0046418  (Moon's perigee longitude)

References:
    Schureman, P. (1958). Manual of Harmonic Analysis and Prediction of Tides.
        USC&GS Special Publication No. 98.
    Foreman, M.G.G. (1977). Manual for Tidal Heights Analysis and Prediction.
        IOS Manuscript Report 77-10.
    Egbert, G.D. & Erofeeva, S.Y. (2002). Efficient Inverse Modeling of
        Barotropic Ocean Tides. J. Atmos. Oceanic Technol., 19, 183–204.
"""

import sqlite3
import math
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_s = 0.5490165
_h = 0.0410686
_p = 0.0046418
_T = 15.0

SPEED: Dict[str, float] = {
    "m2":   2*_T - 2*_s + 2*_h,
    "s2":   2*_T,
    "n2":   2*_T - 3*_s + 2*_h + _p,
    "k2":   2*_T + 2*_h,
    "2n2":  2*_T - 4*_s + 2*_h + 2*_p,
    "nu2":  2*_T - 3*_s + 4*_h - _p,
    "mu2":  2*_T - 4*_s + 4*_h,
    "l2":   2*_T - _s + 2*_h - _p,
    "t2":   2*_T - _h + _p,
    "k1":   _T + _h,
    "o1":   _T - 2*_s + _h,
    "p1":   _T - _h,
    "q1":   _T - 3*_s + _h + _p,
    "j1":   _T + _s + _h - _p,
    "oo1":  _T + 2*_s + _h,
    "m1":   _T - _s + _h,
    "mf":   2*_s,
    "mm":   _s - _p,
    "ssa":  2*_h,
    "sa":   _h,
    "m4":   4*_T - 4*_s + 4*_h,
    "mn4":  4*_T - 5*_s + 4*_h + _p,
    "ms4":  4*_T - 2*_s + 4*_h,
    "m6":   6*_T - 6*_s + 6*_h,
    "2ms6": 6*_T - 4*_s + 6*_h,
    "2sm2": 2*_T + 2*_s - 2*_h,
    "s1":   _T,
}

# 15 constituents used in Searibu (same set as TPXO9 for compatibility)
TPXO_CONS: List[str] = [
    "2n2", "k1", "k2", "m2", "m4", "mf", "mm",
    "mn4", "ms4", "n2", "o1", "p1", "q1", "s1", "s2",
]

# Backward-compatible alias
TPXO9_CONS = TPXO_CONS

_JD_J1900 = 2415020.0
MAX_POINTS_PER_REQUEST = 527_040


def julian_day(dt: datetime) -> float:
    """Return the Julian Day Number for a UTC datetime (Meeus 1991, §7)."""
    y, m, d = dt.year, dt.month, dt.day
    frac = (dt.hour + dt.minute / 60.0 + dt.second / 3600.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + A // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5 + frac


def hours_since_j1900(dt: datetime) -> float:
    return (julian_day(dt) - _JD_J1900) * 24.0


def astronomical_args(dt: datetime) -> Dict[str, float]:
    """Compute mean astronomical longitudes at dt (Schureman 1958, Table 1)."""
    T = (julian_day(dt) - _JD_J1900) / 36525.0
    return {
        "s":  (277.0247 + 481267.8906 * T) % 360.0,
        "h":  (280.1895 +  36000.7689 * T) % 360.0,
        "p":  (334.3853 +   4069.0340 * T) % 360.0,
        "N":  (259.1561 -   1934.1423 * T) % 360.0,
        "p1": (281.2209 +      1.7192 * T) % 360.0,
    }


def equilibrium_arguments(astro: Dict[str, float]) -> Dict[str, float]:
    """Compute V₀ equilibrium arguments (Schureman 1958)."""
    s, h, p, N, p1 = astro["s"], astro["h"], astro["p"], astro["N"], astro["p1"]
    V0 = {
        "m2":   (2*h - 2*s)             % 360.0,
        "s2":   0.0,
        "n2":   (2*h - 3*s + p)         % 360.0,
        "k2":   (2*h)                   % 360.0,
        "2n2":  (2*h - 4*s + 2*p)       % 360.0,
        "nu2":  (4*h - 3*s - p)         % 360.0,
        "mu2":  (4*h - 4*s)             % 360.0,
        "l2":   (2*h - s - p + 180.0)   % 360.0,
        "t2":   (2*h - p1)              % 360.0,
        "k1":   (h + 90.0)              % 360.0,
        "o1":   (h - 2*s - 90.0)        % 360.0,
        "p1":   (-h + 90.0)             % 360.0,
        "q1":   (h - 3*s + p - 90.0)    % 360.0,
        "j1":   (s + h - p + 90.0)      % 360.0,
        "oo1":  (2*s + h + 90.0)        % 360.0,
        "m1":   (h - s + 90.0)          % 360.0,
        "mf":   (2*s)                   % 360.0,
        "mm":   (s - p)                 % 360.0,
        "ssa":  (2*h)                   % 360.0,
        "sa":   h                       % 360.0,
        "s1":   0.0,
    }
    V0["m4"]   = (2 * V0["m2"]) % 360.0
    V0["mn4"]  = (V0["m2"] + V0["n2"]) % 360.0
    V0["ms4"]  = (V0["m2"] + V0["s2"]) % 360.0
    V0["m6"]   = (3 * V0["m2"]) % 360.0
    V0["2ms6"] = (2 * V0["m2"] + V0["s2"]) % 360.0
    V0["2sm2"] = (2 * V0["s2"] - V0["m2"]) % 360.0
    return V0


def nodal_corrections(N_deg: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Compute nodal amplitude (f) and phase (u) corrections (Schureman 1958 / Foreman 1977)."""
    Nr  = math.radians(N_deg)
    N2r = math.radians(2.0 * N_deg)

    def _atan2d(y, x):
        return math.degrees(math.atan2(y, x))

    fM2_x = 1.0 - 0.03731 * math.cos(Nr) + 0.00052 * math.cos(N2r)
    fM2_y = 0.03731 * math.sin(Nr) - 0.00052 * math.sin(N2r)
    f_M2  = math.hypot(fM2_x, fM2_y)
    u_M2  = _atan2d(-fM2_y, fM2_x)

    fK2_x = 1.0 + 0.2852 * math.cos(Nr) + 0.0324 * math.cos(N2r)
    fK2_y = 0.3108 * math.sin(Nr) + 0.0328 * math.sin(N2r)
    f_K2  = math.hypot(fK2_x, fK2_y)
    u_K2  = _atan2d(-fK2_y, fK2_x)

    fK1_x = 1.0 + 0.1158 * math.cos(Nr) - 0.0029 * math.cos(N2r)
    fK1_y = 0.1554 * math.sin(Nr) - 0.0029 * math.sin(N2r)
    f_K1  = math.hypot(fK1_x, fK1_y)
    u_K1  = _atan2d(-fK1_y, fK1_x)

    fO1_x = 1.0 - 0.10980 * math.cos(Nr) + 0.00148 * math.cos(N2r)
    fO1_y = 0.10980 * math.sin(Nr) - 0.00148 * math.sin(N2r)
    f_O1  = math.hypot(fO1_x, fO1_y)
    u_O1  = _atan2d(-fO1_y, fO1_x)

    fMf_x = 1.0 - 0.15636 * math.cos(Nr)
    fMf_y = 0.15636 * math.sin(Nr)
    f_Mf  = math.hypot(fMf_x, fMf_y)
    u_Mf  = _atan2d(-fMf_y, fMf_x)

    f_Mm = 1.0 - 0.13023 * math.cos(Nr)

    f: Dict[str, float] = {
        "m2": f_M2, "s2": 1.0, "n2": f_M2, "k2": f_K2,
        "2n2": f_M2, "nu2": f_M2, "mu2": f_M2, "l2": f_M2, "t2": 1.0,
        "k1": f_K1, "o1": f_O1, "p1": 1.0, "q1": f_O1,
        "j1": f_K1, "oo1": f_K1, "m1": f_O1,
        "mf": f_Mf, "mm": f_Mm, "ssa": 1.0, "sa": 1.0,
        "m4": f_M2**2, "mn4": f_M2**2, "ms4": f_M2,
        "m6": f_M2**3, "2ms6": f_M2**2, "2sm2": f_M2, "s1": 1.0,
    }
    u: Dict[str, float] = {
        "m2": u_M2, "s2": 0.0, "n2": u_M2, "k2": u_K2,
        "2n2": u_M2, "nu2": u_M2, "mu2": u_M2, "l2": u_M2, "t2": 0.0,
        "k1": u_K1, "o1": u_O1, "p1": 0.0, "q1": u_O1,
        "j1": u_K1, "oo1": u_K1, "m1": u_O1,
        "mf": u_Mf, "mm": 0.0, "ssa": 0.0, "sa": 0.0,
        "m4": 2.0 * u_M2, "mn4": 2.0 * u_M2, "ms4": u_M2,
        "m6": 3.0 * u_M2, "2ms6": 2.0 * u_M2, "2sm2": u_M2, "s1": 0.0,
    }
    return f, u


def predict_harmonic(
    t_rel_hours: np.ndarray,
    amp: Dict[str, float],
    kappa: Dict[str, float],
    V0: Dict[str, float],
    f_dict: Dict[str, float],
    u_dict: Dict[str, float],
) -> np.ndarray:
    """Vectorised harmonic summation over TPXO_CONS constituents."""
    h = np.zeros(len(t_rel_hours), dtype=np.float64)
    for name in TPXO_CONS:
        A     = amp.get(name, 0.0)
        k     = kappa.get(name, 0.0)
        omega = SPEED.get(name)
        if A < 1e-7 or math.isnan(A) or math.isnan(k) or omega is None:
            continue
        arg_deg = omega * t_rel_hours + (V0.get(name, 0.0) + u_dict.get(name, 0.0) - k)
        h += f_dict.get(name, 1.0) * A * np.cos(np.deg2rad(arg_deg))
    return h


class TPXOPredictor:
    """Tide predictor backed by the TPXO10-atlas-v2 SQLite database.

    The SQLite database is generated by scripts/preprocess_tpxo10.py from the
    TPXO10-atlas-v2 per-constituent NetCDF files.

    Usage:
        predictor = TPXOPredictor("data/tpxo_seribu.db")
        predictor.connect()
        result = predictor.predict(lon=106.58, lat=-5.60, start_dt=..., end_dt=...)
        predictor.close()
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def find_nearest_grid(self, lon: float, lat: float) -> Dict:
        cur  = self.conn.cursor()
        cur.execute("SELECT id, lon, lat FROM grid_points")
        rows = cur.fetchall()
        if not rows:
            raise ValueError("No grid points in database")

        best, best_d = None, float("inf")
        for row in rows:
            d = self._haversine(lon, lat, row["lon"], row["lat"])
            if d < best_d:
                best_d = d
                best   = dict(row)
                best["distance_km"] = d
        return best

    def get_harmonics(self, grid_point_id: int) -> Dict[str, Dict[str, float]]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT cm.name, hc.amplitude, hc.phase
            FROM harmonic_constants hc
            JOIN constituents_metadata cm ON hc.constituent_id = cm.id
            WHERE hc.grid_point_id = ?
            """,
            (grid_point_id,),
        )
        result: Dict[str, Dict[str, float]] = {}
        for row in cur.fetchall():
            result[row["name"].lower()] = {
                "amplitude": float(row["amplitude"]),
                "phase":     float(row["phase"]),
            }
        return result

    def predict(
        self,
        lon: float,
        lat: float,
        start_dt: datetime,
        end_dt: datetime,
        interval_hours: int = 1,
        interval_minutes: Optional[int] = None,
    ) -> Dict:
        """Predict tidal heights for the given location and time window.

        Args:
            lon, lat:         target coordinates (WGS-84).
            start_dt, end_dt: prediction window (UTC).
            interval_hours:   output interval in hours (1, 3, or 6).
            interval_minutes: overrides interval_hours when set (1–60).

        Returns:
            dict with keys: request, grid, predictions, statistics, metadata.
        """
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        if end_dt <= start_dt:
            raise ValueError("end_dt must be after start_dt")

        if interval_minutes is not None:
            if not (1 <= interval_minutes <= 60):
                raise ValueError("interval_minutes must be 1–60")
            dt_hours = interval_minutes / 60.0
        else:
            if interval_hours not in (1, 3, 6):
                raise ValueError("interval_hours must be 1, 3, or 6")
            dt_hours = float(interval_hours)

        total_h = (end_dt - start_dt).total_seconds() / 3600.0
        n_steps = int(round(total_h / dt_hours)) + 1
        if n_steps > MAX_POINTS_PER_REQUEST:
            raise ValueError(
                f"Too many prediction points ({n_steps:,}); maximum is {MAX_POINTS_PER_REQUEST:,}"
            )

        grid      = self.find_nearest_grid(lon, lat)
        harmonics = self.get_harmonics(grid["id"])
        if not harmonics:
            raise ValueError(f"No harmonic data for grid point {grid['id']}")

        amp   = {n: harmonics.get(n, {}).get("amplitude", 0.0) for n in TPXO_CONS}
        kappa = {n: harmonics.get(n, {}).get("phase",     0.0) for n in TPXO_CONS}

        astro_t0 = astronomical_args(start_dt.replace(tzinfo=None))
        V0       = equilibrium_arguments(astro_t0)

        t_mid  = (start_dt + timedelta(hours=total_h / 2.0)).replace(tzinfo=None)
        f_dict, u_dict = nodal_corrections(astronomical_args(t_mid)["N"])

        t_rel  = np.arange(n_steps, dtype=np.float64) * dt_hours
        h_pred = predict_harmonic(t_rel, amp, kappa, V0, f_dict, u_dict)

        predictions = [
            {
                "time":   (start_dt + timedelta(hours=float(t_rel[i]))).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "height": round(float(h_pred[i]), 4),
            }
            for i in range(n_steps)
        ]

        return {
            "request": {
                "lon": lon, "lat": lat,
                "start_time":       start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_time":         end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval_hours":   dt_hours,
                "interval_minutes": interval_minutes,
                "n_points":         n_steps,
            },
            "grid": {
                "id":          grid["id"],
                "lon":         round(grid["lon"], 6),
                "lat":         round(grid["lat"], 6),
                "distance_km": round(grid["distance_km"], 3),
            },
            "predictions": predictions,
            "statistics": {
                "max":   round(float(np.max(h_pred)),  4),
                "min":   round(float(np.min(h_pred)),  4),
                "mean":  round(float(np.mean(h_pred)), 4),
                "range": round(float(np.max(h_pred) - np.min(h_pred)), 4),
            },
            "metadata": {
                "model":            "TPXO10-atlas-v2",
                "method":           "Harmonic Analysis — Schureman (1958) / OTIS formulation",
                "datum":            "MSL (Mean Sea Level)",
                "timezone":         "UTC",
                "constituents":     TPXO_CONS,
                "n_constituents":   len(TPXO_CONS),
                "nodal_epoch":      t_mid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reference_epoch":  "J1900.0 (JD 2415020.0)",
            },
        }

    @staticmethod
    def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        R    = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a    = (math.sin(dlat / 2) ** 2
                + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
                * math.sin(dlon / 2) ** 2)
        return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))