from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services import _get_bo_type_by_barcode, _resolve_image_path

router = APIRouter()


@router.get("/aoi/image")
def get_board_image(
    barcode: str = Query(..., min_length=1, description="Barcode"),
) -> FileResponse:
    board_type = _get_bo_type_by_barcode(barcode)
    image_path = _resolve_image_path(board_type)
    if image_path is None:
        raise HTTPException(status_code=404, detail=f"Image not found: {board_type}")
    return FileResponse(image_path, media_type="image/jpeg")
