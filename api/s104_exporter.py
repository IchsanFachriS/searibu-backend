"""
s104_exporter.py — IHO S-104 Water Level Information for Surface Navigation
Edition 2.0.0 compliant HDF5 generator for Sistem Searibu.

Referensi:
  [1] IHO S-100 Universal Hydrographic Data Model, Edition 5.2.0 (2024)
  [2] IHO S-104 Water Level Information for Surface Navigation, Edition 2.0.0 (2024)
  [3] Amanda et al. (2023) "Process Design of Bathymetric, Water Level, and
      Surface Current Data Conversion According to IHO S-100 Standard",
      ITB Capstone Design Project.

Struktur HDF5 yang dihasilkan:
  /  (root attributes — S-100 mandatory metadata)
  /Group_F/
      featureCode          : ['WaterLevel']
      WaterLevel           : feature attributes table
  /WaterLevel/             (feature container)
      attrs: dataCodingFormat, typeOfWaterLevelData / dataDynamicity, …
      /WaterLevel.01/      (feature instance)
          attrs: dateTimeOfFirstRecord, dateTimeOfLastRecord, …
          /Group_001/      (satu titik / station)
              values       : structured array [waterLevelHeight, waterLevelTrend]
              attrs: startDateTime, endDateTime, numberOfTimes, …
          /positioning/    (koordinat titik)
              geometryValues : [[lat, lon]]

Data Dynamicity (S-104 Ed.2.0.0 §8.7.1):
  1 = astronomicalPrediction   ← data TPXO
  3 = observed                 ← data Luwes stasiun
"""

import os
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

import numpy as np
import h5py

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

# ── Konstanta S-104 / S-100 ────────────────────────────────────────────────
PRODUCT_SPEC       = "INT.IHO.S-104.2.0"   # S-104 Ed.2.0.0
HORIZONTAL_CRS     = 4326                    # WGS84 (EPSG:4326)
VERTICAL_CS        = 6499                    # Height (EPSG)
VERTICAL_DATUM     = 12                      # MSL = code 12 dalam S-100 vertical datum
VERTICAL_DATUM_REF = 1                       # S-100 Vertical Datum Reference
VERTICAL_COORD_BASE = 2                      # verticalDatum (S-104 Ed.2 wajib = 2)

DATA_CODING_FORMAT = 1    # Time series at fixed stations
COMMON_POINT_RULE  = 4    # high
DIMENSION          = 2    # [waterLevelHeight, waterLevelTrend]
PICK_PRIORITY_TYPE = 2

TOL_CORRECTION_M   = -2.156   # Transfer of Level: Luwes → MSL TPXO9

# waterLevelTrend threshold (m/interval) — dari referensi ITB
TREND_THRESHOLD    = 0.1


def _iso_to_hdf_dt(iso_str: str) -> str:
    """Tulis waktu apa adanya ke format S-104 yyyymmddTHHMMSSZ, tanpa konversi."""
    return iso_str.replace("-", "").replace(":", "").replace(" ", "T").rstrip("Z") + "Z"


def _compute_trend(heights: np.ndarray, threshold: float = TREND_THRESHOLD) -> np.ndarray:
    """
    Compute waterLevelTrend per S-104 spec:
      1  = decreasing
      2  = steady
      3  = increasing
    Menggunakan finite difference.
    """
    diff = np.diff(heights, prepend=heights[0])
    trend = np.where(diff > threshold, 3, np.where(diff < -threshold, 1, 2))
    return trend.astype(np.int8)


def _build_feature_attrs_table() -> np.ndarray:
    """
    Group_F/WaterLevel attribute table sesuai S-104 §10.
    Format: [attributeName, description, uom, fillValue, datatype, lower, upper, closure]
    """
    dtype = h5py.string_dtype()
    rows = np.array([
        [b'waterLevelHeight', b'Water level height above MSL',
         b'metres', b'-9999', b'H5T_FLOAT', b'-99.99', b'99.99', b'closedInterval'],
        [b'waterLevelTrend',  b'Water level trend (1=dec,2=steady,3=inc)',
         b'', b'0', b'H5T_ENUM', b'', b'', b''],
    ])
    return rows


