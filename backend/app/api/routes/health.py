"""Health check endpoint router."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Verify app health", response_model=dict[str, str])
async def get_health() -> dict[str, str]:
    """Verify that the backend application is alive and responsive."""
    return {"status": "ok"}
