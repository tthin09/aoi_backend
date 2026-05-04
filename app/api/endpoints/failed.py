from typing import List

from fastapi import APIRouter, BackgroundTasks, Query, HTTPException
from fastapi.responses import JSONResponse

import psycopg
from psycopg.rows import dict_row

from app.services import (
    _build_conn_info,
    _attach_positions,
    _dedupe_rows,
    _fetch_failed_from_sqlserver,
    _save_failed_rows_to_postgres,
    DB_CONNECT_TIMEOUT_SECONDS,
    FailedResponse,
)

router = APIRouter()


@router.get("/aoi/failed", response_model=FailedResponse)
def get_failed_by_barcode(
    background_tasks: BackgroundTasks,
    barcode: str = Query(..., min_length=1, description="Barcode to filter by"),
) -> FailedResponse:
    try:
        barcode_exists = False
        conn_info = _build_conn_info()
        fail_query = (
            "SELECT pkg_type, uname, label, defect, bo_type, inspection_time, image_2d "
            "FROM aoi_metadata_1 "
            "WHERE barcode = %s AND lower(label) = 'fail'"
        )
        with psycopg.connect(conn_info, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(fail_query, (barcode,))
                rows = cur.fetchall()
                if rows:
                    return {
                        "items": _attach_positions(_dedupe_rows(rows)),
                        "message": "Found on PostgreSQL",
                    }

                cur.execute(
                    "SELECT 1 FROM aoi_metadata_1 WHERE barcode = %s LIMIT 1",
                    (barcode,),
                )
                if cur.fetchone() is not None:
                    barcode_exists = True

        if barcode_exists:
            return {"items": [], "message": "Found on PostgreSQL"}

        try:
            sql_rows, barcode_found = _fetch_failed_from_sqlserver(barcode)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SQL Server error: {exc}")
        if not barcode_found:
            return {"items": [], "message": f"Barcode not found: {barcode}"}
        if not sql_rows:
            return {"items": [], "message": "Found on SQL Server"}
        if sql_rows:
            background_tasks.add_task(_save_failed_rows_to_postgres, sql_rows)
        response_rows = [
            {
                key: value
                for key, value in row.items()
                if key
                in {
                    "pkg_type",
                    "uname",
                    "label",
                    "defect",
                    "bo_type",
                    "inspection_time",
                    "image_2d",
                }
            }
            for row in sql_rows
        ]
        return {
            "items": _attach_positions(_dedupe_rows(response_rows)),
            "message": "Found on SQL Server",
        }
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
