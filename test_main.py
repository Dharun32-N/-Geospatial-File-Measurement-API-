import io
import zipfile

import geopandas as gpd
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from main import app

client = TestClient(app)

POLYGON_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Plot</name>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
77.0,12.0,0 77.001,12.0,0 77.001,12.001,0 77.0,12.001,0 77.0,12.0,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Document></kml>"""

LINE_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Road</name>
<LineString><coordinates>77.0,12.0,0 77.0,12.01,0</coordinates></LineString>
</Placemark></Document></kml>"""

POINT_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Spot</name>
<Point><coordinates>77.0,12.0,0</coordinates></Point></Placemark></Document></kml>"""


def upload(name, data, content_type="application/octet-stream"):
    return client.post("/api/files/", files={"file": (name, data, content_type)})


def test_polygon_area_is_calculated_in_metres():
    response = upload("plot.kml", POLYGON_KML.encode())
    assert response.status_code == 201
    info = response.json()
    assert info["feature_count"] == 1
    assert info["crs"] == "EPSG:4326"
    assert info["status"] == "COMPLETED"

    result = client.get(f"/api/files/{info['id']}/measurements/").json()
    m = result["measurements"][0]
    assert m["status"] == "MEASURED"
    assert m["projected_crs"] == "EPSG:32643"  # UTM zone 43N
    assert 11_500 < m["area_sq_m"] < 12_600  # roughly 109 m x 111 m


def test_line_length():
    info = upload("road.kml", LINE_KML.encode()).json()
    m = client.get(f"/api/files/{info['id']}/measurements/").json()["measurements"][0]
    assert 1_090 < m["length_m"] < 1_120  # 0.01 degrees of latitude


def test_point_needs_no_measurement():
    info = upload("spot.kml", POINT_KML.encode()).json()
    m = client.get(f"/api/files/{info['id']}/measurements/").json()["measurements"][0]
    assert m["status"] == "NOT_REQUIRED"
    assert m["area_sq_m"] is None and m["length_m"] is None


def test_features_endpoint_returns_properties():
    info = upload("plot.kml", POLYGON_KML.encode()).json()
    features = client.get(f"/api/files/{info['id']}/features/").json()
    assert features[0]["geometry_type"] == "Polygon"
    assert features[0]["properties"]["Name"] == "Plot"


def test_zipped_shapefile(tmp_path):
    square = Polygon([(77.0, 12.0), (77.001, 12.0), (77.001, 12.001), (77.0, 12.001)])
    gpd.GeoDataFrame({"name": ["Plot"]}, geometry=[square], crs="EPSG:4326").to_file(
        tmp_path / "plots.shp"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path in tmp_path.iterdir():
            zf.write(path, path.name)

    response = upload("plots.zip", buffer.getvalue())
    assert response.status_code == 201
    result = client.get(f"/api/files/{response.json()['id']}/measurements/").json()
    assert result["measurements"][0]["status"] == "MEASURED"


def test_unsupported_extension_is_rejected():
    assert upload("notes.txt", b"hello").status_code == 422


def test_corrupt_zip_is_rejected():
    assert upload("bad.zip", b"not really a zip").status_code == 422


def test_unknown_file_id_returns_404():
    assert client.get("/api/files/doesnotexist/").status_code == 404
