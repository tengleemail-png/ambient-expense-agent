import asyncio
import base64
import logging
import os
import uuid
from pathlib import Path

import google.auth
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.runners import InMemoryRunner
from google.genai import types

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.agent import app as agent_app  # noqa: E402

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

runner = InMemoryRunner(app=agent_app)

app = FastAPI(title="ambient-expense-agent")

_pending: dict[str, dict] = {}
_resume_events: dict[str, asyncio.Event] = {}
_resume_responses: dict[str, str] = {}
_background_tasks: set[asyncio.Task] = set()


def _normalize_subscription(subscription: str) -> str:
    return subscription.rsplit("/", 1)[-1] if "/" in subscription else subscription


async def _run_workflow(
    session_id: str, user_id: str, text: str, msg_id: str, sub_name: str
):
    try:
        await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id=user_id,
            session_id=session_id,
        )
    except AlreadyExistsError:
        pass

    content = types.Content(
        role="user", parts=[types.Part.from_text(text=text)]
    )
    final_output = None
    invocation_id = None

    for attempt in range(5):
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
            invocation_id=invocation_id,
        ):
            if event.output is not None:
                final_output = event.output
                invocation_id = event.invocation_id

        # Check if session has pending interrupts
        session = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        pending_events = [
            e for e in session.events
            if e.author != "user" and e.output is None
            and e.actions.requested_auth_configs is not None
        ]

        if not pending_events:
            break

        # Park until a human responds via POST /respond/{session_id}
        event = asyncio.Event()
        _resume_events[session_id] = event
        _pending[session_id] = {
            "session_id": session_id,
            "sub_name": sub_name,
            "msg_id": msg_id,
            "attempt": attempt,
        }
        logger.info(
            "HITL waiting [%s] session=%s attempt=%d",
            msg_id[:8], session_id, attempt,
        )
        await event.wait()

        # Build resume content from stored response
        resp_text = _resume_responses.pop(session_id, "no")
        content = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=pe.id if hasattr(pe, "id") else "",
                        name="",
                        response={"response": resp_text},
                    )
                )
                for pe in pending_events
            ],
        )

    _pending.pop(session_id, None)
    _resume_events.pop(session_id, None)

    logger.info(
        "Done [%s] session=%s output=%s",
        msg_id[:8], session_id, final_output,
    )


@app.post("/")
async def handle_pubsub(request: Request):
    envelope = await request.json()
    message = envelope.get("message", {})
    data_b64 = message.get("data", "")
    subscription = envelope.get("subscription", "unknown")
    sub_name = _normalize_subscription(subscription)

    try:
        data = base64.b64decode(data_b64).decode("utf-8")
    except Exception:
        logger.error("Failed to decode message data")
        return {"status": "error", "error": "invalid data"}

    msg_id = message.get("messageId", uuid.uuid4().hex)
    session_id = f"{sub_name}--{msg_id[:12]}"
    user_id = sub_name

    logger.info("Received [%s] sub=%s session=%s", msg_id[:8], sub_name, session_id)

    task = asyncio.create_task(
        _run_workflow(session_id, user_id, data, msg_id, sub_name)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"status": "accepted", "session_id": session_id}


@app.get("/pending")
async def list_pending():
    return {
        "count": len(_pending),
        "items": list(_pending.values()),
    }


@app.post("/respond/{session_id}")
async def respond_hitl(session_id: str, request: Request):
    body = await request.json()
    resp_text = body.get("response", "no")
    _resume_responses[session_id] = resp_text
    event = _resume_events.pop(session_id, None)
    if event:
        event.set()
        return {"status": "resumed", "session_id": session_id, "response": resp_text}
    return {"status": "not_found", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
