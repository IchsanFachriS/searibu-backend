"""IHO S-104 Water Level Information for Surface Navigation — HDF5 exporter.

Generates HDF5 files compliant with IHO S-104 Edition 2.0.0 (adopted Dec 2024).

HDF5 structure produced (per spec table):
    /                               root S-100 metadata attributes
    /Group_F/featureCode            ['WaterLevel']
    /Group_F/WaterLevel             feature attribute table
    /WaterLevel/                    feature container (dataCodingFormat=2, dimension=2)
        WaterLevel.01/              feature instance
            Group_001/ … Group_NNN/ one group per time step (numGRP = numberOfTimes)
                values              structured array [waterLevelHeight, waterLevelTrend]
                dateTime            UTC DateTime string for that time step

Key attribute corrections vs previous version
    verticalDatum          = 3  (meanSeaLevel, S-100 code)   [was 12]
    verticalCoordinateBase = 2  (verticalDatum)
    dataCodingFormat       = 2  (regularly-gridded arrays)    [was 1]
    dataDynamicity         = 2 for TPXO (astronomicalPrediction) [was 1]
    dataDynamicity         = 1 for Luwes (observation)        [was 3]
    numGRP                 = numberOfTimes (one group per timestep)

Data dynamicity codes (S-104 §8.7.1):
    1 = observation
    2 = astronomicalPrediction

References:
    IHO S-100 Universal Hydrographic Data Model, Ed. 5.2.0 (2024)
    IHO S-104 Water Level Information for Surface Navigation, Ed. 2.0.0 (2024)
"""

import os
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import numpy as np
import h5py

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

# ── S-104 Ed.2.0.0 mandatory attribute values ─────────────────────────────
PRODUCT_SPEC         = "INT.IHO.S-104.2.0"
HORIZONTAL_CRS       = 4326          # WGS 84 (EPSG:4326)
VERTICAL_CS          = 6499          # Height, Metres, Orientation Up
VERTICAL_COORD_BASE  = 2             # verticalDatum (only permitted value in S-100)
VERTICAL_DATUM_REF   = 1             # S-100 vertical datum reference
VERTICAL_DATUM       = 3             # meanSeaLevel (MSL) — S-100 code 3
DATA_CODING_FORMAT   = 2             # Regularly-gridded arrays (only permitted in S-104)
COMMON_POINT_RULE    = 4             # all
DIMENSION            = 2             # 2D spatial grid

# dataDynamicity codes
DYNDYN_PREDICTION    = 2             # astronomicalPrediction
DYNDYN_OBSERVATION   = 1             # observation

TOL_CORRECTION_M     = -1.944        # Transfer of Level: Luwes → MSL
TREND_THRESHOLD      = 0.1           # metres; determines steady vs rising/falling


def _to_hdf_dt(iso_str: str) -> str:
    """Convert an ISO 8601 string to the S-104 YYYYMMDDTHHMMSSz format."""
    s = iso_str.replace("-", "").replace(":", "").replace(" ", "T")
    if not s.endswith("Z"):
        s = s.rstrip("Z") + "Z"
    return s


def _compute_trend(heights: np.ndarray, threshold: float = TREND_THRESHOLD) -> np.ndarray:
    """Per-sample waterLevelTrend: 1=decreasing, 2=increasing, 3=steady."""
    diff = np.diff(heights, prepend=heights[0])
    return np.where(diff > threshold, 2,
           np.where(diff < -threshold, 1, 3)).astype(np.int8)


def _feature_attrs_table() -> np.ndarray:
    """Build the Group_F/WaterLevel attribute table required by S-104 §10."""
    return np.array([
        [b"waterLevelHeight", b"Water level height above MSL", b"metres",
         b"-9999", b"H5T_FLOAT", b"-99.99", b"99.99", b"closedInterval"],
        [b"waterLevelTrend",
         b"Water level trend (1=decreasing,2=increasing,3=steady)",
         b"", b"0", b"H5T_ENUM", b"", b"", b""],
    ])


