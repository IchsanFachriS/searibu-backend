"""
core/tpxo_predictor.py — TPXO Tide Predictor
Harmonic formulation strictly following OTIS / Schureman (1958)

Formula
-------
  h(t) = Σ_k  f_k(t) · A_k · cos[ ω_k · (t − t₀)  +  V₀_k(t₀)  +  u_k(t)  −  κ_k ]

where
  t      = time (hours from any fixed origin; we use J1900 = JD 2415020.0)
  t₀     = start of prediction window (same origin as t)
  ω_k    = angular speed of constituent k  (°/hour, from Schureman Table 2)
  A_k    = amplitude  (m)  stored in the TPXO9 SQLite database
  κ_k    = Greenwich phase lag  (°) stored in the TPXO9 SQLite database
  f_k    = time-varying amplitude correction (nodal factor)
  V₀_k   = astronomical equilibrium argument at t₀  (°)
  u_k    = nodal phase correction at t (evaluated at mid-interval)

Angular speeds  ω_k  (Doodson, Schureman Table 2; degrees per mean solar hour)
  s  = 0.5490165   °/h  (mean Moon)
  h  = 0.0410686   °/h  (mean Sun)
  p  = 0.0046418   °/h  (Moon's perigee)
  N' = 0.00220641  °/h  (ascending node, negative because regression)

  ω = n₁·T + n₂·s + n₃·h + n₄·p + n₅·N'  (Doodson numbers n₁…n₅)
  T = 15.0°/h  (mean solar time, rate of Earth rotation relative to Sun)

V₀  (Schureman equilibrium arguments at epoch t₀, in terms of s, h, p, N)
    Uses the standard Schureman (1958) Table 1 expressions evaluated by
    direct computation of the lunar/solar mean longitudes from JD.

Nodal corrections  f, u
    From Schureman (1958) Tables 14 and 15 / Foreman (1977) Appendix.
    Evaluated at the mid-point of the prediction window.

References
----------
  Schureman, P. (1958). Manual of Harmonic Analysis and Prediction of Tides.
      USC&GS Special Publication No. 98. US Gov. Printing Office.
  Foreman, M.G.G. (1977). Manual for Tidal Heights Analysis and Prediction.
      IOS Manuscript Report 77-10.
  Egbert, G.D. & Erofeeva, S.Y. (2002). Efficient Inverse Modeling of
      Barotropic Ocean Tides. J. Atmos. Oceanic Technol., 19, 183–204.
  OTIS source code (Oregon State University, 2019).
"""

import sqlite3
import math
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════
# 1. CONSTITUENT CATALOGUE
#    Doodson numbers and angular speeds from Schureman (1958) Table 2.
#    ω = n₁·15 + n₂·s + n₃·h + n₄·p   (°/h, ignoring N' in speed only)
#    N' appears only in the nodal correction u, not in the mean speed.
# ═══════════════════════════════════════════════════════════════════════════

# Fundamental angular speeds (degrees/hour), Schureman §10
_s  = 0.5490165   # mean Moon longitude rate
_h  = 0.0410686   # mean Sun longitude rate
_p  = 0.0046418   # Moon's perigee longitude rate
_T  = 15.0        # mean solar time rate (Earth rotation w.r.t. Sun)

# Constituent angular speeds  ω  (°/hour)
# Each entry: exact Schureman speed in degrees/hour
SPEED: Dict[str, float] = {
    # ── semidiurnal ──────────────────────────────────────────
    "m2":  2*_T - 2*_s + 2*_h,           # 28.984104
    "s2":  2*_T,                           # 30.000000
    "n2":  2*_T - 3*_s + 2*_h + _p,       # 28.439730
    "k2":  2*_T + 2*_h,                   # 30.082138
    "2n2": 2*_T - 4*_s + 2*_h + 2*_p,    # 27.895355
    "nu2": 2*_T - 3*_s + 4*_h - _p,      # 28.512583  (ν₂)
    "mu2": 2*_T - 4*_s + 4*_h,           # 27.968208  (μ₂)
    "l2":  2*_T - _s + 2*_h - _p,        # 29.528479  (λ₂)
    "t2":  2*_T - _h + _p,               # 29.958933
    # ── diurnal ──────────────────────────────────────────────
    "k1":  _T + _h,                        # 15.041069
    "o1":  _T - 2*_s + _h,                # 13.943036
    "p1":  _T - _h,                        # 14.958931
    "q1":  _T - 3*_s + _h + _p,           # 13.398661
    "j1":  _T + _s + _h - _p,             # 15.585443
    "oo1": _T + 2*_s + _h,                # 16.139102
    "m1":  _T - _s + _h,                  # 14.496694
    # ── long period ──────────────────────────────────────────
    "mf":  2*_s,                           #  1.098033
    "mm":  _s - _p,                        #  0.544375
    "ssa": 2*_h,                           #  0.082137
    "sa":  _h,                             #  0.041069
    # ── shallow water / overtides ────────────────────────────
    "m4":  4*_T - 4*_s + 4*_h,            # 57.968208
    "mn4": 4*_T - 5*_s + 4*_h + _p,       # 57.423832
    "ms4": 4*_T - 2*_s + 4*_h,            # 58.984104
    "m6":  6*_T - 6*_s + 6*_h,            # 86.952313
    "2ms6":6*_T - 4*_s + 6*_h,            # 87.968208
    "2sm2":2*_T + 2*_s - 2*_h,            # 31.015896
    "s1":  _T,                             # 15.000000
}

