"""IHO S-104 Water Level Information for Surface Navigation — HDF5 exporter.

Generates HDF5 files compliant with IHO S-104 Edition 2.0.0 (adopted Dec 2024).

HDF5 structure produced:
    /                               (root S-100 metadata attributes)
    /Group_F/featureCode            ['WaterLevel']
    /Group_F/WaterLevel             feature attribute table
    /WaterLevel/                    feature container
        WaterLevel.01/              feature instance
            Group_001/values        structured array [waterLevelHeight, waterLevelTrend]
            Group_001/dateTime      ISO 8601 UTC timestamps
            positioning/            station coordinates

Data dynamicity codes (S-104 §8.7.1):
    1 = astronomicalPrediction  (TPXO data)
    3 = observed                (Luwes station data)

References:
    IHO S-100 Universal Hydrographic Data Model, Ed. 5.2.0 (2024)
    IHO S-104 Water Level Information for Surface Navigation, Ed. 2.0.0 (2024)
    Amanda et al. (2023) ITB Capstone — S-100 Process Design
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

PRODUCT_SPEC = "INT.IHO.S-104.2.0"
HORIZONTAL_CRS = 4326
VERTICAL_CS = 6499
VERTICAL_DATUM = 12
VERTICAL_DATUM_REF = 1
VERTICAL_COORD_BASE = 2
DATA_CODING_FORMAT = 1
COMMON_POINT_RULE = 4
DIMENSION = 2
PICK_PRIORITY_TYPE = 2
TOL_CORRECTION_M = -2.156
TREND_THRESHOLD = 0.1


def _to_hdf_dt(iso_str: str) -> str:
    """Convert an ISO 8601 string to the S-104 yyyymmddTHHMMSSZ format."""
    return iso_str.replace("-", "").replace(":", "").replace(" ", "T").rstrip("Z") + "Z"


def _compute_trend(heights: np.ndarray, threshold: float = TREND_THRESHOLD) -> np.ndarray:
    """Compute per-sample waterLevelTrend values (1=decreasing, 2=steady, 3=increasing)."""
    diff = np.diff(heights, prepend=heights[0])
    return np.where(diff > threshold, 3, np.where(diff < -threshold, 1, 2)).astype(np.int8)


def _feature_attrs_table() -> np.ndarray:
    """Build the Group_F/WaterLevel attribute table required by S-104 §10."""
    return np.array([
        [b"waterLevelHeight", b"Water level height above MSL", b"metres", b"-9999", b"H5T_FLOAT", b"-99.99", b"99.99", b"closedInterval"],
        [b"waterLevelTrend", b"Water level trend (1=dec,2=steady,3=inc)", b"", b"0", b"H5T_ENUM", b"", b"", b""],
    ])


def export_s104_tpxo(
    predictions: List[Dict],
    grid_lat: float,
    grid_lon: float,
    grid_distance_km: float,
    date_str: str,
    output_path: Optional[str] = None,
) -> str:
    """Export TPXO9 astronomical predictions as an IHO S-104 Ed. 2.0.0 HDF5 file.

    Args:
        predictions:      list of {time: ISO8601 UTC, height: float (m)}.
        grid_lat/lon:     coordinates of the nearest TPXO grid point.
        grid_distance_km: interpolation distance from the requested point.
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
    times = [_to_hdf_dt(p["time"]) for p in predictions]
    n_times = len(predictions)

    values = np.zeros(n_times, dtype=np.dtype([("waterLevelHeight", np.float32), ("waterLevelTrend", np.int8)]))
    values["waterLevelHeight"] = heights
    values["waterLevelTrend"] = _compute_trend(heights)

    now = datetime.now(timezone.utc)
    bbox = (grid_lat - 0.05, grid_lat + 0.05, grid_lon - 0.05, grid_lon + 0.05)
    dt_first = times[0] if times else ""
    dt_last = times[-1] if times else ""
    station_id = f"TPXO_{abs(grid_lat):.3f}_{grid_lon:.3f}"

    with h5py.File(output_path, "w") as hdf:
        hdf.attrs.update({
            "productSpecification": PRODUCT_SPEC,
            "issueDate": now.strftime("%Y%m%d"),
            "issueTime": now.strftime("%H%M%SZ"),
            "horizontalCRS": HORIZONTAL_CRS,
            "verticalCS": VERTICAL_CS,
            "verticalCoordinateBase": VERTICAL_COORD_BASE,
            "verticalDatum": VERTICAL_DATUM,
            "verticalDatumReference": VERTICAL_DATUM_REF,
            "northBoundLatitude": float(bbox[1]),
            "southBoundLatitude": float(bbox[0]),
            "eastBoundLongitude": float(bbox[3]),
            "westBoundLongitude": float(bbox[2]),
            "geographicIdentifier": "Kepulauan Seribu, Jakarta Bay, Indonesia",
            "producer": "Searibu — ITB Geodesy and Geomatics Engineering",
            "datasetDeliveryInterval": "PT1H",
            "waterLevelTrendThreshold": TREND_THRESHOLD,
            "metaFeatures": "tpxo9-atlas-v5; harmonicPrediction",
        })

        gf = hdf.create_group("Group_F")
        gf.create_dataset("featureCode", data=np.array([b"WaterLevel"]))
        gf.create_dataset("WaterLevel", data=_feature_attrs_table())

        wl = hdf.create_group("WaterLevel")
        wl.attrs.update({
            "commonPointRule": COMMON_POINT_RULE,
            "dataCodingFormat": DATA_CODING_FORMAT,
            "dataDynamicity": 1,
            "dimension": DIMENSION,
            "horizontalPositionUncertainty": float(grid_distance_km * 1000),
            "maxDatasetHeight": float(np.max(heights)),
            "minDatasetHeight": float(np.min(heights)),
            "numInstances": 1,
            "pickPriorityType": PICK_PRIORITY_TYPE,
            "timeUncertainty": -1.0,
            "verticalUncertainty": -1.0,
        })

        wl01 = hdf.create_group("WaterLevel/WaterLevel.01")
        wl01.attrs.update({
            "dateTimeOfFirstRecord": dt_first,
            "dateTimeOfLastRecord": dt_last,
            "northBoundLatitude": float(bbox[1]),
            "southBoundLatitude": float(bbox[0]),
            "eastBoundLongitude": float(bbox[3]),
            "westBoundLongitude": float(bbox[2]),
            "numGRP": 1,
            "numberOfStations": 1,
            "numberOfTimes": n_times,
            "timeRecordInterval": 3600,
            "startDateTime": dt_first,
            "endDateTime": dt_last,
            "stationIdentification": station_id,
        })

        g001 = hdf.create_group("WaterLevel/WaterLevel.01/Group_001")
        g001.create_dataset("values", data=values)
        g001.create_dataset("dateTime", data=np.array([t.encode() for t in times]))
        g001.attrs.update({
            "startDateTime": dt_first,
            "endDateTime": dt_last,
            "numberOfTimes": n_times,
            "timeRecordInterval": 3600,
            "timeIntervalIndex": 1,
            "stationIdentification": station_id,
        })

        pos = hdf.create_group("WaterLevel/WaterLevel.01/positioning")
        pos.create_dataset("geometryValues", data=np.array([[grid_lat, grid_lon]], dtype=np.float64))

    logger.info("S-104 TPXO export complete: %s (%d timesteps)", output_path, n_times)
    return output_path


