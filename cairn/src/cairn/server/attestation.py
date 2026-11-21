"""Hardware evidence for the machine Cairn runs in, for checking from the owner's browser.

Cairn asks the confidential machine's guest agent, over a local socket, for a quote that
includes a value the browser chose, and for the description of what the machine was
started with. Cairn does not judge the evidence: the browser sends it to a verifier.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from cairn.server import hosted

router = APIRouter(tags=["attestation"])

DSTACK_SOCKET_ENV = "DSTACK_SOCKET"
_DEFAULT_SOCKET = "/var/run/dstack.sock"
_MAX_RESPONSE_BYTES = 32 << 20
_TIMEOUT_SECONDS = 20
_NO_STORE = {"Cache-Control": "no-store"}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("dstack", timeout=_TIMEOUT_SECONDS)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_TIMEOUT_SECONDS)
        sock.connect(self._path)
        self.sock = sock


def guest_agent(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One call to the machine's guest agent. Only the three read-only methods are used."""

    if method not in {"GetQuote", "Info", "Attest"}:
        raise ValueError("unsupported guest agent method")
    connection = _UnixConnection(os.environ.get(DSTACK_SOCKET_ENV, "").strip() or _DEFAULT_SOCKET)
    try:
        connection.request("POST", "/" + method, body=json.dumps(payload), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise HTTPException(502, "this machine's hardware evidence is not available") from exc
    finally:
        connection.close()
    if len(body) > _MAX_RESPONSE_BYTES:
        raise HTTPException(502, "the hardware evidence is too large")
    if response.status != 200:
        raise HTTPException(502, f"the guest agent answered {method} with HTTP {response.status}")
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise HTTPException(502, f"the guest agent answered {method} with malformed JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(502, f"the guest agent answered {method} with malformed JSON")
    if value.get("error"):
        raise HTTPException(502, f"the guest agent refused {method}")
    return value


@router.get("/attestation")
def attestation(report_data: str = Query("", alias="reportData", max_length=130)) -> JSONResponse:
    """A fresh quote bound to the caller's value (hex, at most 64 bytes)."""

    value = report_data.removeprefix("0x") or "0" * 128
    if len(value) > 128 or len(value) % 2:
        raise HTTPException(400, "reportData must be an even-length hex string of at most 64 bytes")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise HTTPException(400, "reportData must be hexadecimal") from exc
    evidence = guest_agent("GetQuote", {"report_data": value})
    try:
        vendor = guest_agent("Info", {}).get("cloud_vendor")
    except HTTPException:
        vendor = None
    if vendor == "Google":
        full = guest_agent("Attest", {"report_data": value}).get("attestation")
        if not full:
            raise HTTPException(502, "the guest agent returned no attestation evidence")
        evidence["attestation"] = full
    return JSONResponse(evidence, headers=_NO_STORE)


@router.get("/info")
def info() -> JSONResponse:
    """What this machine was started with, as the guest agent reports it."""

    return JSONResponse(guest_agent("Info", {}), headers=_NO_STORE)


@router.get("/attestation/config")
def attestation_config() -> JSONResponse:
    return JSONResponse({"verifier_url": hosted.verifier_url()}, headers=_NO_STORE)
