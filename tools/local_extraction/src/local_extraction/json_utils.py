from __future__ import annotations

import re

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


def extract_json_object(text: str) -> str:
    """Strip conversational prose or markdown code fences surrounding a JSON
    object, without altering the JSON content itself.

    Small local models routinely prepend text like "Here is the JSON:" before
    an otherwise-valid object, or wrap it in ```json ... ``` fences, despite
    being told not to. This recovers the JSON payload from that wrapping; it
    does NOT repair malformed JSON syntax inside the object (e.g. a missing
    comma) — that's a genuine model failure, left for the caller to detect
    and retry, not silently patched.
    """
    stripped = text.strip()

    fence_match = _FENCE_PATTERN.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()

    start = stripped.find("{")
    if start == -1:
        return stripped

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return stripped[start : i + 1]

    return stripped