def export_s104_tpxo(
    predictions: List[Dict],
    grid_lat: float,
    grid_lon: float,
    grid_distance_km: float,
    date_str: str,
    output_path: Optional[str] = None,
) -> str:
    """
    Ekspor prediksi TPXO9 ke format IHO S-104 Ed.2.0.0 HDF5.

    Args:
        predictions  : list of {'time': ISO8601_UTC, 'height': float}
        grid_lat/lon : koordinat grid TPXO terdekat
        grid_distance_km : jarak interpolasi dari titik yang diminta
        date_str     : 'YYYY-MM-DD' tanggal prediksi
        output_path  : path output, jika None buat temp file

    Returns:
        Path file HDF5 yang dihasilkan.
    """
    if not output_path:
        output_path = os.path.join(
            tempfile.gettempdir(),
            f"searibu_s104_tpxo_{date_str}_{abs(grid_lat):.3f}_{grid_lon:.3f}.h5"
        )

    heights = np.array([p["height"] for p in predictions], dtype=np.float32)
    times   = [_iso_to_hdf_dt(p["time"]) for p in predictions]
    trends  = _compute_trend(heights)
    n_times = len(predictions)

    # Structured array: waterLevelHeight + waterLevelTrend
    dt_vals = np.dtype([("waterLevelHeight", np.float32), ("waterLevelTrend", np.int8)])
    values  = np.zeros(n_times, dtype=dt_vals)
    values["waterLevelHeight"] = heights
    values["waterLevelTrend"]  = trends

    dt_now = datetime.now(timezone.utc)
    date_issue  = dt_now.strftime("%Y%m%d")
    time_issue  = dt_now.strftime("%H%M%SZ")

    bbox_lat_min = grid_lat - 0.05
    bbox_lat_max = grid_lat + 0.05
    bbox_lon_min = grid_lon - 0.05
    bbox_lon_max = grid_lon + 0.05

    dt_first = times[0] if times else ""
    dt_last  = times[-1] if times else ""

    with h5py.File(output_path, "w") as hdf:
        # ── ROOT ATTRIBUTES (S-100 §4.4 mandatory) ────────────────────────
        hdf.attrs["productSpecification"]        = PRODUCT_SPEC
        hdf.attrs["issueDate"]                   = date_issue
        hdf.attrs["issueTime"]                   = time_issue
        hdf.attrs["horizontalCRS"]               = HORIZONTAL_CRS
        hdf.attrs["verticalCS"]                  = VERTICAL_CS
        hdf.attrs["verticalCoordinateBase"]      = VERTICAL_COORD_BASE   # Ed.2 mandatory
        hdf.attrs["verticalDatum"]               = VERTICAL_DATUM
        hdf.attrs["verticalDatumReference"]      = VERTICAL_DATUM_REF
        hdf.attrs["northBoundLatitude"]          = float(bbox_lat_max)
        hdf.attrs["southBoundLatitude"]          = float(bbox_lat_min)
        hdf.attrs["eastBoundLongitude"]          = float(bbox_lon_max)
        hdf.attrs["westBoundLongitude"]          = float(bbox_lon_min)
        hdf.attrs["geographicIdentifier"]        = "Kepulauan Seribu, Jakarta Bay, Indonesia"
        hdf.attrs["producer"]                    = "Searibu — ITB Geodesy and Geomatics Engineering"
        hdf.attrs["datasetDeliveryInterval"]     = "PT1H"
        hdf.attrs["waterLevelTrendThreshold"]    = TREND_THRESHOLD
        hdf.attrs["metaFeatures"]                = "tpxo9-atlas-v5; harmonicPrediction"

        # ── GROUP_F (Feature Catalogue) ───────────────────────────────────
        gf = hdf.create_group("Group_F")
        gf.create_dataset("featureCode", data=np.array([b"WaterLevel"]))
        gf.create_dataset("WaterLevel",  data=_build_feature_attrs_table())

        # ── WaterLevel (Feature Container) ────────────────────────────────
        wl = hdf.create_group("WaterLevel")
        wl.attrs["commonPointRule"]              = COMMON_POINT_RULE
        wl.attrs["dataCodingFormat"]             = DATA_CODING_FORMAT
        wl.attrs["dataDynamicity"]               = 1    # astronomicalPrediction (S-104 Ed.2)
        wl.attrs["dimension"]                    = DIMENSION
        wl.attrs["horizontalPositionUncertainty"] = float(grid_distance_km * 1000)  # m
        wl.attrs["maxDatasetHeight"]             = float(np.max(heights))
        wl.attrs["minDatasetHeight"]             = float(np.min(heights))
        wl.attrs["numInstances"]                 = 1
        wl.attrs["pickPriorityType"]             = PICK_PRIORITY_TYPE
        wl.attrs["timeUncertainty"]              = -1.0
        wl.attrs["verticalUncertainty"]          = -1.0

        # ── WaterLevel.01 (Feature Instance) ──────────────────────────────
        wl01 = hdf.create_group("WaterLevel/WaterLevel.01")
        wl01.attrs["dateTimeOfFirstRecord"]      = dt_first
        wl01.attrs["dateTimeOfLastRecord"]       = dt_last
        wl01.attrs["northBoundLatitude"]         = float(bbox_lat_max)
        wl01.attrs["southBoundLatitude"]         = float(bbox_lat_min)
        wl01.attrs["eastBoundLongitude"]         = float(bbox_lon_max)
        wl01.attrs["westBoundLongitude"]         = float(bbox_lon_min)
        wl01.attrs["numGRP"]                     = 1
        wl01.attrs["numberOfStations"]           = 1
        wl01.attrs["numberOfTimes"]              = n_times
        wl01.attrs["timeRecordInterval"]         = 3600   # 1 jam dalam detik
        wl01.attrs["startDateTime"]              = dt_first
        wl01.attrs["endDateTime"]                = dt_last
        wl01.attrs["stationIdentification"]      = f"TPXO_{abs(grid_lat):.3f}_{grid_lon:.3f}"

        # ── Group_001 (Station Data) ───────────────────────────────────────
        g001 = hdf.create_group("WaterLevel/WaterLevel.01/Group_001")
        g001.create_dataset("values", data=values)
        g001.attrs["startDateTime"]              = dt_first
        g001.attrs["endDateTime"]                = dt_last
        g001.attrs["numberOfTimes"]              = n_times
        g001.attrs["timeRecordInterval"]         = 3600
        g001.attrs["timeIntervalIndex"]          = 1
        g001.attrs["stationIdentification"]      = f"TPXO_{abs(grid_lat):.3f}_{grid_lon:.3f}"

        # ── Positioning (koordinat) ────────────────────────────────────────
        pos_grp = hdf.create_group("WaterLevel/WaterLevel.01/positioning")
        pos_grp.create_dataset(
            "geometryValues",
            data=np.array([[grid_lat, grid_lon]], dtype=np.float64)
        )

        # ── DateTime array ─────────────────────────────────────────────────
        g001.create_dataset(
            "dateTime",
            data=np.array([t.encode() for t in times])
        )

    logger.info(f"[S-104] TPXO export selesai: {output_path} ({n_times} timestep)")
    return output_path


