"""Health check endpoint router (liveness vs readiness)."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health", summary="Verify app health (liveness)", response_model=dict[str, str])
async def get_health() -> dict[str, str]:
    """Liveness: process is alive."""
    return {"status": "ok"}


@router.get("/health/live", summary="Liveness", response_model=dict[str, str])
async def get_liveness() -> dict[str, str]:
    """Liveness probe — always ok if process responds."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness", response_model=dict[str, str])
async def get_readiness(request: Request) -> dict[str, str]:
    """Readiness: DB + TWS connectivity if available."""
    # webhook ingest: check session_factory DB
    # trading backend: check DB + TWS
    try:
        factory = getattr(request.app.state, "session_factory", None)
        if factory is not None:
            from sqlalchemy import text

            async with factory() as session:
                await session.execute(text("SELECT 1"))
        # trading only: check TWS client
        client = getattr(request.app.state, "client", None) or getattr(request.app.state, "tws_client", None)
        if client is not None and hasattr(client, "is_connected"):
            # if disconnected, still return degraded but not 500 — readiness reflects it
            if not client.is_connected():
                return {"status": "degraded", "reason": "tws_disconnected"}
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "reason": str(exc)[:200]}