def _root_attrs(
    now: datetime,
    bbox: tuple,
    delivery_interval: str,
    extra: Optional[dict] = None,
) -> dict:
    """Return the mandatory S-104 root group attributes."""
    south, north, west, east = bbox
    attrs = {
        "productSpecification":   PRODUCT_SPEC,
        "issueDate":              now.strftime("%Y%m%d"),
        "issueTime":              now.strftime("%H%M%SZ"),
        "horizontalCRS":          HORIZONTAL_CRS,
        "verticalCS":             VERTICAL_CS,
        "verticalCoordinateBase": VERTICAL_COORD_BASE,
        "verticalDatumReference": VERTICAL_DATUM_REF,
        "verticalDatum":          VERTICAL_DATUM,
        "datasetDeliveryInterval": delivery_interval,
        "geographicIdentifier":   "Kepulauan Seribu, Jakarta, Indonesia",
        "northBoundLatitude":     float(north),
        "southBoundLatitude":     float(south),
        "eastBoundLongitude":     float(east),
        "westBoundLongitude":     float(west),
        "producer":               "Searibu — ITB Geodesy and Geomatics Engineering",
    }
    if extra:
        attrs.update(extra)
    return attrs


def _write_feature_group(
    hdf: h5py.File,
    heights: np.ndarray,
    times: List[str],
    lat: float,
    lon: float,
    bbox: tuple,
    data_dynamicity: int,
    time_record_interval: int,
    h_uncert: float = -1.0,
    pos_uncert: float = -1.0,
    station_id: str = "",
) -> None:
    """Write /Group_F, /WaterLevel, and all time-step groups into the HDF5 file.

    For dataCodingFormat=2 (regularly-gridded), S-104 Ed.2.0.0 requires
    one Group_nnn per time step, each containing a 'values' dataset
    (structured array) and a 'dateTime' scalar string.
    numGRP must equal numberOfTimes.
    """
    n_times = len(times)
    trends = _compute_trend(heights)

    south, north, west, east = bbox

    # /Group_F
    gf = hdf.create_group("Group_F")
    gf.create_dataset("featureCode", data=np.array([b"WaterLevel"]))
    gf.create_dataset("WaterLevel", data=_feature_attrs_table())

    # /WaterLevel feature container
    wl = hdf.create_group("WaterLevel")
    wl.attrs.update({
        "dataCodingFormat":               DATA_CODING_FORMAT,
        "dataDynamicity":                 data_dynamicity,
        "dimension":                      DIMENSION,
        "commonPointRule":                COMMON_POINT_RULE,
        "numInstances":                   1,
        "minDatasetHeight":               float(np.min(heights)),
        "maxDatasetHeight":               float(np.max(heights)),
        "horizontalPositionUncertainty":  pos_uncert,
        "verticalUncertainty":            h_uncert,
    })

    # /WaterLevel/WaterLevel.01 feature instance
    wl01 = hdf.create_group("WaterLevel/WaterLevel.01")
    wl01.attrs.update({
        "dateTimeOfFirstRecord": times[0] if times else "",
        "dateTimeOfLastRecord":  times[-1] if times else "",
        "northBoundLatitude":    float(north),
        "southBoundLatitude":    float(south),
        "eastBoundLongitude":    float(east),
        "westBoundLongitude":    float(west),
        "numberOfTimes":         n_times,
        "timeRecordInterval":    time_record_interval,
        "numGRP":                n_times,          # one group per time step
        "stationIdentification": station_id,
        "startDateTime":         times[0] if times else "",
        "endDateTime":           times[-1] if times else "",
    })

    # One Group_nnn per time step (S-104 Ed.2.0.0 §10, dataCodingFormat=2)
    dt_type = np.dtype([
        ("waterLevelHeight", np.float32),
        ("waterLevelTrend",  np.int8),
    ])
    for idx, (t_str, h_val, trend_val) in enumerate(zip(times, heights, trends)):
        grp_name = f"Group_{idx + 1:03d}"
        grp = hdf.create_group(f"WaterLevel/WaterLevel.01/{grp_name}")

        val = np.zeros(1, dtype=dt_type)
        val["waterLevelHeight"][0] = float(h_val)
        val["waterLevelTrend"][0]  = int(trend_val)
        grp.create_dataset("values",   data=val)
        grp.create_dataset("dateTime", data=np.bytes_(t_str))
        grp.attrs["timePoint"] = t_str

    # /WaterLevel/WaterLevel.01/positioning
    pos = hdf.create_group("WaterLevel/WaterLevel.01/positioning")
    pos.create_dataset(
        "geometryValues",
        data=np.array([[lat, lon]], dtype=np.float64),
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Public export functions
# ──────────────────────────────────────────────────────────────────────────────

def export_s104_tpxo(
    predictions: List[Dict],
    grid_lat: float,
    grid_lon: float,
    grid_distance_km: float,
    date_str: str,
    output_path: Optional[str] = None,
) -> str:
    """Export TPXO astronomical predictions as an IHO S-104 Ed. 2.0.0 HDF5 file.

    dataDynamicity = 2 (astronomicalPrediction)
    timeRecordInterval = 3600 s (hourly)
    numGRP = numberOfTimes (one group per hour)

    Args:
        predictions:      list of {time: ISO8601 UTC, height: float (m)}.
        grid_lat/lon:     coordinates of the nearest TPXO grid point.
        grid_distance_km: interpolation distance (m → horizontal uncertainty).
        date_str:         'YYYY-MM-DD' for the prediction date.
        output_path:      destination path; a temp file is used if None.

    Returns:
        Absolute path to the generated HDF5 file.
    """
    if not output_path:
        output_path = os.path.join(
            tempfile.gettempdir(),
            f"searibu_s104_tpxo_{date_str}_{abs(grid_lat):.3f}_{grid_lon:.3f}.h5",
        )

    heights = np.array([p["height"] for p in predictions], dtype=np.float32)
    times   = [_to_hdf_dt(p["time"]) for p in predictions]
    now     = datetime.now(timezone.utc)
    margin  = 0.05
    bbox    = (grid_lat - margin, grid_lat + margin,
               grid_lon - margin, grid_lon + margin)   # (S, N, W, E)
    station_id = f"TPXO_{abs(grid_lat):.3f}_{grid_lon:.3f}"

    with h5py.File(output_path, "w") as hdf:
        # Root attributes
        hdf.attrs.update(
            _root_attrs(
                now, bbox, "PT1H",
                extra={
                    "metaFeatures": "TPXO10-atlas-v2; harmonicPrediction",
                    "waterLevelTrendThreshold": TREND_THRESHOLD,
                },
            )
        )

        _write_feature_group(
            hdf,
            heights=heights,
            times=times,
            lat=grid_lat,
            lon=grid_lon,
            bbox=bbox,
            data_dynamicity=DYNDYN_PREDICTION,
            time_record_interval=3600,
            h_uncert=-1.0,
            pos_uncert=float(grid_distance_km * 1000),
            station_id=station_id,
        )

    logger.info(
        "S-104 TPXO export complete: %s (%d timesteps, dataDynamicity=2)",
        output_path, len(predictions),
    )
    return output_path


def export_s104_luwes(
    observations: List[Dict],
    station_meta: Dict,
    date_str: str,
    apply_tol: bool = True,
    output_path: Optional[str] = None,
) -> str:
    """Export Luwes station observations as an IHO S-104 Ed. 2.0.0 HDF5 file.

    dataDynamicity = 1 (observation)
    timeRecordInterval = 60 s (nominal 1-minute telemetry)
    numGRP = numberOfTimes (one group per observation)
    TOL correction of -1.944 m applied when apply_tol=True.

    Args:
        observations: list of {recorded_at: ISO 8601 +07:00, level_m: float}.
        station_meta: {imei, lat, lon, name}.
        date_str:     'YYYY-MM-DD'.
        apply_tol:    apply TOL correction to shift Luwes datum to MSL.
        output_path:  destination path; a temp file is used if None.
    """
    if not output_path:
        output_path = os.path.join(
            tempfile.gettempdir(),
            f"searibu_s104_luwes_{date_str}.h5",
        )

    raw_heights = np.array([o["level_m"] for o in observations], dtype=np.float32)
    heights     = raw_heights + TOL_CORRECTION_M if apply_tol else raw_heights

    # Convert WIB timestamps → UTC S-104 format
    times: List[str] = []
    for o in observations:
        ts = o["recorded_at"]
        try:
            if "+07" in ts:
                dt_utc = (datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                          - timedelta(hours=7))
                times.append(dt_utc.strftime("%Y%m%dT%H%M%SZ"))
            else:
                times.append(_to_hdf_dt(ts))
        except Exception:
            times.append(_to_hdf_dt(ts))

    lat     = station_meta.get("lat", -5.7439)
    lon     = station_meta.get("lon", 106.6128)
    imei    = station_meta.get("imei", "869556066101370")
    name    = station_meta.get("name", "Luwes Tidal Station")
    margin  = 0.01
    bbox    = (lat - margin, lat + margin,
               lon - margin, lon + margin)
    now     = datetime.now(timezone.utc)

    extra: dict = {
        "stationIMEI": imei,
        "waterLevelTrendThreshold": TREND_THRESHOLD,
    }
    if apply_tol:
        extra["verticalDatumCorrectionFactor"]      = TOL_CORRECTION_M
        extra["verticalDatumCorrectionDescription"] = (
            f"Transfer of Level (TOL): Luwes station corrected to MSL "
            f"TPXO10-atlas-v2. Correction: {TOL_CORRECTION_M} m"
        )

    with h5py.File(output_path, "w") as hdf:
        hdf.attrs.update(_root_attrs(now, bbox, "PT1M", extra=extra))

        _write_feature_group(
            hdf,
            heights=heights,
            times=times,
            lat=lat,
            lon=lon,
            bbox=bbox,
            data_dynamicity=DYNDYN_OBSERVATION,
            time_record_interval=60,
            h_uncert=0.05,
            pos_uncert=5.0,
            station_id=imei,
        )

        # Extra station metadata on the WaterLevel.01 group
        wl01 = hdf["WaterLevel/WaterLevel.01"]
        wl01.attrs["stationName"] = name
        wl01.attrs["stationIMEI"] = imei

    logger.info(
        "S-104 Luwes export complete: %s (%d observations, dataDynamicity=1)",
        output_path, len(observations),
    )
    return output_path


def validate_s104_file(path: str) -> Dict:
    """Validate the structure and mandatory attributes of an S-104 HDF5 file.

    Returns a dict with keys: status, path, errors, warnings, standard.
    """
    errors:   list = []
    warnings: list = []

    required_root_attrs = [
        "productSpecification", "issueDate", "horizontalCRS",
        "verticalCoordinateBase", "verticalDatum",
        "northBoundLatitude", "southBoundLatitude",
        "eastBoundLongitude", "westBoundLongitude",
    ]

    try:
        with h5py.File(path, "r") as f:
            for attr in required_root_attrs:
                if attr not in f.attrs:
                    errors.append(f"Missing root attribute: {attr}")

            ps = str(f.attrs.get("productSpecification", ""))
            if "S-104" not in ps:
                errors.append(
                    f"productSpecification does not reference S-104: {ps}"
                )

            if int(f.attrs.get("horizontalCRS", 0)) != 4326:
                warnings.append(
                    f"horizontalCRS is not WGS84 (4326): {f.attrs.get('horizontalCRS')}"
                )

            if int(f.attrs.get("verticalCoordinateBase", 0)) != 2:
                errors.append(
                    "verticalCoordinateBase must be 2 (S-100/S-104 Ed.2.0.0 requirement)"
                )

            if int(f.attrs.get("verticalDatum", 0)) != 3:
                warnings.append(
                    f"verticalDatum should be 3 (meanSeaLevel): {f.attrs.get('verticalDatum')}"
                )

            for grp in ("Group_F", "WaterLevel"):
                if grp not in f:
                    errors.append(f"Missing group: {grp}")

            for ds in ("Group_F/featureCode", "Group_F/WaterLevel"):
                if ds not in f:
                    errors.append(f"Missing dataset: {ds}")

            if "WaterLevel" in f:
                wl = f["WaterLevel"]
                for attr in ("dataCodingFormat", "dataDynamicity", "dimension", "numInstances"):
                    if attr not in wl.attrs:
                        errors.append(f"WaterLevel missing attribute: {attr}")

                dcf = int(wl.attrs.get("dataCodingFormat", -1))
                if dcf != 2:
                    errors.append(
                        f"dataCodingFormat must be 2 (only permitted value in S-104): got {dcf}"
                    )

                dyn = int(wl.attrs.get("dataDynamicity", -1))
                if dyn not in {1, 2}:
                    errors.append(
                        f"dataDynamicity must be 1 (observation) or 2 (astronomicalPrediction): got {dyn}"
                    )

                if "WaterLevel.01" in f["WaterLevel"]:
                    wl01 = f["WaterLevel/WaterLevel.01"]
                    n_times = wl01.attrs.get("numberOfTimes", 0)
                    n_grp   = wl01.attrs.get("numGRP", 0)
                    if n_times != n_grp:
                        errors.append(
                            f"numGRP ({n_grp}) must equal numberOfTimes ({n_times}) for dataCodingFormat=2"
                        )

    except Exception as exc:
        return {
            "status":   "error",
            "path":     path,
            "errors":   [str(exc)],
            "warnings": [],
            "standard": "IHO S-104 Edition 2.0.0",
        }

    return {
        "status":   "valid" if not errors else "invalid",
        "path":     path,
        "errors":   errors,
        "warnings": warnings,
        "standard": "IHO S-104 Edition 2.0.0",
    }