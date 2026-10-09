# Geospatial File Measurement API

A FastAPI backend that accepts a geospatial file (**KML** or a **zipped Shapefile**), extracts every feature, and returns **area** (polygons) and **length** (lines) measurements in metres.

## Setup

Requires Python 3.9+.

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Mac / Linux

pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive Swagger UI.

Run the tests:

```bash
pip install pytest httpx
pytest
```

A sample file is provided in `sample.kml` (one polygon, one line, one point).

## API

### `POST /api/files/` - upload and process a file
Multipart form upload, field name `file` (`.kml` or `.zip` containing a Shapefile, max 25 MB).

```bash
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@sample.kml"
```
```json
{
  "id": "3f9a1c2b7d4e",
  "filename": "sample.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-09T08:50:12.123456Z"
}
```
Errors: `400` empty file, `413` file too large, `422` unsupported/corrupt file or Shapefile without a CRS.

### `GET /api/files/{id}/` - file information
Returns the same object as above.

### `GET /api/files/{id}/measurements/` - measurements
```json
{
  "file_id": "3f9a1c2b7d4e",
  "source_crs": "EPSG:4326",
  "feature_count": 3,
  "measurements": [
    {"feature_id": 0, "geometry_type": "Polygon", "status": "MEASURED",
     "area_sq_m": 12043.2, "area_hectares": 1.20432, "length_m": null,
     "projected_crs": "EPSG:32643", "message": null},
    {"feature_id": 1, "geometry_type": "LineString", "status": "MEASURED",
     "area_sq_m": null, "area_hectares": null, "length_m": 1105.9,
     "projected_crs": "EPSG:32643", "message": null},
    {"feature_id": 2, "geometry_type": "Point", "status": "NOT_REQUIRED",
     "message": "No measurement is required for points."}
  ]
}
```
`status` is `MEASURED`, `NOT_REQUIRED` (points) or `UNSUPPORTED` (other geometry types; handled without crashing).

### `GET /api/files/{id}/features/` - extracted features (extra)
Returns every feature with `id`, `geometry_type`, `geometry` (GeoJSON-style), `crs` and `properties`.

(The numbers above are illustrative; run the sample file to see exact values.)

## Architecture

```
main.py            The whole application, organised in clearly marked sections:
                   response models, in-memory storage, file processing /
                   measurement logic, API endpoints, app setup
requirements.txt   Dependencies
test_main.py       pytest tests (KML, zipped Shapefile, error cases)
sample.kml         Example input (polygon, line, point)
```

**File-processing flow**
1. `POST /api/files/` receives the upload and checks size/emptiness.
2. `read_geodataframe` validates the extension, then reads the file with GeoPandas (KML: all layers; ZIP: extracts safely and reads the first `.shp`).
3. `build_features` converts each row into a feature (id, geometry type, geometry, CRS, properties), making property values JSON-safe.
4. `calculate_measurements` measures each feature, and the result is stored and a file `id` is returned.

**Measurement flow**
- Polygon / MultiPolygon: area (m² and hectares). LineString / MultiLineString: length (m). Point / MultiPoint: no measurement. Any other type: `UNSUPPORTED` with an explanatory message.

**CRS handling**
- Areas and lengths are never calculated on latitude/longitude degrees.
- All geometries are first converted to EPSG:4326, then each feature is projected into the **UTM zone of its centroid** (EPSG:326xx north, EPSG:327xx south) and measured there in metres.
- KML is always WGS84 by specification. A Shapefile without a CRS (missing `.prj`) is rejected with a clear error rather than guessing.

## Design Decisions

- **FastAPI over Django**: lightweight, automatic Swagger docs, and Pydantic validation suit a small API with no admin/auth needs.
- **GeoPandas/GDAL for parsing**: one reliable path for both KML and Shapefile, instead of hand-writing parsers.
- **Per-feature UTM zone**: keeps measurements accurate even when a file spans several zones. *Alternatives considered*: one UTM zone for the whole file (simpler, less accurate for large extents), a global equal-area projection such as EPSG:6933 (good for area, poor for lengths), or geodesic calculation with `pyproj.Geod` (accurate for everything, but a different workflow from the "project, then measure" approach).
- **In-memory storage**: sufficient for the assignment and hidden behind a `FileStore` class so it is easy to replace. Data is lost on restart.
- **Synchronous processing**: files are processed during the upload request, so `status` is always `COMPLETED` when returned.
- **Safety**: upload size limit, zip-bomb check, and graceful `422` errors for corrupt input.

## Known Limitations
- Very large files are processed in memory.
- UTM is not valid near the poles (above 84°N / below 80°S), and features crossing the antimeridian are not handled specially.
- Only the first `.shp` found in a zip is read.

## Learning
<!-- Edit this section in your own words so it reflects what you actually learned. -->
- How geographic (lat/long) and projected coordinate systems differ, and why area/length must be calculated in a projected CRS.
- Reading KML and Shapefile data with GeoPandas, and converting geospatial values into clean JSON.
- Structuring a FastAPI project (routes, schemas, processing, storage) and testing it with pytest.

**AI assistance:** Claude (an AI assistant) was used to help design and write parts of this project. <!-- Keep this line only if true, and make sure you have run the project and understand the code. -->

## Future Scope
- Persist data in PostgreSQL/PostGIS (or SQLite) and add pagination for large files.
- Background processing (Celery/RQ) with `PENDING`/`PROCESSING`/`FAILED` statuses for big uploads.
- Support GeoJSON and GeoPackage, and multiple Shapefiles per zip.
- Docker setup, authentication, and CI with GitHub Actions.

*Note: the code is kept in a single `main.py` for simplicity; in a larger project the sections would be split into separate modules (routes, schemas, processing, storage).*
