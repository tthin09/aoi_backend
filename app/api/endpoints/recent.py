from typing import List

from fastapi import APIRouter, HTTPException, Query

from app.services import _get_recent_failed_barcodes, FailedBoard

router = APIRouter()


@router.get("/aoi/failed/recent", response_model=List[FailedBoard])
def get_recent_failed_barcodes(
    limit: int = Query(10, ge=1, le=50, description="Number of barcodes to return"),
) -> List[FailedBoard]:
    try:
        return _get_recent_failed_barcodes(limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SQL Server error: {exc}")
