"""Geospatial File Measurement API (FastAPI).

Upload a KML file or a zipped Shapefile and get area/length measurements in metres.
Run with:  uvicorn main:app --reload
"""
import math
import os
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel
from pyproj import Transformer
from shapely.geometry import mapping
from shapely.ops import transform



# ==========================================================================
# Response models (schemas)
# ==========================================================================
class FileInfo(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str
    status: str
    created_at: datetime


class Feature(BaseModel):
    id: int
    geometry_type: str
    geometry: Dict[str, Any]
    crs: str
    properties: Dict[str, Any]


class Measurement(BaseModel):
    feature_id: int
    geometry_type: str
    status: str  # MEASURED | NOT_REQUIRED | UNSUPPORTED
    area_sq_m: Optional[float] = None
    area_hectares: Optional[float] = None
    length_m: Optional[float] = None
    projected_crs: Optional[str] = None
    message: Optional[str] = None


class MeasurementsResponse(BaseModel):
    file_id: str
    source_crs: str
    feature_count: int
    measurements: List[Measurement]


# ==========================================================================
# In-memory storage
# ==========================================================================
@dataclass
class StoredFile:
    id: str
    filename: str
    crs: str
    status: str
    created_at: datetime
    features: List[dict]
    measurements: List[dict]


class FileStore:
    def __init__(self) -> None:
        self._files: Dict[str, StoredFile] = {}
        self._lock = threading.Lock()

    def add(
        self,
        filename: str,
        crs: str,
        features: List[dict],
        measurements: List[dict],
    ) -> StoredFile:
        stored = StoredFile(
            id=uuid.uuid4().hex[:12],
            filename=filename,
            crs=crs,
            status="COMPLETED",
            created_at=datetime.now(timezone.utc),
            features=features,
            measurements=measurements,
        )
        with self._lock:
            self._files[stored.id] = stored
        return stored

    def get(self, file_id: str) -> Optional[StoredFile]:
        with self._lock:
            return self._files.get(file_id)


store = FileStore()


# ==========================================================================
# File reading, feature extraction and measurements
# ==========================================================================
AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString"}
NO_MEASUREMENT_TYPES = {"Point", "MultiPoint"}

MAX_UNCOMPRESSED_ZIP_BYTES = 200 * 1024 * 1024  # protects against zip bombs


class FileProcessingError(Exception):
    """Raised when an uploaded file cannot be read as geospatial data."""


# --------------------------------------------------------------------------
# 1. Reading files
# --------------------------------------------------------------------------
def read_geodataframe(filename: str, content: bytes) -> gpd.GeoDataFrame:
    """Read a .kml file or a .zip (containing a Shapefile) into a GeoDataFrame."""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".kml", ".zip"):
        raise FileProcessingError(
            "Unsupported file type. Upload a .kml file or a .zip containing a Shapefile."
        )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            if suffix == ".zip":
                gdf = _read_zipped_shapefile(content, tmp)
            else:
                gdf = _read_kml(content, tmp)
        except FileProcessingError:
            raise
        except Exception as exc:  # unreadable / corrupt data
            raise FileProcessingError(f"Could not read geospatial data: {exc}") from exc

    # Drop rows that have no usable geometry.
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
    if gdf.empty:
        raise FileProcessingError("The file does not contain any features with geometry.")
    return gdf


def _read_kml(content: bytes, tmp: str) -> gpd.GeoDataFrame:
    path = os.path.join(tmp, "upload.kml")
    with open(path, "wb") as f:
        f.write(content)

    # A KML file can hold several layers (one per folder), so read all of them.
    layer_names = [row[0] for row in pyogrio.list_layers(path)]
    frames = [gpd.read_file(path, layer=name) for name in layer_names]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        raise FileProcessingError("The KML file does not contain any features.")

    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry")
    # KML always uses WGS84 (EPSG:4326) by specification.
    return gdf.set_crs(epsg=4326, allow_override=True)


def _read_zipped_shapefile(content: bytes, tmp: str) -> gpd.GeoDataFrame:
    zip_path = os.path.join(tmp, "upload.zip")
    extract_dir = os.path.join(tmp, "extracted")
    with open(zip_path, "wb") as f:
        f.write(content)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            if sum(info.file_size for info in zf.infolist()) > MAX_UNCOMPRESSED_ZIP_BYTES:
                raise FileProcessingError("The zip archive is too large once extracted.")
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise FileProcessingError("The uploaded file is not a valid zip archive.") from exc

    shp_files = sorted(Path(extract_dir).rglob("*.shp"))
    if not shp_files:
        raise FileProcessingError("No .shp file was found inside the zip archive.")

    gdf = gpd.read_file(shp_files[0])
    if gdf.crs is None:
        raise FileProcessingError(
            "The Shapefile has no CRS (the .prj file is missing), so measurements "
            "cannot be calculated reliably."
        )
    return gdf


