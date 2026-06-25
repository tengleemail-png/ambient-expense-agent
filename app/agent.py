import os
import re
from collections.abc import AsyncGenerator
from datetime import date
from pathlib import Path

import google.auth
from dotenv import load_dotenv
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.events.request_input import RequestInput
from google.adk.workflow import FunctionNode, RetryConfig, Workflow, node
from google.genai import Client as GenaiClient
from google.genai import types as genai_types
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"


class ExpenseDetails(BaseModel):
    amount: float
    category: str
    date: str
    merchant: str
    description: str = ""


today = date.today().isoformat()

_expenses: list[dict] = []
_next_id: int = 1

# ---------------------------------------------------------------------------
# PII patterns
# ---------------------------------------------------------------------------
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
_AMOUNT_RE = re.compile(r"\$(\d+(?:,\d{3})*(?:\.\d{2})?)")

# ---------------------------------------------------------------------------
# Prompt injection patterns (comprehensive)
# ---------------------------------------------------------------------------
_INJECTION_RE = re.compile(
    "|".join(
        [
            # --- Group A: Instruction override ---
            r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|directives|commands|rules|training)",
            r"forget\s+(all\s+)?(previous|prior|your)\s+(instructions|directives|commands|rules|training|prompt)",
            r"disregard\s+(all\s+)?(previous|prior)\s+(instructions|directives|commands|rules)",
            r"override\s+(instructions|directives|commands|rules|system|settings)",
            r"you\s+are\s+(now\s+|reprogrammed\s+|a\s+free\s+|acting\s+as\s+)",
            r"act\s+as\s+if\s+(you|your)",
            r"new\s+(instructions|directives|commands|rules|prompt|system|task|mission):?",
            r"your\s+(new\s+)?(prompt|instructions|task|mission|role|purpose)\s+(is|are|will be|has been)",
            r"system\s+(override|prompt|message|instruction|directive)",
            r"stop\s+(complying|following|obeying|adhering)",
            r"do\s+not\s+(follow|comply with|obey|adhere to|respect)",
            r"print(ed|ing)?\s+(the\s+)?(instructions|directives|system prompt|rules)",
            r"reveal\s+(your|the|all)\s+(instructions|prompt|system|rules|directives)",
            r"say\s+(your|the)\s+(instructions|prompt|system|rules|directives)",
            r"tell\s+me\s+(all\s+)?(your|the)\s+(instructions|prompt|system|rules|directives|initial|training)",
            r"ignore\s+the\s+(above|previous|prior)\s+(instructions|directives|rules)",
            # --- Group B: Auto-approval bypass ---
            r"auto[\s-]?approve",
            r"mark\s+(as\s+)?(approved|paid|cleared|confirmed)",
            r"skip\s+(all\s+)?(checks|validation|approval|review|verification|audit)",
            r"bypass\s+(all\s+)?(checks|validation|approval|review|verification|audit|security)",
            r"already[\s-]?approved",
            r"pre[\s-]?(approved|authorized)",
            r"consider\s+this\s+(approved|confirmed|valid|cleared)",
            r"do\s+not\s+(check|validate|verify|review|audit|approve)",
            r"no\s+need\s+to\s+(approve|check|validate|review|verify)",
            r"don['\u2019]t\s+(check|validate|verify|review|approve|audit)",
            # --- Group C: Authority escalation ---
            r"I\s+am\s+(the\s+)?(admin|administrator|owner|manager|supervisor|CEO|director)",
            r"I\s+have\s+(the\s+)?(authority|clearance|permission|rights|access)\s+to",
            r"authorized\s+by\s+(management|admin|CEO|director|supervisor|executive)",
            r"this\s+(is|was)\s+(already\s+)?(authorized|approved|cleared|confirmed)\s+(on|by)",
            r"you\s+must\s+(approve|accept|confirm|pass|allow)",
            # --- Group D: Cognitive / redirection ---
            r"repeat\s+(after me|the above|the following|this\s+(100|thousand|forever))",
            r"(output|print|say|display)\s+the\s+word",
            r"tell\s+me\s+.*(instructions|prompt|system|directives)\s+word\s+for\s+word",
            r"ignore\s+.*\s+and\s+(do|say|output|print|return|respond)",
        ]
    ),
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Function nodes
# ---------------------------------------------------------------------------


def _extract_text(content: genai_types.Content) -> str:
    return " ".join(p.text for p in content.parts if p.text)


def classify_expense(ctx: Context, node_input: genai_types.Content) -> Event:
    msg = _extract_text(node_input).lower()
    if any(w in msg for w in ["lunch", "dinner", "coffee", "food", "groceries"]):
        cat = "food"
    elif any(w in msg for w in ["uber", "taxi", "flight", "hotel", "gas", "travel"]):
        cat = "travel"
    elif any(w in msg for w in ["aws", "gcp", "saas", "software", "subscription"]):
        cat = "software"
    elif any(w in msg for w in ["rent", "electric", "water", "internet", "utilities"]):
        cat = "utilities"
    else:
        cat = "other"
    ctx.state["category_hint"] = cat
    ctx.state["today"] = today
    return Event(output=_extract_text(node_input))


