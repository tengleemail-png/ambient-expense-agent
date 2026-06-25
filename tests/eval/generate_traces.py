#!/usr/bin/env python3
"""Run expense scenarios through the ADK workflow and produce grade-ready traces."""

import asyncio
import json
import os

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

import app.agent as agent_module
from app.agent import app

DATASET_PATH = "tests/eval/datasets/basic-dataset.json"
OUTPUT_DIR = "artifacts/traces"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "generated_traces.json")
AGENT_NAME = "ambient_expense_agent"


def _reset_state():
    agent_module._expenses.clear()
    agent_module._next_id = 1


def _output_to_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return json.dumps(output, default=str)
    if hasattr(output, "parts"):
        texts = [p.text for p in output.parts if p.text]
        return " ".join(texts) if texts else str(output)
    return str(output)


def _make_trace_events(collected, user_text, final_record=None):
    events = [
        {
            "author": "user",
            "content": {"role": "user", "parts": [{"text": user_text}]},
        }
    ]

    for ev in collected:
        if ev.output is None:
            continue

        text = _output_to_text(ev.output)
        author = ev.author if ev.author else AGENT_NAME

        # Include routing info when present
        route = None
        if ev.actions and ev.actions.route:
            route = ev.actions.route

        parts_text = text
        if route:
            parts_text = f"[route: {route}] {text}"

        events.append({
            "author": author,
            "content": {"role": "model", "parts": [{"text": parts_text}]},
        })

    if final_record:
        events.append({
            "author": "system",
            "content": {
                "role": "model",
                "parts": [{"text": json.dumps(final_record, default=str)}],
            },
        })

    return events


def _resolve_hitl(raw_output: str) -> str:
    for record in list(agent_module._expenses):
        if record["status"] == "pending_approval":
            record["status"] = "approved"
            return agent_module.format_response(record)
        if record["status"] == "pending_security_review":
            record["status"] = "not_approved"
            return agent_module.format_response(record)
    return raw_output


def _get_record():
    for record in list(agent_module._expenses):
        return record
    return None


def _resolve_hitl_summary():
    """Return a text summary of what HITL decision was made."""
    for record in list(agent_module._expenses):
        status = record.get("status", "")
        amount = record.get("amount", 0)
        if status == "approved" and amount > 1000:
            return f"HITL: Human approved this expense (amount ${amount:.2f} > $1000 threshold)"
        if status == "not_approved" and record.get("security_flag"):
            return (
                f"HITL: Security review rejected this expense "
                f"(security_flag={record['security_flag']})"
            )
        if status == "not_approved":
            return f"HITL: Human rejected this expense"
    return None


async def _run_one(case, runner):
    prompt = case["prompt"]
    user_text = prompt["parts"][0]["text"]
    case_id = case["eval_case_id"]
    session_id = f"eval-{case_id}"
    user_id = "eval-user"

    _reset_state()

    try:
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
    except AlreadyExistsError:
        pass

    content = genai_types.Content(
        role="user", parts=[genai_types.Part.from_text(text=user_text)]
    )

    collected = []
    raw_output = None
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content
    ):
        collected.append(event)
        if event.output is not None:
            if isinstance(event.output, str):
                raw_output = event.output
            elif hasattr(event.output, "parts"):
                texts = [p.text for p in event.output.parts if p.text]
                raw_output = " ".join(texts) if texts else str(event.output)
            else:
                raw_output = str(event.output)

    record_before = _get_record()
    final_output = _resolve_hitl(raw_output or "")
    record_after = _get_record()

    trace_events = _make_trace_events(
        collected, user_text, final_record=record_after
    )

    # Add HITL summary after the events
    hitl_summary = _resolve_hitl_summary()
    if hitl_summary:
        trace_events.append({
            "author": "system",
            "content": {"role": "model", "parts": [{"text": hitl_summary}]},
        })

    return {
        "eval_case_id": case_id,
        "agent_data": {
            "agents": {
                AGENT_NAME: {
                    "agent_id": AGENT_NAME,
                    "instruction": "Expense management agent",
                }
            },
            "turns": [{"turn_index": 0, "events": trace_events}],
        },
        "response": {"role": "model", "parts": [{"text": final_output}]},
    }


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DATASET_PATH) as f:
        dataset = json.load(f)

    runner = InMemoryRunner(app=app)
    results = []
    for case in dataset["eval_cases"]:
        cid = case["eval_case_id"]
        print(f"Running [{cid}]...", end=" ", flush=True)
        trace = await _run_one(case, runner)
        results.append(trace)
        out = trace["response"]["parts"][0]["text"]
        print(f"  {out}")

    with open(OUTPUT_PATH, "w") as f:
        json.dump({"eval_cases": results}, f, indent=2, default=str)
    print(f"\nWrote {len(results)} traces to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