def export_s104_luwes(
    observations: List[Dict],
    station_meta: Dict,
    date_str: str,
    apply_tol: bool = True,
    output_path: Optional[str] = None,
) -> str:
    """
    Ekspor observasi stasiun Luwes ke format IHO S-104 Ed.2.0.0.
    dataDynamicity = 3 (observed)

    Args:
        observations : list of {'recorded_at': ISO8601+07, 'level_m': float}
        station_meta : {'imei': str, 'lat': float, 'lon': float, 'name': str}
        date_str     : 'YYYY-MM-DD'
        apply_tol    : apakah menerapkan koreksi TOL = -2.156 m
        output_path  : path output HDF5
    """
    if not output_path:
        output_path = os.path.join(
            tempfile.gettempdir(),
            f"searibu_s104_luwes_{date_str}.h5"
        )

    raw_heights = np.array([o["level_m"] for o in observations], dtype=np.float32)
    heights = raw_heights + TOL_CORRECTION_M if apply_tol else raw_heights
    times   = []
    for o in observations:
        # Konversi +07:00 → UTC
        ts = o["recorded_at"]
        try:
            if "+07" in ts:
                dt_wib = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                dt_utc = dt_wib - timedelta(hours=7)
                times.append(dt_utc.strftime("%Y%m%dT%H%M%SZ"))
            else:
                times.append(_iso_to_hdf_dt(ts))
        except Exception:
            times.append(ts)

    trends  = _compute_trend(heights)
    n_times = len(observations)

    dt_vals = np.dtype([("waterLevelHeight", np.float32), ("waterLevelTrend", np.int8)])
    values  = np.zeros(n_times, dtype=dt_vals)
    values["waterLevelHeight"] = heights
    values["waterLevelTrend"]  = trends

    dt_now = datetime.now(timezone.utc)
    lat = station_meta.get("lat", -5.7439)
    lon = station_meta.get("lon", 106.6128)
    imei = station_meta.get("imei", "869556066101370")
    name = station_meta.get("name", "Luwes Tidal Station")

    dt_first = times[0] if times else ""
    dt_last  = times[-1] if times else ""

    with h5py.File(output_path, "w") as hdf:
        # ROOT
        hdf.attrs["productSpecification"]        = PRODUCT_SPEC
        hdf.attrs["issueDate"]                   = dt_now.strftime("%Y%m%d")
        hdf.attrs["issueTime"]                   = dt_now.strftime("%H%M%SZ")
        hdf.attrs["horizontalCRS"]               = HORIZONTAL_CRS
        hdf.attrs["verticalCS"]                  = VERTICAL_CS
        hdf.attrs["verticalCoordinateBase"]      = VERTICAL_COORD_BASE
        hdf.attrs["verticalDatum"]               = VERTICAL_DATUM
        hdf.attrs["verticalDatumReference"]      = VERTICAL_DATUM_REF
        hdf.attrs["northBoundLatitude"]          = float(lat + 0.01)
        hdf.attrs["southBoundLatitude"]          = float(lat - 0.01)
        hdf.attrs["eastBoundLongitude"]          = float(lon + 0.01)
        hdf.attrs["westBoundLongitude"]          = float(lon - 0.01)
        hdf.attrs["geographicIdentifier"]        = "Kepulauan Seribu, Jakarta Bay, Indonesia"
        hdf.attrs["producer"]                    = "Searibu — ITB Geodesy and Geomatics Engineering"
        hdf.attrs["datasetDeliveryInterval"]     = "PT1M"   # Luwes fetch tiap 1 menit
        hdf.attrs["waterLevelTrendThreshold"]    = TREND_THRESHOLD
        hdf.attrs["stationIMEI"]                 = imei
        if apply_tol:
            hdf.attrs["verticalDatumCorrectionFactor"] = TOL_CORRECTION_M
            hdf.attrs["verticalDatumCorrectionDescription"] = (
                "Transfer of Level (TOL): Luwes station reading corrected to MSL TPXO9-atlas-v5. "
                f"Correction applied: {TOL_CORRECTION_M} m"
            )

        # GROUP_F
        gf = hdf.create_group("Group_F")
        gf.create_dataset("featureCode", data=np.array([b"WaterLevel"]))
        gf.create_dataset("WaterLevel",  data=_build_feature_attrs_table())

        # WaterLevel container
        wl = hdf.create_group("WaterLevel")
        wl.attrs["commonPointRule"]              = COMMON_POINT_RULE
        wl.attrs["dataCodingFormat"]             = DATA_CODING_FORMAT
        wl.attrs["dataDynamicity"]               = 3    # observed (S-104 Ed.2)
        wl.attrs["dimension"]                    = DIMENSION
        wl.attrs["horizontalPositionUncertainty"] = 5.0  # m (GPS precision)
        wl.attrs["maxDatasetHeight"]             = float(np.max(heights)) if n_times > 0 else 0.0
        wl.attrs["minDatasetHeight"]             = float(np.min(heights)) if n_times > 0 else 0.0
        wl.attrs["numInstances"]                 = 1
        wl.attrs["pickPriorityType"]             = PICK_PRIORITY_TYPE
        wl.attrs["timeUncertainty"]              = 60.0  # ±60 detik (Luwes fetch interval)
        wl.attrs["verticalUncertainty"]          = 0.05  # ±5 cm sensor precision

        # WaterLevel.01
        wl01 = hdf.create_group("WaterLevel/WaterLevel.01")
        wl01.attrs["dateTimeOfFirstRecord"]      = dt_first
        wl01.attrs["dateTimeOfLastRecord"]       = dt_last
        wl01.attrs["northBoundLatitude"]         = float(lat + 0.01)
        wl01.attrs["southBoundLatitude"]         = float(lat - 0.01)
        wl01.attrs["eastBoundLongitude"]         = float(lon + 0.01)
        wl01.attrs["westBoundLongitude"]         = float(lon - 0.01)
        wl01.attrs["numGRP"]                     = 1
        wl01.attrs["numberOfStations"]           = 1
        wl01.attrs["numberOfTimes"]              = n_times
        wl01.attrs["timeRecordInterval"]         = 300   # ~5 menit Luwes nominal
        wl01.attrs["startDateTime"]              = dt_first
        wl01.attrs["endDateTime"]                = dt_last
        wl01.attrs["stationIdentification"]      = imei

        # Group_001
        g001 = hdf.create_group("WaterLevel/WaterLevel.01/Group_001")
        g001.create_dataset("values",   data=values)
        g001.create_dataset("dateTime", data=np.array([t.encode() for t in times]))
        g001.attrs["startDateTime"]          = dt_first
        g001.attrs["endDateTime"]            = dt_last
        g001.attrs["numberOfTimes"]          = n_times
        g001.attrs["timeRecordInterval"]     = 300
        g001.attrs["timeIntervalIndex"]      = 1
        g001.attrs["stationIdentification"]  = imei
        g001.attrs["stationName"]            = name

        # Positioning
        pos = hdf.create_group("WaterLevel/WaterLevel.01/positioning")
        pos.create_dataset(
            "geometryValues",
            data=np.array([[lat, lon]], dtype=np.float64)
        )

    logger.info(f"[S-104] Luwes export selesai: {output_path} ({n_times} obs)")
    return output_path