# --------------------------------------------------------------------------
# 2. Feature extraction
# --------------------------------------------------------------------------
def crs_label(gdf: gpd.GeoDataFrame) -> str:
    epsg = gdf.crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else gdf.crs.to_string()


def build_features(gdf: gpd.GeoDataFrame) -> List[dict]:
    """Return one dictionary per feature: id, geometry type, geometry, CRS, properties."""
    label = crs_label(gdf)
    geometry_column = gdf.geometry.name
    features = []
    for index, row in gdf.iterrows():
        geom = row[geometry_column]
        properties = {
            str(key): _to_json_safe(value)
            for key, value in row.items()
            if key != geometry_column
        }
        features.append(
            {
                "id": int(index),
                "geometry_type": geom.geom_type,
                "geometry": mapping(geom),
                "crs": label,
                "properties": properties,
            }
        )
    return features


def _to_json_safe(value: Any) -> Any:
    """Convert pandas/numpy values into plain JSON-friendly Python values."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# --------------------------------------------------------------------------
# 3. Measurements
# --------------------------------------------------------------------------
def calculate_measurements(gdf: gpd.GeoDataFrame) -> List[dict]:
    """Measure every feature. Geometries are converted to WGS84 first, then each
    one is projected to its own UTM zone so distances/areas are in metres."""
    wgs84 = gdf.to_crs(epsg=4326)
    results = []
    for index, geom in wgs84.geometry.items():
        result = measure_geometry(geom)
        result["feature_id"] = int(index)
        result["geometry_type"] = geom.geom_type
        results.append(result)
    return results


def measure_geometry(geom_wgs84) -> dict:
    geom_type = geom_wgs84.geom_type

    if geom_type in NO_MEASUREMENT_TYPES:
        return {"status": "NOT_REQUIRED", "message": "No measurement is required for points."}

    if geom_type not in AREA_TYPES | LENGTH_TYPES:
        return {
            "status": "UNSUPPORTED",
            "message": f"Measurement is not supported for geometry type '{geom_type}'.",
        }

    geom_2d = shapely.force_2d(geom_wgs84)  # ignore altitude (Z) values
    epsg = utm_epsg_for(geom_2d)
    projected = transform(_transformer_to_utm(epsg).transform, geom_2d)

    result = {"status": "MEASURED", "projected_crs": f"EPSG:{epsg}"}
    if geom_type in AREA_TYPES:
        area = projected.area
        result["area_sq_m"] = round(area, 3)
        result["area_hectares"] = round(area / 10_000, 5)
    else:
        result["length_m"] = round(projected.length, 3)
    return result


def utm_epsg_for(geom_wgs84) -> int:
    """Pick the UTM zone (WGS84) that contains the geometry's centroid.
    Northern hemisphere = EPSG:326xx, southern hemisphere = EPSG:327xx."""
    centroid = geom_wgs84.centroid
    zone = min(int((centroid.x + 180) // 6) + 1, 60)
    return (32600 if centroid.y >= 0 else 32700) + zone


@lru_cache(maxsize=128)
def _transformer_to_utm(epsg: int) -> Transformer:
    return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)


# ==========================================================================
# API endpoints
# ==========================================================================
router = APIRouter(prefix="/api/files", tags=["files"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


def _to_info(stored: StoredFile) -> FileInfo:
    return FileInfo(
        id=stored.id,
        filename=stored.filename,
        feature_count=len(stored.features),
        crs=stored.crs,
        status=stored.status,
        created_at=stored.created_at,
    )


def _get_or_404(file_id: str) -> StoredFile:
    stored = store.get(file_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return stored


@router.post("/", response_model=FileInfo, status_code=201)
def upload_file(file: UploadFile = File(...)):
    """Upload a .kml file or a .zip containing a Shapefile and process it."""
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is too large (limit is 25 MB).")

    filename = file.filename or "upload"
    try:
        gdf = read_geodataframe(filename, content)
        features = build_features(gdf)
        measurements = calculate_measurements(gdf)
    except FileProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    stored = store.add(filename, crs_label(gdf), features, measurements)
    return _to_info(stored)


@router.get("/{file_id}/", response_model=FileInfo)
def get_file(file_id: str):
    """Return basic information about an uploaded file."""
    return _to_info(_get_or_404(file_id))


@router.get("/{file_id}/features/", response_model=List[Feature])
def get_features(file_id: str):
    """Return every feature (id, geometry type, geometry, CRS, properties)."""
    return _get_or_404(file_id).features


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse)
def get_measurements(file_id: str):
    """Return area/length measurements for every feature."""
    stored = _get_or_404(file_id)
    return MeasurementsResponse(
        file_id=stored.id,
        source_crs=stored.crs,
        feature_count=len(stored.features),
        measurements=stored.measurements,
    )


# ==========================================================================
# Application
# ==========================================================================
app = FastAPI(
    title="Geospatial File Measurement API",
    description="Upload a KML or zipped Shapefile and get area/length measurements.",
    version="1.0.0",
)
app.include_router(router)


@app.get("/", tags=["health"])
def health_check():
    return {"status": "ok"}
