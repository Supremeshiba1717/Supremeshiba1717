"""The ONLY two LLM call sites in the app (cost control lives here).

1. parse_availability_batch() — one batched call for ALL raw replies in a cycle.
2. parse_manager_reply() — one call per manager message that isn't a plain yes.

Everything is constrained to JSON output. On any failure we degrade gracefully
(flag for manual review / ask the manager to clarify) rather than crash or
silently drop an employee.

⚠️ BILLING NOTE: these are metered Anthropic API calls, NOT covered by a Claude
consumer subscription. At ~10-20 employees and 2 calls/week the cost is a few
cents/week, but it is real pay-per-token usage.
"""

import json
import logging

from anthropic import Anthropic

from app.config import settings

log = logging.getLogger("llm")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _extract_json(text: str):
    """Pull the first JSON object/array out of a model response."""
    text = text.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# --- Call site 1: availability parsing (batched) ----------------------------


def parse_availability_batch(
    replies: list[dict],
) -> dict[int, dict]:
    """Parse many employees' free-text availability in ONE call.

    Input: [{"employee_id": 1, "name": "Alex", "text": "Mon Wed Fri"}, ...]
    Output: {employee_id: {"days": [...], "caveats": str, "confident": bool}}

    Any employee the model can't confidently parse comes back confident=False
    so the caller can mark them needs_review — the batch never fails wholesale.
    """
    if not replies:
        return {}

    # Build a compact, numbered prompt so the model returns keyed results.
    lines = [
        f'{r["employee_id"]}: "{r["text"]}" (from {r.get("name", "?")})'
        for r in replies
    ]
    prompt = (
        "You parse bike-shop employees' weekly availability texts into "
        "structured data. Days of the week are exactly: "
        f"{', '.join(DAYS)}.\n\n"
        "For each numbered reply below, determine which days the employee CAN "
        "work. Interpret natural phrasing (e.g. 'weekdays' = Mon-Fri, "
        "'off Tuesday' means every day they normally would EXCEPT Tuesday — but "
        "if you can't tell their baseline, only include days explicitly "
        "mentioned as available). Capture anything conditional in 'caveats'.\n\n"
        "Return ONLY a JSON object keyed by the employee id (as a string), each "
        'value: {"days": [list of day codes], "caveats": "text or empty", '
        '"confident": true/false}. Set confident=false if the text is empty, '
        "unintelligible, or you are genuinely unsure.\n\n"
        "Replies:\n" + "\n".join(lines)
    )

    try:
        resp = _get_client().messages.create(
            model=settings.llm_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        parsed = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        log.exception("Availability batch parse failed: %s", e)
        # Total failure → mark everyone needs_review, keep raw text intact.
        return {
            r["employee_id"]: {"days": [], "caveats": "", "confident": False}
            for r in replies
        }

    # Normalize keys to int and validate day codes.
    result: dict[int, dict] = {}
    for r in replies:
        eid = r["employee_id"]
        entry = parsed.get(str(eid)) or parsed.get(eid)
        if not isinstance(entry, dict):
            result[eid] = {"days": [], "caveats": "", "confident": False}
            continue
        days = [d for d in entry.get("days", []) if d in DAYS]
        result[eid] = {
            "days": days,
            "caveats": entry.get("caveats", "") or "",
            "confident": bool(entry.get("confident", False)) and bool(days),
        }
    return result


# --- Call site 2: manager reply parsing -------------------------------------


def parse_manager_reply(text: str) -> dict:
    """Classify Steve's reply to a draft.

    Returns one of:
      {"action": "approve"}
      {"action": "revise", "changes": [
          {"day": "Thu", "direction": "increase"|"decrease",
           "count": int, "employee": str|null}]}
      {"action": "unclear"}   # caller should ask him to rephrase

    A cheap regex catches plain approvals BEFORE the LLM, so many manager
    replies cost nothing.
    """
    stripped = text.strip().lower()
    approve_words = {"approved", "approve", "yes", "yep", "looks good", "lgtm",
                     "good", "ok", "okay", "👍", "send it"}
    if stripped in approve_words:
        return {"action": "approve"}

    prompt = (
        "You interpret a bike-shop manager's reply to a proposed weekly staff "
        "schedule. Days are exactly: " + ", ".join(DAYS) + ".\n\n"
        "Classify the reply into JSON. If they approve it as-is, return "
        '{"action":"approve"}. If they request changes, return '
        '{"action":"revise","changes":[{"day":"<day code>",'
        '"direction":"increase" or "decrease","count":<integer, default 1>,'
        '"employee":"<name if they named someone, else null>"}]}. '
        'If you cannot tell what they want, return {"action":"unclear"}.\n\n'
        "Reply: " + json.dumps(text)
    )

    try:
        resp = _get_client().messages.create(
            model=settings.llm_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = _extract_json(resp.content[0].text)
    except Exception as e:  # noqa: BLE001
        log.exception("Manager reply parse failed: %s", e)
        return {"action": "unclear"}

    if parsed.get("action") not in {"approve", "revise", "unclear"}:
        return {"action": "unclear"}
    return parsed