def security_check(ctx: Context, node_input: str) -> Event:
    redacted = []

    scrubbed = node_input
    if _SSN_RE.search(scrubbed):
        scrubbed = _SSN_RE.sub("[REDACTED-SSN]", scrubbed)
        redacted.append("ssn")
    if _CC_RE.search(scrubbed):
        scrubbed = _CC_RE.sub("[REDACTED-CC]", scrubbed)
        redacted.append("credit_card")

    match = _INJECTION_RE.search(node_input)
    if match:
        return Event(
            output={
                "original": node_input,
                "scrubbed": scrubbed,
                "redacted_categories": redacted,
                "flagged_pattern": match.group().strip(),
            },
            actions=EventActions(route="flagged"),
        )

    if redacted:
        ctx.state["redacted_categories"] = redacted
    return Event(output=scrubbed, actions=EventActions(route="safe"))


@node(rerun_on_resume=True)
async def security_alert(
    ctx: Context, node_input: dict
) -> AsyncGenerator[Event | RequestInput, None]:
    if "security_review" not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="security_review",
            message=(
                f"⚠️ SECURITY FLAG — Prompt injection detected\n\n"
                f'Matched pattern: "{node_input["flagged_pattern"]}"\n'
                f"Redacted categories: {node_input.get('redacted_categories', [])}\n"
                f"\nOriginal message:\n{node_input['original']}\n"
                f"\nScrubbed message:\n{node_input['scrubbed']}\n"
                f"\nApprove or reject this expense?"
            ),
        )
        return
    resp = ctx.resume_inputs["security_review"]
    if isinstance(resp, dict):
        resp = resp.get("response", str(resp))
    approved = resp.strip().lower() in ("yes", "y", "approve", "approved")
    original = node_input.get("original", "")
    match = _AMOUNT_RE.search(original)
    amount = float(match.group(1).replace(",", "")) if match else 0.0
    yield Event(
        output={
            "amount": amount,
            "category": "security_flagged",
            "date": today,
            "merchant": "N/A",
            "description": original,
            "approved": approved,
            "security_flag": None if approved else "prompt_injection",
        }
    )


def check_amount(ctx: Context, node_input: ExpenseDetails) -> Event:
    ctx.state["expense"] = node_input.model_dump()
    if node_input.amount > 1000:
        return Event(
            output=node_input.model_dump(),
            actions=EventActions(route="needs_approval"),
        )
    return Event(output=node_input.model_dump(), actions=EventActions(route="auto"))


@node(rerun_on_resume=True)
async def approval_gate(
    ctx: Context, node_input: dict
) -> AsyncGenerator[Event | RequestInput, None]:
    if "approve_expense" not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="approve_expense",
            message=(
                f"Expense needs approval:\n"
                f"  Amount:   ${node_input['amount']:.2f}\n"
                f"  Merchant: {node_input['merchant']}\n"
                f"  Category: {node_input['category']}\n"
                f"  Date:     {node_input['date']}\n"
                f"Approve? (yes/no)"
            ),
        )
        return
    resp = ctx.resume_inputs["approve_expense"]
    if isinstance(resp, dict):
        resp = resp.get("response", str(resp))
    node_input["approved"] = resp.strip().lower() in ("yes", "y", "approve", "approved")
    yield Event(output=node_input)


def record_expense(node_input: dict) -> dict:
    global _next_id
    security_flag = node_input.get("security_flag")
    record = {
        "id": f"EXP-{_next_id:04d}",
        "amount": node_input.get("amount", 0.0),
        "category": node_input.get("category", "security_flagged"),
        "date": node_input.get("date", today),
        "merchant": node_input.get("merchant", "N/A"),
        "description": node_input.get("description", ""),
        "status": (
            "not_approved"
            if not node_input.get("approved", True)
            else "approved"
        ),
        "security_flag": security_flag,
    }
    _expenses.append(record)
    _next_id += 1
    return record


def format_response(node_input: dict) -> str:
    if node_input.get("security_flag"):
        return f"⚠️ FLAGGED — {node_input['security_flag']} [{node_input['id']}]"
    status_icon = "✅" if node_input["status"] == "approved" else "❌"
    return (
        f"{status_icon} {node_input['status'].title()} — "
        f"${node_input['amount']:.2f} at {node_input['merchant']} "
        f"[{node_input['id']}]"
    )


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------

_genai_client = GenaiClient()


def capture_details(ctx: Context, node_input: str) -> dict:
    category_hint = ctx.state.get("category_hint", "other")
    prompt = (
        "Extract expense details from this text."
        f" Category hint: {category_hint}."
        f" If no date is given, use: {today}"
        ' Respond with valid JSON matching: {"amount": float,'
        ' "category": str, "date": str (YYYY-MM-DD),'
        ' "merchant": str, "description": str}\n'
        f"Text: {node_input}"
    )
    response = _genai_client.models.generate_content(
        model="gemini-flash-latest",
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    details = ExpenseDetails.model_validate_json(response.text)
    ctx.state["expense"] = details.model_dump()
    return details.model_dump()


capture_node = FunctionNode(
    func=capture_details,
    retry_config=RetryConfig(
        max_attempts=6,
        initial_delay=5.0,
        max_delay=60.0,
        backoff_factor=2.0,
        jitter=2.0,
    ),
)

# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="ambient_expense_agent",
    edges=[
        ("START", classify_expense),
        (classify_expense, security_check),
        (
            security_check,
            {
                "safe": capture_node,
                "flagged": security_alert,
            },
        ),
        (capture_node, check_amount),
        (
            check_amount,
            {
                "needs_approval": approval_gate,
                "auto": record_expense,
            },
        ),
        (approval_gate, record_expense),
        (security_alert, record_expense),
        (record_expense, format_response),
    ],
)

app = App(name="app", root_agent=root_agent)
