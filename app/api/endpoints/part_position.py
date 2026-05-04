from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services import (
    _get_bo_type_by_barcode,
    _resolve_part_position_csv,
    _resolve_part_position_json_path,
)

router = APIRouter()


@router.get("/aoi/part-position")
def get_part_position(
    barcode: str = Query(..., min_length=1, description="Barcode"),
) -> FileResponse:
    board_type = _get_bo_type_by_barcode(barcode)

    # Prefer CSV when available, but fall back to JSON files in the json/ directory.
    csv_path = _resolve_part_position_csv(board_type)
    if csv_path:
        return FileResponse(csv_path, media_type="text/csv")

    json_path = _resolve_part_position_json_path(board_type)
    if json_path:
        return FileResponse(json_path, media_type="application/json")

    raise HTTPException(
        status_code=404,
        detail=f"Part position not found: {board_type}",
    )