def export_s104_luwes(
    observations: List[Dict],
    station_meta: Dict,
    date_str: str,
    apply_tol: bool = True,
    output_path: Optional[str] = None,
) -> str:
    """Export Luwes station observations as an IHO S-104 Ed. 2.0.0 HDF5 file.

    dataDynamicity is set to 3 (observed).

    Args:
        observations: list of {recorded_at: ISO 8601 +07:00, level_m: float}.
        station_meta: {imei, lat, lon, name}.
        date_str:     'YYYY-MM-DD'.
        apply_tol:    whether to apply the TOL correction of -2.156 m.
        output_path:  destination path; a temp file is used if None.
    """
    if not output_path:
        output_path = os.path.join(tempfile.gettempdir(), f"searibu_s104_luwes_{date_str}.h5")

    raw_heights = np.array([o["level_m"] for o in observations], dtype=np.float32)
    heights = raw_heights + TOL_CORRECTION_M if apply_tol else raw_heights

    times = []
    for o in observations:
        ts = o["recorded_at"]
        try:
            if "+07" in ts:
                dt_utc = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") - timedelta(hours=7)
                times.append(dt_utc.strftime("%Y%m%dT%H%M%SZ"))
            else:
                times.append(_to_hdf_dt(ts))
        except Exception:
            times.append(ts)

    n_times = len(observations)
    values = np.zeros(n_times, dtype=np.dtype([("waterLevelHeight", np.float32), ("waterLevelTrend", np.int8)]))
    values["waterLevelHeight"] = heights
    values["waterLevelTrend"] = _compute_trend(heights)

    now = datetime.now(timezone.utc)
    lat = station_meta.get("lat", -5.7439)
    lon = station_meta.get("lon", 106.6128)
    imei = station_meta.get("imei", "869556066101370")
    name = station_meta.get("name", "Luwes Tidal Station")
    dt_first = times[0] if times else ""
    dt_last = times[-1] if times else ""

    with h5py.File(output_path, "w") as hdf:
        hdf.attrs.update({
            "productSpecification": PRODUCT_SPEC,
            "issueDate": now.strftime("%Y%m%d"),
            "issueTime": now.strftime("%H%M%SZ"),
            "horizontalCRS": HORIZONTAL_CRS,
            "verticalCS": VERTICAL_CS,
            "verticalCoordinateBase": VERTICAL_COORD_BASE,
            "verticalDatum": VERTICAL_DATUM,
            "verticalDatumReference": VERTICAL_DATUM_REF,
            "northBoundLatitude": float(lat + 0.01),
            "southBoundLatitude": float(lat - 0.01),
            "eastBoundLongitude": float(lon + 0.01),
            "westBoundLongitude": float(lon - 0.01),
            "geographicIdentifier": "Kepulauan Seribu, Jakarta Bay, Indonesia",
            "producer": "Searibu — ITB Geodesy and Geomatics Engineering",
            "datasetDeliveryInterval": "PT1M",
            "waterLevelTrendThreshold": TREND_THRESHOLD,
            "stationIMEI": imei,
        })
        if apply_tol:
            hdf.attrs["verticalDatumCorrectionFactor"] = TOL_CORRECTION_M
            hdf.attrs["verticalDatumCorrectionDescription"] = (
                f"Transfer of Level (TOL): Luwes station corrected to MSL TPXO9-atlas-v5. "
                f"Correction: {TOL_CORRECTION_M} m"
            )

        gf = hdf.create_group("Group_F")
        gf.create_dataset("featureCode", data=np.array([b"WaterLevel"]))
        gf.create_dataset("WaterLevel", data=_feature_attrs_table())

        wl = hdf.create_group("WaterLevel")
        wl.attrs.update({
            "commonPointRule": COMMON_POINT_RULE,
            "dataCodingFormat": DATA_CODING_FORMAT,
            "dataDynamicity": 3,
            "dimension": DIMENSION,
            "horizontalPositionUncertainty": 5.0,
            "maxDatasetHeight": float(np.max(heights)) if n_times > 0 else 0.0,
            "minDatasetHeight": float(np.min(heights)) if n_times > 0 else 0.0,
            "numInstances": 1,
            "pickPriorityType": PICK_PRIORITY_TYPE,
            "timeUncertainty": 60.0,
            "verticalUncertainty": 0.05,
        })

        wl01 = hdf.create_group("WaterLevel/WaterLevel.01")
        wl01.attrs.update({
            "dateTimeOfFirstRecord": dt_first,
            "dateTimeOfLastRecord": dt_last,
            "northBoundLatitude": float(lat + 0.01),
            "southBoundLatitude": float(lat - 0.01),
            "eastBoundLongitude": float(lon + 0.01),
            "westBoundLongitude": float(lon - 0.01),
            "numGRP": 1,
            "numberOfStations": 1,
            "numberOfTimes": n_times,
            "timeRecordInterval": 300,
            "startDateTime": dt_first,
            "endDateTime": dt_last,
            "stationIdentification": imei,
        })

        g001 = hdf.create_group("WaterLevel/WaterLevel.01/Group_001")
        g001.create_dataset("values", data=values)
        g001.create_dataset("dateTime", data=np.array([t.encode() for t in times]))
        g001.attrs.update({
            "startDateTime": dt_first,
            "endDateTime": dt_last,
            "numberOfTimes": n_times,
            "timeRecordInterval": 300,
            "timeIntervalIndex": 1,
            "stationIdentification": imei,
            "stationName": name,
        })

        pos = hdf.create_group("WaterLevel/WaterLevel.01/positioning")
        pos.create_dataset("geometryValues", data=np.array([[lat, lon]], dtype=np.float64))

    logger.info("S-104 Luwes export complete: %s (%d observations)", output_path, n_times)
    return output_path


