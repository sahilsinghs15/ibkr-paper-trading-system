"""Broker status endpoint router."""

from fastapi import APIRouter, Request

from app.schemas.api_schemas import BrokerStatusResponse

router = APIRouter(tags=["broker"])


@router.get(
    "/broker/status",
    response_model=BrokerStatusResponse,
    summary="Get broker connection status",
)
async def get_broker_status(request: Request) -> BrokerStatusResponse:
    """Retrieve the current broker connection status and type."""
    broker = getattr(request.app.state, "broker", None)
    broker_mode = getattr(request.app.state, "broker_mode", "unknown")
    
    if broker is None:
        return BrokerStatusResponse(
            broker_mode=broker_mode,
            connected=False,
            broker_type="None",
        )
        
    broker_type = broker.__class__.__name__
    
    # Check connection status based on broker type
    connected = False
    if broker_type == "MockBroker":
        connected = broker.status.name == "CONNECTED"
    elif broker_type == "IBKRBroker":
        connected = broker._client.is_connected()
        
    return BrokerStatusResponse(
        broker_mode=broker_mode,
        connected=connected,
        broker_type=broker_type,
    )
