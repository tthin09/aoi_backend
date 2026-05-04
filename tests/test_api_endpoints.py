import os
import sys
import json
import pytest
import importlib
from fastapi.testclient import TestClient

# Try several import paths for the application module so tests work whether
# the repository root layout is "repo root" or a nested `backend/` folder.
app = None
candidates = ["main", "backend.main", "aoi_backend.main"]
for cand in candidates:
    try:
        mod = importlib.import_module(cand)
        app = getattr(mod, "app", None)
        if app is not None:
            break
    except Exception:
        continue

if app is None:
    # As a last resort, add repository parent to sys.path and try again
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    for cand in candidates:
        try:
            mod = importlib.import_module(cand)
            app = getattr(mod, "app", None)
            if app is not None:
                break
        except Exception:
            continue

if app is None:
    pytest.exit("Could not import FastAPI `app` from main module. Check project layout.")

client = TestClient(app)

BARCODE = "IWHT04730192"


def _skip_if_no_env(var_name: str):
    if not os.getenv(var_name):
        pytest.skip(f"Set {var_name} to run this test")


def test_get_recent_failed_barcodes_min_items():
    resp = client.get("/aoi/failed/recent?limit=10")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 7


def test_image_endpoint_returns_jpg():
    barcode = BARCODE
    if not barcode:
        pytest.skip("Set TEST_BARCODE_IMAGE to test /aoi/image")
    resp = client.get(f"/aoi/image?barcode={barcode}")
    assert resp.status_code == 200, resp.text
    content_type = resp.headers.get("content-type", "")
    assert "image" in content_type.lower()
    assert len(resp.content) > 0


def test_part_position_matches_ground_truth():
    barcode = BARCODE
    if not barcode:
        pytest.skip("Set TEST_BARCODE_PART_POSITION to test /aoi/part-position")

    resp = client.get(f"/aoi/part-position?barcode={barcode}")
    assert resp.status_code == 200, resp.text

    ct = resp.headers.get("content-type", "")

    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
    json_fixture = os.path.join(fixtures_dir, "part_position_ground_truth.json")
    csv_fixture = os.path.join(fixtures_dir, "part_position_ground_truth.csv")

    if "application/json" in ct:
        if not os.path.exists(json_fixture):
            pytest.skip("Add tests/fixtures/part_position_ground_truth.json to enable comparison")
        expected = json.load(open(json_fixture, "r", encoding="utf-8"))
        assert resp.json() == expected
    elif "csv" in ct or "text/plain" in ct:
        if not os.path.exists(csv_fixture):
            pytest.skip("Add tests/fixtures/part_position_ground_truth.csv to enable comparison")
        expected = open(csv_fixture, "r", encoding="utf-8").read().strip()
        assert resp.text.strip() == expected
    else:
        pytest.fail(f"Unexpected content-type for part-position: {ct}")


def test_failed_endpoint_matches_ground_truth():
    barcode = BARCODE
    if not barcode:
        pytest.skip("Set TEST_BARCODE_FAILED to test /aoi/failed")

    resp = client.get(f"/aoi/failed?barcode={barcode}")
    assert resp.status_code == 200, resp.text

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "failed_ground_truth.json")
    if not os.path.exists(fixture):
        pytest.skip("Add tests/fixtures/failed_ground_truth.json to enable comparison")

    expected = json.load(open(fixture, "r", encoding="utf-8"))
    # The endpoint returns {"items": [...], "message": "..."}
    data = resp.json()
    assert "items" in data
    assert data["items"] == expected["items"]