def validate_s104_file(path: str) -> Dict:
    """Validate the structure and mandatory attributes of an S-104 HDF5 file.

    Returns a dict with keys: status, path, errors, warnings, standard.
    """
    errors: list = []
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
                errors.append(f"productSpecification does not contain 'S-104': {ps}")

            if int(f.attrs.get("horizontalCRS", 0)) != 4326:
                warnings.append(f"horizontalCRS is not WGS84 (4326): {f.attrs.get('horizontalCRS')}")

            if int(f.attrs.get("verticalCoordinateBase", 0)) != 2:
                errors.append(f"verticalCoordinateBase must be 2 (S-104 Ed.2): {f.attrs.get('verticalCoordinateBase')}")

            for grp in ("Group_F", "WaterLevel"):
                if grp not in f:
                    errors.append(f"Missing group: {grp}")

            for ds in ("Group_F/featureCode", "Group_F/WaterLevel"):
                if ds not in f:
                    errors.append(f"Missing dataset: {ds}")

            if "WaterLevel" in f:
                wl = f["WaterLevel"]
                for attr in ("dataCodingFormat", "dataDynamicity", "dimension"):
                    if attr not in wl.attrs:
                        errors.append(f"WaterLevel missing attribute: {attr}")
                if int(wl.attrs.get("dataDynamicity", -1)) not in {1, 2, 3, 5}:
                    errors.append(f"Invalid dataDynamicity: {wl.attrs.get('dataDynamicity')}")

    except Exception as exc:
        return {"status": "error", "path": path, "errors": [str(exc)], "warnings": [], "standard": "IHO S-104 Edition 2.0.0"}

    return {
        "status": "valid" if not errors else "invalid",
        "path": path,
        "errors": errors,
        "warnings": warnings,
        "standard": "IHO S-104 Edition 2.0.0",
    }