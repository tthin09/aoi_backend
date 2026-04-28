from typing import List, Optional

import json
import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="AOI Backend")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGE_DIR = os.path.join(DATA_DIR, "image")
PART_POSITION_DIR = os.path.join(DATA_DIR, "part_position")
ALIAS_PATH = os.path.join(PART_POSITION_DIR, "alias.json")


class AOIRecord(BaseModel):
    pkg_type: Optional[str]
    uname: Optional[str]
    label: Optional[str]
    defect: Optional[str]


def _build_conn_info() -> str:
    missing = [
        name
        for name in ("HOST_NAME", "DB_NAME", "USER", "PASSWORD", "PORT")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return (
        f"host={os.getenv('HOST_NAME')} "
        f"dbname={os.getenv('DB_NAME')} "
        f"user={os.getenv('USER')} "
        f"password={os.getenv('PASSWORD')} "
        f"port={os.getenv('PORT')}"
    )


def _resolve_part_position_csv(board_type: str) -> Optional[str]:
    direct_path = os.path.join(PART_POSITION_DIR, f"{board_type}.csv")
    if os.path.isfile(direct_path):
        return direct_path

    if not os.path.isfile(ALIAS_PATH):
        return None

    with open(ALIAS_PATH, "r", encoding="utf-8") as handle:
        aliases = json.load(handle)

    for entry in aliases:
        if entry.get("board_type") != board_type:
            continue
        for candidate in entry.get("part_position", []):
            name = candidate if candidate.lower().endswith(".csv") else f"{candidate}.csv"
            alias_path = os.path.join(PART_POSITION_DIR, name)
            if os.path.isfile(alias_path):
                return alias_path

    return None


@app.get("/aoi/failed", response_model=List[AOIRecord])
def get_failed_by_barcode(
    barcode: str = Query(..., min_length=1, description="Barcode to filter by"),
) -> List[AOIRecord]:
    try:
        conn_info = _build_conn_info()
        fail_query = (
            "SELECT pkg_type, uname, label, defect "
            "FROM aoi_metadata "
            "WHERE barcode = %s AND lower(label) = 'fail'"
        )
        with psycopg.connect(conn_info) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(fail_query, (barcode,))
                rows = cur.fetchall()
                if not rows:
                    # Distinguish between missing barcode vs. no failed items.
                    cur.execute(
                        "SELECT 1 FROM aoi_metadata WHERE barcode = %s LIMIT 1",
                        (barcode,),
                    )
                    if cur.fetchone() is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Barcode not found: {barcode}",
                        )
        return rows
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@app.get("/aoi/image")
def get_board_image(
    board_type: str = Query(..., min_length=1, description="Board type"),
) -> FileResponse:
    image_path = os.path.join(IMAGE_DIR, f"{board_type}.jpg")
    if not os.path.isfile(image_path):
        raise HTTPException(status_code=404, detail=f"Image not found: {board_type}")
    return FileResponse(image_path, media_type="image/jpeg")


@app.get("/aoi/part-position")
def get_part_position(
    board_type: str = Query(..., min_length=1, description="Board type"),
) -> FileResponse:
    csv_path = _resolve_part_position_csv(board_type)
    if csv_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"Part position not found: {board_type}",
        )
    return FileResponse(csv_path, media_type="text/csv")
