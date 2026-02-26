"""
Luwes Preprocessing — Spike removal & smoothing untuk data water level.

Pipeline:
  1. Hampel Filter  → deteksi & hapus spike (ganti dengan NaN)
  2. Linear interpolasi → isi NaN hasil Hampel
  3. Savitzky-Golay Filter → smoothing tanpa scipy (numpy murni)

Semua fungsi stateless dan pure — tidak ada side effect ke DB.
DB write dilakukan di luwes_scheduler.py setelah preprocessing selesai.

Referensi:
  Hampel (2001) — Robust Statistics, Wiley
  Savitzky & Golay (1964) — Analytical Chemistry, 36(8)
"""

import numpy as np
from typing import List, Dict, Tuple
import copy

# ── Parameter default ─────────────────────────────────────────
HAMPEL_WINDOW   = 7     # setengah-window Hampel (total window = 2k+1 = 15 data)
HAMPEL_N_SIGMA  = 3.0   # ambang batas: n × MAD
SG_WINDOW       = 15    # window Savitzky-Golay (harus ganjil, ≥ polyorder+1)
SG_POLYORDER    = 2     # orde polinomial SG


# ══════════════════════════════════════════════════════════════
# HAMPEL FILTER
# ══════════════════════════════════════════════════════════════

def hampel_filter(
    values: np.ndarray,
    k: int = HAMPEL_WINDOW,
    n_sigma: float = HAMPEL_N_SIGMA,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Hampel filter untuk deteksi outlier berbasis MAD (Median Absolute Deviation).

    Untuk setiap titik i, hitung:
      median_i = median(x[i-k : i+k+1])
      MAD_i    = median(|x[i-k : i+k+1] - median_i|)
      threshold = n_sigma × 1.4826 × MAD_i
      jika |x[i] - median_i| > threshold → x[i] dianggap spike

    Args:
        values   : array 1D level air (float)
        k        : setengah-lebar window (total = 2k+1)
        n_sigma  : faktor pengali threshold

    Returns:
        filtered : array dengan spike diganti NaN
        is_spike : boolean array, True = spike
    """
    n = len(values)
    filtered  = values.copy().astype(float)
    is_spike  = np.zeros(n, dtype=bool)

    # Konstanta konsistensi untuk distribusi normal
    K_MAD = 1.4826

    for i in range(n):
        lo = max(0, i - k)
        hi = min(n, i + k + 1)
        window = values[lo:hi]

        # Lewati jika window terlalu kecil atau semua NaN
        valid = window[~np.isnan(window)]
        if len(valid) < 3:
            continue

        med = np.median(valid)
        mad = np.median(np.abs(valid - med))
        threshold = n_sigma * K_MAD * mad

        if mad > 0 and abs(values[i] - med) > threshold:
            filtered[i] = np.nan
            is_spike[i] = True

    return filtered, is_spike


# ══════════════════════════════════════════════════════════════
# INTERPOLASI LINEAR
# ══════════════════════════════════════════════════════════════

def interpolate_nans(values: np.ndarray) -> np.ndarray:
    """
    Interpolasi linear untuk mengisi NaN.
    NaN di ujung array diisi dengan nilai valid terdekat (extrapolate=False → fill edge).
    """
    result = values.copy()
    nans   = np.isnan(result)

    if not nans.any():
        return result

    # Indeks posisi
    x_all   = np.arange(len(result))
    x_valid = x_all[~nans]
    y_valid = result[~nans]

    if len(x_valid) == 0:
        return result  # semua NaN, tidak bisa interpolasi

    # Interpolasi di dalam range valid
    result = np.interp(x_all, x_valid, y_valid)
    return result


# ══════════════════════════════════════════════════════════════
# SAVITZKY-GOLAY FILTER (pure numpy)
# ══════════════════════════════════════════════════════════════

def _sg_coefficients(window: int, polyorder: int) -> np.ndarray:
    """
    Hitung koefisien Savitzky-Golay via least-squares polynomial fitting.
    window harus ganjil. polyorder < window.
    """
    if window % 2 == 0:
        raise ValueError("SG window harus ganjil")
    if polyorder >= window:
        raise ValueError("polyorder harus < window")

    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)

    # Vandermonde matrix
    A = np.vander(x, polyorder + 1, increasing=True)

    # Koefisien = (A^T A)^-1 A^T, baris 0 (nilai smoothed, bukan derivative)
    ATA_inv = np.linalg.pinv(A.T @ A)
    coeffs  = (ATA_inv @ A.T)[0]   # shape: (window,)
    return coeffs


def savitzky_golay(
    values: np.ndarray,
    window: int = SG_WINDOW,
    polyorder: int = SG_POLYORDER,
) -> np.ndarray:
    """
    Savitzky-Golay smoothing filter (numpy murni, tanpa scipy).

    Di ujung array (mode='nearest'), nilai diisi dengan edge value
    sehingga output selalu sepanjang input.

    Args:
        values    : array 1D (float, tanpa NaN — jalankan interpolate_nans dulu)
        window    : lebar window (ganjil)
        polyorder : orde polinomial

    Returns:
        smoothed : array 1D hasil smoothing
    """
    if window % 2 == 0:
        window += 1   # pastikan ganjil

    # Jika data lebih pendek dari window, kurangi window secara adaptif
    n = len(values)
    while window >= n and window > polyorder + 2:
        window -= 2

    if window <= polyorder:
        return values.copy()   # data terlalu sedikit, skip smoothing

    coeffs = _sg_coefficients(window, polyorder)
    half   = window // 2

    # Pad dengan mode 'edge' (nilai ujung diulang)
    padded  = np.pad(values, half, mode='edge')
    smoothed = np.convolve(padded, coeffs[::-1], mode='valid')

    # Trim ke panjang asli
    return smoothed[:n]


# ══════════════════════════════════════════════════════════════
# PIPELINE UTAMA
# ══════════════════════════════════════════════════════════════

def preprocess(
    records: List[Dict],
    hampel_k: int     = HAMPEL_WINDOW,
    hampel_sigma: float = HAMPEL_N_SIGMA,
    sg_window: int    = SG_WINDOW,
    sg_polyorder: int = SG_POLYORDER,
) -> Tuple[List[Dict], Dict]:
    """
    Jalankan pipeline preprocessing lengkap pada list records.

    Args:
        records       : list dict dari DB raw, sudah diurutkan ascending by recorded_at
                        Setiap dict harus punya field: level_m, recorded_at, rec, dst.
        hampel_k      : setengah-window Hampel
        hampel_sigma  : threshold sigma Hampel
        sg_window     : window Savitzky-Golay (ganjil)
        sg_polyorder  : orde polinomial SG

    Returns:
        processed_records : list dict — record yang BUKAN spike, dengan level_m
                            sudah di-smooth. rec dan metadata lain dipertahankan.
        stats             : dict ringkasan {n_input, n_spikes, n_output}
    """
    if not records:
        return [], {"n_input": 0, "n_spikes": 0, "n_output": 0}

    n_input = len(records)

    # Ekstrak array level
    raw_levels = np.array(
        [r["level_m"] if r["level_m"] is not None else np.nan for r in records],
        dtype=float,
    )

    # ── Step 1: Hampel filter ────────────────────────────────
    filtered, is_spike = hampel_filter(raw_levels, k=hampel_k, n_sigma=hampel_sigma)
    n_spikes = int(is_spike.sum())

    # ── Step 2: Interpolasi NaN (spike yang sudah jadi NaN) ──
    interpolated = interpolate_nans(filtered)

    # ── Step 3: Savitzky-Golay smoothing ────────────────────
    smoothed = savitzky_golay(interpolated, window=sg_window, polyorder=sg_polyorder)

    # ── Step 4: Susun kembali records — buang record spike ──
    # Kita HAPUS record yang terdeteksi spike (bukan ganti nilainya),
    # lalu nilai yang tersisa di-smooth
    processed_records = []
    smooth_idx = 0
    non_spike_indices = np.where(~is_spike)[0]

    # Buat mapping: indeks non-spike → smoothed value
    # Smoothed dihitung dari seluruh array (termasuk interpolasi di posisi spike),
    # tapi kita hanya keep record yang bukan spike
    for orig_idx in non_spike_indices:
        rec = copy.deepcopy(records[orig_idx])
        rec["level_m"] = round(float(smoothed[orig_idx]), 4)
        processed_records.append(rec)

    stats = {
        "n_input":  n_input,
        "n_spikes": n_spikes,
        "n_output": len(processed_records),
    }

    return processed_records, stats