def validate_s104_file(path: str) -> Dict:
    """
    Validasi sederhana struktur file S-104 HDF5.
    Returns dict dengan status dan daftar error/warning.
    """
    errors   = []
    warnings = []

    required_root_attrs = [
        "productSpecification", "issueDate", "horizontalCRS",
        "verticalCoordinateBase", "verticalDatum", "northBoundLatitude",
        "southBoundLatitude", "eastBoundLongitude", "westBoundLongitude",
    ]
    required_groups   = ["Group_F", "WaterLevel"]
    required_datasets = ["Group_F/featureCode", "Group_F/WaterLevel"]

    try:
        with h5py.File(path, "r") as f:
            # Root attributes
            for attr in required_root_attrs:
                if attr not in f.attrs:
                    errors.append(f"Missing root attribute: {attr}")

            # productSpecification check
            ps = str(f.attrs.get("productSpecification", ""))
            if "S-104" not in ps:
                errors.append(f"productSpecification tidak mengandung 'S-104': {ps}")

            # horizontalCRS
            crs = f.attrs.get("horizontalCRS", 0)
            if int(crs) != 4326:
                warnings.append(f"horizontalCRS bukan WGS84 (4326): {crs}")

            # verticalCoordinateBase Ed.2 = 2
            vcb = f.attrs.get("verticalCoordinateBase", 0)
            if int(vcb) != 2:
                errors.append(f"verticalCoordinateBase harus 2 (S-104 Ed.2): {vcb}")

            # Groups
            for grp in required_groups:
                if grp not in f:
                    errors.append(f"Missing group: {grp}")

            # Datasets
            for ds in required_datasets:
                if ds not in f:
                    errors.append(f"Missing dataset: {ds}")

            # WaterLevel attributes
            if "WaterLevel" in f:
                wl = f["WaterLevel"]
                for attr in ["dataCodingFormat", "dataDynamicity", "dimension"]:
                    if attr not in wl.attrs:
                        errors.append(f"WaterLevel missing attr: {attr}")

                # dataDynamicity range check
                dd = wl.attrs.get("dataDynamicity", -1)
                valid_dd = {1, 2, 3, 5}   # Ed.2 allowed values
                if int(dd) not in valid_dd:
                    errors.append(f"dataDynamicity nilai tidak valid: {dd} (harus 1,2,3,5)")

            n_groups = sum(1 for k in f.keys() if k.startswith("WaterLevel"))
            if n_groups == 0:
                errors.append("Tidak ada WaterLevel feature group")

        status = "valid" if not errors else "invalid"
        return {
            "status":   status,
            "path":     path,
            "errors":   errors,
            "warnings": warnings,
            "standard": "IHO S-104 Edition 2.0.0",
        }

    except Exception as e:
        return {
            "status":   "error",
            "path":     path,
            "errors":   [str(e)],
            "warnings": [],
            "standard": "IHO S-104 Edition 2.0.0",
        }