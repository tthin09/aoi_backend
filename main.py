from fastapi import FastAPI

# Import routers from endpoint modules
from app.api.endpoints.failed import router as failed_router
from app.api.endpoints.recent import router as recent_router
from app.api.endpoints.image import router as image_router
from app.api.endpoints.part_position import router as part_position_router


app = FastAPI(title="AOI Backend")

app.include_router(failed_router)
app.include_router(recent_router)
app.include_router(image_router)
app.include_router(part_position_router)
