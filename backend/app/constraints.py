import re

CORE_THESIS_MAX_WORDS = 150
PRINCIPLE_SUMMARY_MAX_WORDS = 200
MAX_QUOTE_WORDS = 15
MAX_QUOTES_PER_PRINCIPLE = 1

# Matches text within straight or curly double quotes. Mirrors
# tools/local_extraction/src/local_extraction/validation.py so both the local
# extraction tool and the ingestion endpoint enforce the same quote rule
# (architecture §2/§3): reimplemented locally rather than imported, since the
# local tool must stay fully standalone from this app.
_QUOTE_PATTERN = re.compile(r'["“]([^"”]+)["”]')


def count_words(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return len(stripped.split())


def find_quotes(text: str) -> list[str]:
    return _QUOTE_PATTERN.findall(text)
