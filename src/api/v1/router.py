from fastapi import APIRouter, status

from src.api.v1 import documents, health, query
from src.schemas.common import ErrorResponse

api_router = APIRouter(
    prefix="/api/v1",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "Validation Error",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Internal Server Error",
        },
    },
)
api_router.include_router(health.router)
api_router.include_router(documents.router)
api_router.include_router(query.router)
