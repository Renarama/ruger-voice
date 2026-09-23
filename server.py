#!/usr/bin/env python3
"""
Rutger – bro mellan 46elks (websocket-samtal) och ElevenLabs Agents.
Uppringare <-> 46elks <-> denna server <-> ElevenLabs-agenten "Rutger"

Miljövariabler (Fly secrets):
  ELEVENLABS_API_KEY, RUTGER_AGENT_ID,
  SUPABASE_URL (valfri), SUPABASE_SERVICE_ROLE_KEY (valfri), PORT (default 8080)
"""

import asyncio
import json
import logging
import os

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
AGENT_ID = os.environ["RUTGER_AGENT_ID"]
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

# ElevenLabs-format -> 46elks-format
FORMAT_MAP = {
    "pcm_8000": "pcm_8000",
    "pcm_16000": "pcm_16000",
    "pcm_24000": "pcm_24000",
    "ulaw_8000": "ulaw",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rutger")


async def get_signed_url() -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
            params={"agent_id": AGENT_ID},
            headers={"xi-api-key": ELEVENLABS_API_KEY},
        )
        r.raise_for_status()
        return r.json()["signed_url"]


async def log_call(call_id: str, fields: dict) -> None:
    """Uppdaterar incoming_calls-raden. Får aldrig fälla ett samtal."""
    if not (SUPABASE_URL and SUPABASE_KEY and call_id):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.patch(
                f"{SUPABASE_URL}/rest/v1/incoming_calls",
                params={"call_id": f"eq.{call_id}"},
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=fields,
            )
            if r.status_code >= 300:
                log.warning("Supabase-loggning misslyckades (%s): %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Supabase-loggning fel: %s", e)


async def handle_call(elks_ws):
    # 1. hello från 46elks
    try:
        hello = json.loads(await asyncio.wait_for(elks_ws.recv(), timeout=10))
    except Exception as e:
        log.error("Fick inget hello: %s", e)
        return
    if hello.get("t") != "hello":
        log.error("Förväntade hello, fick: %s", hello)
        return

    call_id = hello.get("callid", "")
    caller = hello.get("from", "")
    log.info("Samtal %s från %s till %s", call_id, caller, hello.get("to"))

    # 2. Anslut till ElevenLabs
    try:
        signed_url = await get_signed_url()
    except Exception as e:
        log.error("Kunde inte hämta signed URL från ElevenLabs: %s", e)
        await elks_ws.send(json.dumps({"t": "bye"}))
        return

    async with ws_connect(signed_url, max_size=None) as el_ws:
        await el_ws.send(json.dumps({
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {"caller_phone": caller, "call_id": call_id},
        }))

        conversation_id = None
        in_fmt = out_fmt = "pcm_16000"
        while True:
            msg = json.loads(await el_ws.recv())
            if msg.get("type") == "conversation_initiation_metadata":
                meta = msg.get("conversation_initiation_metadata_event", {})
                conversation_id = meta.get("conversation_id")
                in_fmt = meta.get("user_input_audio_format", in_fmt)
                out_fmt = meta.get("agent_output_audio_format", out_fmt)
                break
            if msg.get("type") == "ping":
                await el_ws.send(json.dumps({"type": "pong", "event_id": msg["ping_event"]["event_id"]}))

        if in_fmt not in FORMAT_MAP or out_fmt not in FORMAT_MAP:
            log.error("Ljudformat som 46elks inte stöder: in=%s ut=%s", in_fmt, out_fmt)
            await elks_ws.send(json.dumps({"t": "bye"}))
            return

        log.info("Konversation %s (in=%s, ut=%s)", conversation_id, in_fmt, out_fmt)
        asyncio.create_task(log_call(call_id, {
            "handled_by": "rutger",
            "elevenlabs_conversation_id": conversation_id,
        }))

        # 3. Ljudformat till 46elks
        await elks_ws.send(json.dumps({"t": "sending", "format": FORMAT_MAP[out_fmt]}))
        await elks_ws.send(json.dumps({"t": "listening", "format": FORMAT_MAP[in_fmt]}))

        # 4a. Uppringare -> ElevenLabs
        async def elks_to_el():
            async for raw in elks_ws:
                m = json.loads(raw)
                t = m.get("t")
                if t == "audio":
                    await el_ws.send(json.dumps({"user_audio_chunk": m["data"]}))
                elif t == "bye":
                    log.info("46elks avslutade samtalet: %s", m.get("reason"))
                    return

        # 4b. ElevenLabs -> uppringare
        async def el_to_elks():
            async for raw in el_ws:
                m = json.loads(raw)
                t = m.get("type")
                if t == "audio":
                    await elks_ws.send(json.dumps({"t": "audio", "data": m["audio_event"]["audio_base_64"]}))
                elif t == "interruption":
                    await elks_ws.send(json.dumps({"t": "interrupt"}))
                elif t == "ping":
                    await el_ws.send(json.dumps({"type": "pong", "event_id": m["ping_event"]["event_id"]}))
                elif t == "user_transcript":
                    log.info("Kund: %s", m.get("user_transcription_event", {}).get("user_transcript"))
                elif t == "agent_response":
                    log.info("Rutger: %s", m.get("agent_response_event", {}).get("agent_response"))
            log.info("ElevenLabs avslutade konversationen")

        tasks = [asyncio.create_task(elks_to_el()), asyncio.create_task(el_to_elks())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            try:
                await elks_ws.send(json.dumps({"t": "bye"}))
            except Exception:
                pass
    log.info("Samtal %s klart", call_id)


async def handler(ws):
    try:
        await handle_call(ws)
    except websockets.ConnectionClosed:
        log.info("Anslutning stängd")
    except Exception:
        log.exception("Oväntat fel i samtal")


def process_request(connection, request):
    # Hälsokontroll för Fly
    if request.headers.get("Upgrade", "").lower() != "websocket":
        return Response(200, "OK", Headers([("Content-Type", "text/plain")]), b"Rutger OK\n")
    return None


async def main():
    log.info("Rutger lyssnar på port %d", PORT)
    async with serve(handler, "0.0.0.0", PORT, process_request=process_request, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
