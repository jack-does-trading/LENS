from __future__ import annotations


class JSONExtractionError(RuntimeError):
    """Raised when no balanced JSON object can be found in a string."""


def extract_json_object(raw: str) -> str:
    """Strip markdown fences / stray prose around a single JSON object via
    balanced-brace scanning. Deliberately does not repair malformed JSON
    syntax inside the object -- same boundary tools/local_extraction draws.

    Shared by app/synthesis.py's Step B parse and app/verification.py's
    Step C entailment check -- both parse a single JSON object out of raw
    LLM output. Ollama's client forces `format="json"` so its output is
    reliably a bare object, but GroqLLMClient has no such guarantee: Groq
    routinely wraps the object in ```json fences or a sentence of prose
    ("Here's the verdict:"), so a plain `json.loads(raw)` fails every time
    on that provider. Both call sites need this extraction, not just
    synthesis -- verification skipping it was a real bug (it forced every
    entailment check to fail-closed under Groq, so every analysis silently
    fell back to the template regardless of whether the model's answer was
    actually fine).
    """
    start = raw.find("{")
    if start == -1:
        raise JSONExtractionError("no JSON object found in model response")
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    raise JSONExtractionError("unbalanced braces in model response")
