import asyncio
import base64
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent import _expenses, app as agent_app, format_response

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

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
    ):
        if event.output is not None:
            final_output = event.output

    # Check for pending records needing HITL
    for record in list(_expenses):
        if record["status"] == "pending_approval":
            _pending[session_id] = {
                "session_id": session_id,
                "sub_name": sub_name,
                "msg_id": msg_id,
                "type": "approval",
                "record_id": record["id"],
            }
            logger.info(
                "HITL waiting [%s] session=%s type=approval",
                msg_id[:8], session_id,
            )
            hitl_event = asyncio.Event()
            _resume_events[session_id] = hitl_event
            await hitl_event.wait()

            resp_text = _resume_responses.pop(session_id, "no")
            approved = resp_text.strip().lower() in ("yes", "y", "approve", "approved")
            record["status"] = "approved" if approved else "not_approved"
            final_output = format_response(record)
            break

        elif record["status"] == "pending_security_review":
            _pending[session_id] = {
                "session_id": session_id,
                "sub_name": sub_name,
                "msg_id": msg_id,
                "type": "security_review",
                "record_id": record["id"],
                "approved": False,
            }
            logger.info(
                "HITL waiting [%s] session=%s type=security_review",
                msg_id[:8], session_id,
            )
            hitl_event = asyncio.Event()
            _resume_events[session_id] = hitl_event
            await hitl_event.wait()

            resp_text = _resume_responses.pop(session_id, "no")
            approved = resp_text.strip().lower() in ("yes", "y", "approve", "approved")
            if approved:
                record["status"] = "approved"
                record["security_flag"] = None
            else:
                record["status"] = "not_approved"
                record["security_flag"] = "prompt_injection"
            final_output = format_response(record)
            break

    _pending.pop(session_id, None)
    _resume_events.pop(session_id, None)

    if not isinstance(final_output, str):
        final_output = str(final_output)

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
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        body = await request.json()
        resp_text = body.get("response", "no")
    else:
        form = await request.form()
        resp_text = form.get("response", "no")

    _resume_responses[session_id] = resp_text
    event = _resume_events.pop(session_id, None)
    if event:
        event.set()

    if "json" in content_type:
        return {"status": "resumed", "session_id": session_id, "response": resp_text}
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    rows = ""
    for expense in reversed(_expenses):
        eid = expense["id"]
        amount = f"${expense['amount']:.2f}"
        merchant = expense.get("merchant", "N/A")
        cat = expense.get("category", "")
        d = expense.get("date", "")
        status = expense["status"]

        if status == "pending_approval" or status == "pending_security_review":
            label = "⏳ Pending Approval" if status == "pending_approval" else "⚠️ Security Review"
            btn_class = "approve" if status == "pending_approval" else "review"
            sid = next((s for s, p in _pending.items() if p.get("record_id") == eid), None)
            actions = f"""
                <form method="POST" action="/respond/{sid}" style="display:inline">
                    <input type="hidden" name="response" value="yes">
                    <button class="btn btn-yes">✓ Approve</button>
                </form>
                <form method="POST" action="/respond/{sid}" style="display:inline">
                    <input type="hidden" name="response" value="no">
                    <button class="btn btn-no">✗ Reject</button>
                </form>
            """ if sid else ""
            badge = f'<span class="badge badge-{btn_class}">{label}</span>'
        elif status == "approved":
            badge = '<span class="badge badge-approved">✅ Approved</span>'
            actions = ""
        elif status == "not_approved":
            badge = '<span class="badge badge-rejected">❌ Rejected</span>'
            actions = ""
        else:
            badge = f'<span class="badge">{status}</span>'
            actions = ""

        if expense.get("security_flag"):
            cat = f"🚩 {cat}"

        rows += f"""
        <tr>
            <td>{eid}</td>
            <td>{amount}</td>
            <td>{merchant}</td>
            <td>{cat}</td>
            <td>{d}</td>
            <td>{badge} {actions}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Expense Reports</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f5f6fa; color:#2c3e50; padding:2rem }}
  h1 {{ font-size:1.5rem; margin-bottom:1.5rem }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.08) }}
  th,td {{ text-align:left; padding:12px 16px; border-bottom:1px solid #eee }}
  th {{ background:#f8f9fa; font-weight:600; font-size:.85rem; text-transform:uppercase; color:#6c757d }}
  tr:hover td {{ background:#f8f9fa }}
  .badge {{ display:inline-block; padding:4px 8px; border-radius:4px; font-size:.8rem; font-weight:500 }}
  .badge-approve {{ background:#fff3cd; color:#856404 }}
  .badge-review {{ background:#f8d7da; color:#721c24 }}
  .badge-approved {{ background:#d4edda; color:#155724 }}
  .badge-rejected {{ background:#f8d7da; color:#721c24 }}
  .btn {{ display:inline-block; padding:6px 14px; border:none; border-radius:4px; font-size:.8rem; cursor:pointer; margin-left:4px }}
  .btn-yes {{ background:#28a745; color:#fff }}
  .btn-yes:hover {{ background:#218838 }}
  .btn-no {{ background:#dc3545; color:#fff }}
  .btn-no:hover {{ background:#c82333 }}
  .empty {{ text-align:center; padding:3rem; color:#999 }}
  .info {{ margin-bottom:1rem; font-size:.9rem; color:#666 }}
</style>
</head>
<body>
<h1>📋 Expense Reports</h1>
<div class="info">{len(_expenses)} expense(s) — {len(_pending)} pending review</div>
<table>
<thead><tr>
  <th>ID</th><th>Amount</th><th>Merchant</th><th>Category</th><th>Date</th><th>Status</th>
</tr></thead>
<tbody>
  {rows if rows else '<tr><td colspan="6" class="empty">No expenses yet. Send one via PubSub POST /</td></tr>'}
</tbody>
</table>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
