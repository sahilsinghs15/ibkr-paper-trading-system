"""IBKR Connection Runtime Diagnostic.

Establishes host, port, socket connectivity, client_id, and handshake readiness.
Logs explicit diagnostic report as required by Priority 1.
"""

import socket
import sys
from app.core.config import get_settings
from app.broker.ibkr.tws_client import TWSClient

def diagnose_ibkr_connection() -> dict:
    settings = get_settings()
    host = settings.ibkr_host
    port = settings.ibkr_port
    client_id = settings.ibkr_client_id

    # Test TCP Socket Connectivity to configured port
    sock_connected = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        res = s.connect_ex((host, port))
        sock_connected = (res == 0)
        s.close()
    except Exception:
        sock_connected = False

    # Check alternative common IBKR ports if configured port is closed
    alt_ports = [p for p in [4002, 4001, 7497, 7496] if p != port]
    open_alts = []
    for p in alt_ports:
        try:
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.settimeout(0.5)
            if s2.connect_ex((host, p)) == 0:
                open_alts.append(p)
            s2.close()
        except Exception:
            pass

    # Check TWSClient handshake if socket is open
    api_ready = False
    if sock_connected:
        client = TWSClient()
        try:
            connected = client.connect_and_start(host, port, client_id, timeout=3.0)
            api_ready = connected and client.is_connected()
            if client.is_connected():
                client.disconnect_clean()
        except Exception:
            api_ready = False

    return {
        "host": host,
        "port": port,
        "client_id": client_id,
        "socket_connected": sock_connected,
        "api_ready": api_ready,
        "open_alternative_ports": open_alts,
    }

if __name__ == "__main__":
    diag = diagnose_ibkr_connection()
    print("\n========================================================")
    print("IBKR CONNECTION RUNTIME DIAGNOSTIC")
    print("========================================================\n")
    print(f"IBKR CONNECTION:")
    print(f"host={diag['host']}")
    print(f"port={diag['port']}")
    print(f"client_id={diag['client_id']}")
    print(f"socket_connected={diag['socket_connected']}")
    print(f"api_ready={diag['api_ready']}")
    print(f"open_alternative_ports={diag['open_alternative_ports']}\n")

    if not diag["socket_connected"] and not diag["open_alternative_ports"]:
        print("IB GATEWAY UNAVAILABLE — LIVE TICK VERIFICATION IMPOSSIBLE")