# The 15 constituents present in TPXO9-atlas-v5 (Egbert & Erofeeva 2002)
TPXO9_CONS: List[str] = [
    "2n2", "k1", "k2", "m2", "m4", "mf", "mm",
    "mn4", "ms4", "n2", "o1", "p1", "q1", "s1", "s2",
]


# ═══════════════════════════════════════════════════════════════════════════
# 2. JULIAN DATE  (Meeus 1991, Chapter 7)
# ═══════════════════════════════════════════════════════════════════════════

def julian_day(dt: datetime) -> float:
    """
    Julian Day Number for a UTC datetime.
    Uses the proleptic Gregorian calendar algorithm of Meeus (1991) §7.
    """
    y, m, d = dt.year, dt.month, dt.day
    frac = (dt.hour + dt.minute / 60.0 + dt.second / 3600.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + A // 4
    jd = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5
    return jd + frac


# Reference epoch J1900.0 = JD 2415020.0
_JD_J1900 = 2415020.0


def hours_since_j1900(dt: datetime) -> float:
    """Elapsed hours between J1900.0 and *dt* (UTC)."""
    return (julian_day(dt) - _JD_J1900) * 24.0


# ═══════════════════════════════════════════════════════════════════════════
# 3. ASTRONOMICAL ARGUMENTS  (Schureman 1958, Table 1)
#    Evaluated from Julian centuries T₁₀₀ = (JD − 2415020) / 36525
#
#    All angles in degrees, reduced modulo 360.
# ═══════════════════════════════════════════════════════════════════════════

def astronomical_args(dt: datetime) -> Dict[str, float]:
    """
    Compute mean astronomical longitudes at *dt* (UTC).

    Returns a dict with keys:
        s   – mean longitude of Moon
        h   – mean longitude of Sun  (= mean longitude of Earth's perihelion + mean anomaly)
        p   – mean longitude of Moon's perigee
        N   – mean longitude of Moon's ascending node  (retrograde → subtract)
        p1  – mean longitude of Sun's perigee (almost constant ~281°)

    Formulas: Schureman (1958) Table 1, using Julian centuries from J1900.
    """
    T = (julian_day(dt) - _JD_J1900) / 36525.0  # Julian centuries from J1900

    s  = (277.0247 + 481267.8906 * T) % 360.0
    h  = (280.1895 +  36000.7689 * T) % 360.0
    p  = (334.3853 +   4069.0340 * T) % 360.0
    N  = (259.1561 -   1934.1423 * T) % 360.0   # node is retrograde
    p1 = (281.2209 +      1.7192 * T) % 360.0   # Sun's perigee

    return {"s": s, "h": h, "p": p, "N": N, "p1": p1}


# ═══════════════════════════════════════════════════════════════════════════
# 4. EQUILIBRIUM ARGUMENTS  V₀  (Schureman 1958, Table 2 / eq. 13)
#
#    V₀ is the equilibrium argument at the chosen reference epoch t₀.
#    It accounts for the phase of each constituent in the equilibrium tide.
#    Convention: same as OTIS — phase measured from the Greenwich meridian.
#
#    NOTE: The phase stored in TPXO9 is the Greenwich phase lag κ
#    (Schureman's notation).  The prediction formula is therefore:
#
#      h(t) = f · A · cos( ω·(t−t₀) + V₀(t₀) + u(t) − κ )
#
#    which is equivalent to the complex notation used in OTIS.
# ═══════════════════════════════════════════════════════════════════════════

def equilibrium_arguments(astro: Dict[str, float]) -> Dict[str, float]:
    """
    Compute V₀ for each constituent at the epoch whose astronomical
    arguments are given in *astro*.

    These are the standard Schureman (1958) expressions.
    All angles in degrees.
    """
    s  = astro["s"]
    h  = astro["h"]
    p  = astro["p"]
    N  = astro["N"]
    p1 = astro["p1"]

    V0: Dict[str, float] = {}

    # ── semidiurnal ──────────────────────────────────────────────────────
    # M2: 2(T + h − s) = 2h − 2s  (T=0 at Greenwich noon → +90° for midnight?
    #     OTIS/Schureman use the mean solar angle T = 0 at the epoch; the
    #     "clock" term 2·T is absorbed into ω·t, so V₀ carries only the
    #     slowly-varying part.)
    V0["m2"]  = (2*h - 2*s) % 360.0
    V0["s2"]  = 0.0                            # always 0 (≡ 2T − 2T)
    V0["n2"]  = (2*h - 3*s + p) % 360.0
    V0["k2"]  = (2*h) % 360.0
    V0["2n2"] = (2*h - 4*s + 2*p) % 360.0
    V0["nu2"] = (4*h - 3*s - p) % 360.0
    V0["mu2"] = (4*h - 4*s) % 360.0
    V0["l2"]  = (2*h - s - p + 180.0) % 360.0   # +π from Schureman
    V0["t2"]  = (2*h - p1) % 360.0
    # ── diurnal ──────────────────────────────────────────────────────────
    V0["k1"]  = (h + 90.0) % 360.0              # +90° = +π/2 (Schureman eq.)
    V0["o1"]  = (h - 2*s - 90.0) % 360.0
    V0["p1"]  = (-h + 90.0) % 360.0
    V0["q1"]  = (h - 3*s + p - 90.0) % 360.0
    V0["j1"]  = (s + h - p + 90.0) % 360.0
    V0["oo1"] = (2*s + h + 90.0) % 360.0
    V0["m1"]  = (h - s + 90.0) % 360.0          # simplified; see Schureman §152
    # ── long period ──────────────────────────────────────────────────────
    V0["mf"]  = (2*s) % 360.0
    V0["mm"]  = (s - p) % 360.0
    V0["ssa"] = (2*h) % 360.0
    V0["sa"]  = h % 360.0
    # ── shallow water: V₀ constructed from fundamental constituents ──────
    V0["m4"]  = (2 * V0["m2"]) % 360.0
    V0["mn4"] = (V0["m2"] + V0["n2"]) % 360.0
    V0["ms4"] = (V0["m2"] + V0["s2"]) % 360.0
    V0["m6"]  = (3 * V0["m2"]) % 360.0
    V0["2ms6"]= (2 * V0["m2"] + V0["s2"]) % 360.0
    V0["2sm2"]= (2 * V0["s2"] - V0["m2"]) % 360.0
    V0["s1"]  = 0.0

    return V0


# ═══════════════════════════════════════════════════════════════════════════
# 5. NODAL CORRECTIONS  f, u  (Schureman 1958, Tables 14–15; Foreman 1977)
#
#    f  – amplitude correction factor  (dimensionless, ~1)
#    u  – phase correction  (degrees)
#
#    Both are evaluated at the mid-point of the prediction window so
#    they are treated as constant over the window (standard OTIS practice
#    for windows ≤ a few months).
# ═══════════════════════════════════════════════════════════════════════════

def nodal_corrections(N_deg: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Compute nodal amplitude (f) and phase (u) corrections.

    Parameters
    ----------
    N_deg : longitude of Moon's ascending node in degrees.

    Returns
    -------
    f : dict, amplitude correction factors
    u : dict, phase corrections in degrees
    """
    Nr   = math.radians(N_deg)
    N2r  = math.radians(2.0 * N_deg)

    # ── helper: convert (x, y) → angle in degrees ────────────────────────
    def _atan2d(y: float, x: float) -> float:
        return math.degrees(math.atan2(y, x))

    # ── M2  (Schureman Table 14, row M₂) ──────────────────────────────────
    #   f_M2 = √( (1 − 0.03731 cos N + 0.00052 cos 2N)²
    #            + (0.03731 sin N − 0.00052 sin 2N)² )
    fM2_x = 1.0 - 0.03731*math.cos(Nr) + 0.00052*math.cos(N2r)
    fM2_y =       0.03731*math.sin(Nr) - 0.00052*math.sin(N2r)
    f_M2 = math.hypot(fM2_x, fM2_y)
    u_M2 = _atan2d(-fM2_y, fM2_x)          # sign: Schureman uses −u in arg

    # ── K2 ────────────────────────────────────────────────────────────────
    fK2_x = 1.0 + 0.2852*math.cos(Nr) + 0.0324*math.cos(N2r)
    fK2_y =       0.3108*math.sin(Nr) + 0.0328*math.sin(N2r)
    f_K2 = math.hypot(fK2_x, fK2_y)
    u_K2 = _atan2d(-fK2_y, fK2_x)

    # ── K1 ────────────────────────────────────────────────────────────────
    fK1_x = 1.0 + 0.1158*math.cos(Nr) - 0.0029*math.cos(N2r)
    fK1_y =       0.1554*math.sin(Nr) - 0.0029*math.sin(N2r)
    f_K1 = math.hypot(fK1_x, fK1_y)
    u_K1 = _atan2d(-fK1_y, fK1_x)

    # ── O1 ────────────────────────────────────────────────────────────────
    fO1_x = 1.0 - 0.10980*math.cos(Nr) + 0.00148*math.cos(N2r)
    fO1_y =       0.10980*math.sin(Nr) - 0.00148*math.sin(N2r)
    f_O1 = math.hypot(fO1_x, fO1_y)
    u_O1 = _atan2d(-fO1_y, fO1_x)           # O1 sign convention opposite to M2

    # ── Mf  (Schureman Table 14) ──────────────────────────────────────────
    fMf_x = 1.0 - 0.15636*math.cos(Nr)
    fMf_y =       0.15636*math.sin(Nr)
    f_Mf = math.hypot(fMf_x, fMf_y)
    u_Mf = _atan2d(-fMf_y, fMf_x)

    # ── Mm ────────────────────────────────────────────────────────────────
    f_Mm = 1.0 - 0.13023*math.cos(Nr)
    u_Mm = 0.0                              # no phase correction for Mm

    # ── Q1 shares nodal factor with O1 (same Doodson coefficients for N) ─
    f_Q1 = f_O1
    u_Q1 = u_O1

    # ── P1, S1, S2, T2 — no nodal correction ─────────────────────────────
    f_unity = 1.0
    u_zero  = 0.0

    # ── Compound constituents ──────────────────────────────────────────────
    f_M4   = f_M2**2
    u_M4   = 2.0 * u_M2
    f_MN4  = f_M2 * f_M2                        # same as M4 for nodal factor
    u_MN4  = u_M2 +u_M2                     # approximate (N2 ≈ M2 for u)
    f_MS4  = f_M2                           # M2 × S2; S2 has f=1
    u_MS4  = u_M2

    # ── Assemble dictionaries ─────────────────────────────────────────────
    f: Dict[str, float] = {
        "m2":  f_M2,   "s2":  f_unity, "n2":  f_M2,   "k2":  f_K2,
        "2n2": f_M2,   "nu2": f_M2,    "mu2": f_M2,    "l2":  f_M2,
        "t2":  f_unity,
        "k1":  f_K1,   "o1":  f_O1,    "p1":  f_unity, "q1":  f_Q1,
        "j1":  f_K1,   "oo1": f_K1,    "m1":  f_O1,
        "mf":  f_Mf,   "mm":  f_Mm,    "ssa": f_unity, "sa":  f_unity,
        "m4":  f_M4,   "mn4": f_MN4,   "ms4": f_MS4,
        "m6":  f_M2**3, "2ms6": f_M2**2, "2sm2": f_M2,
        "s1":  f_unity,
    }

    u: Dict[str, float] = {
        "m2":  u_M2,   "s2":  u_zero,  "n2":  u_M2,   "k2":  u_K2,
        "2n2": u_M2,   "nu2": u_M2,    "mu2": u_M2,   "l2":  u_M2,
        "t2":  u_zero,
        "k1":  u_K1,   "o1":  u_O1,   "p1":  u_zero,  "q1":  u_Q1,
        "j1":  u_K1,   "oo1": u_K1,   "m1":  u_O1,
        "mf":  u_Mf,   "mm":  u_Mm,   "ssa": u_zero,  "sa":  u_zero,
        "m4":  u_M4,   "mn4": u_MN4,  "ms4": u_MS4,
        "m6":  3.0*u_M2, "2ms6": 2.0*u_M2, "2sm2": u_M2,
        "s1":  u_zero,
    }

    return f, u


# ═══════════════════════════════════════════════════════════════════════════
# 6. CORE HARMONIC PREDICTION
#
#    h(t) = Σ_k  f_k · A_k · cos( ω_k·(t − t₀) + V₀_k(t₀) + u_k − κ_k )
#
#    where  t − t₀  is in hours.
#
#    The argument is in degrees throughout; conversion to radians at the
#    cosine call.
# ═══════════════════════════════════════════════════════════════════════════

def predict_harmonic(
    t_rel_hours: np.ndarray,          # shape (N,), hours since t₀
    amp:    Dict[str, float],         # constituent amplitudes  (m)
    kappa:  Dict[str, float],         # Greenwich phase lags    (°)
    V0:     Dict[str, float],         # equilibrium arguments at t₀  (°)
    f_dict: Dict[str, float],         # nodal amplitude corrections
    u_dict: Dict[str, float],         # nodal phase corrections  (°)
) -> np.ndarray:
    """
    Vectorised harmonic summation.  Returns sea level in metres.
    """
    h = np.zeros(len(t_rel_hours), dtype=np.float64)

    for name in TPXO9_CONS:
        A = amp.get(name, 0.0)
        k = kappa.get(name, 0.0)
        if A < 1e-7 or math.isnan(A) or math.isnan(k):
            continue

        omega = SPEED.get(name)
        if omega is None:
            continue

        V0k = V0.get(name, 0.0)
        fk  = f_dict.get(name, 1.0)
        uk  = u_dict.get(name, 0.0)

        # argument in degrees
        arg_deg = omega * t_rel_hours + (V0k + uk - k)
        h += fk * A * np.cos(np.deg2rad(arg_deg))

    return h


# ═══════════════════════════════════════════════════════════════════════════
# 7. TPXO PREDICTOR CLASS
# ═══════════════════════════════════════════════════════════════════════════

MAX_POINTS_PER_REQUEST = 527_040   # 366 days × 1440 min


class TPXOPredictor:
    """
    Tide predictor backed by the TPXO9-atlas-v5 SQLite database.

    The database must contain tables:
        grid_points          (id, lon, lat, …)
        harmonic_constants   (grid_point_id, constituent_id, amplitude, phase)
        constituents_metadata(id, name, …)

    Usage
    -----
        predictor = TPXOPredictor("data/tpxo_seribu.db")
        predictor.connect()
        result = predictor.predict(lon=106.58, lat=-5.60,
                                   start_dt=…, end_dt=…)
        predictor.close()
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.conn: Optional[sqlite3.Connection] = None

    # ── connection ──────────────────────────────────────────────────────

    def connect(self):
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── spatial lookup ──────────────────────────────────────────────────

    def find_nearest_grid(self, lon: float, lat: float) -> Dict:
        """Return the closest grid point by Haversine distance."""
        cur = self.conn.cursor()
        cur.execute("SELECT id, lon, lat FROM grid_points")
        rows = cur.fetchall()
        if not rows:
            raise ValueError("No grid points found in database.")

        best, best_d = None, float("inf")
        for row in rows:
            d = self._haversine(lon, lat, row["lon"], row["lat"])
            if d < best_d:
                best_d = d
                best = dict(row)
                best["distance_km"] = d
        return best

    def get_harmonics(self, grid_point_id: int) -> Dict[str, Dict[str, float]]:
        """Return {name: {amplitude, phase}} for the given grid point."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT cm.name, hc.amplitude, hc.phase
            FROM harmonic_constants hc
            JOIN constituents_metadata cm ON hc.constituent_id = cm.id
            WHERE hc.grid_point_id = ?
        """, (grid_point_id,))
        result: Dict[str, Dict[str, float]] = {}
        for row in cur.fetchall():
            result[row["name"].lower()] = {
                "amplitude": float(row["amplitude"]),
                "phase":     float(row["phase"]),
            }
        return result

    # ── main prediction ─────────────────────────────────────────────────

    def predict(
        self,
        lon: float,
        lat: float,
        start_dt: datetime,
        end_dt: datetime,
        interval_hours: int = 1,
        interval_minutes: Optional[int] = None,
    ) -> Dict:
        """
        Predict tidal heights from *start_dt* to *end_dt*.

        Parameters
        ----------
        lon, lat         : target coordinates (WGS-84)
        start_dt, end_dt : prediction window (UTC, timezone-aware or naive)
        interval_hours   : output interval in hours  (1, 3, or 6)
                           — ignored if interval_minutes is given
        interval_minutes : output interval in minutes (1–60)
                           — takes priority over interval_hours

        Returns
        -------
        dict with keys: request, grid, predictions, statistics, metadata
        """
        # ── normalise timezone ──────────────────────────────────────────
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        if end_dt <= start_dt:
            raise ValueError("end_dt must be after start_dt.")

        # ── resolve interval ────────────────────────────────────────────
        if interval_minutes is not None:
            if not (1 <= interval_minutes <= 60):
                raise ValueError("interval_minutes must be 1–60.")
            dt_hours = interval_minutes / 60.0
            interval_label_hours = dt_hours
        else:
            if interval_hours not in (1, 3, 6):
                raise ValueError("interval_hours must be 1, 3, or 6.")
            dt_hours = float(interval_hours)
            interval_label_hours = float(interval_hours)

        # ── check point count ───────────────────────────────────────────
        total_h = (end_dt - start_dt).total_seconds() / 3600.0
        n_steps = int(round(total_h / dt_hours)) + 1
        if n_steps > MAX_POINTS_PER_REQUEST:
            raise ValueError(
                f"Too many prediction points ({n_steps:,}). "
                f"Maximum is {MAX_POINTS_PER_REQUEST:,}. "
                "Reduce date range or increase interval."
            )

        # ── database lookup ─────────────────────────────────────────────
        grid = self.find_nearest_grid(lon, lat)
        harmonics = self.get_harmonics(grid["id"])
        if not harmonics:
            raise ValueError(f"No harmonic data for grid point {grid['id']}.")

        amp   = {n: harmonics.get(n, {}).get("amplitude", 0.0) for n in TPXO9_CONS}
        kappa = {n: harmonics.get(n, {}).get("phase",     0.0) for n in TPXO9_CONS}

        # ── astronomical arguments ──────────────────────────────────────
        # V₀ evaluated at the START of the window (= reference epoch t₀)
        t0_naive = start_dt.replace(tzinfo=None)
        astro_t0 = astronomical_args(t0_naive)
        V0 = equilibrium_arguments(astro_t0)

        # Nodal corrections evaluated at the MID-POINT of the window
        t_mid_naive = (start_dt + timedelta(hours=total_h / 2.0)).replace(tzinfo=None)
        astro_mid = astronomical_args(t_mid_naive)
        f_dict, u_dict = nodal_corrections(astro_mid["N"])

        # ── time array ──────────────────────────────────────────────────
        # t_rel_hours : hours since start_dt  (= t − t₀)
        t_rel = np.arange(n_steps, dtype=np.float64) * dt_hours  # shape (N,)

        # ── prediction ──────────────────────────────────────────────────
        h_pred = predict_harmonic(t_rel, amp, kappa, V0, f_dict, u_dict)

        # ── format output ───────────────────────────────────────────────
        predictions = [
            {
                "time":   (start_dt + timedelta(hours=float(t_rel[i]))).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "height": round(float(h_pred[i]), 4),
            }
            for i in range(n_steps)
        ]

        heights = [p["height"] for p in predictions]
        stats = {
            "max":   round(float(np.max(h_pred)),  4),
            "min":   round(float(np.min(h_pred)),  4),
            "mean":  round(float(np.mean(h_pred)), 4),
            "range": round(float(np.max(h_pred) - np.min(h_pred)), 4),
        }

        return {
            "request": {
                "lon": lon,
                "lat": lat,
                "start_time":       start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_time":         end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval_hours":   interval_label_hours,
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
            "statistics":  stats,
            "metadata": {
                "model":          "TPXO9-atlas-v5",
                "method":         "Harmonic Analysis — Schureman (1958) / OTIS formulation",
                "datum":          "MSL (Mean Sea Level)",
                "timezone":       "UTC",
                "constituents":   TPXO9_CONS,
                "n_constituents": len(TPXO9_CONS),
                "nodal_epoch":    t_mid_naive.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reference_epoch":"J1900.0 (JD 2415020.0)",
            },
        }

    # ── utilities ───────────────────────────────────────────────────────

    @staticmethod
    def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        """Great-circle distance in kilometres."""
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(lat1))
             * math.cos(math.radians(lat2))
             * math.sin(dlon / 2) ** 2)
        return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))