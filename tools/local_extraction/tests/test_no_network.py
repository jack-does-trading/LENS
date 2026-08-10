from __future__ import annotations

import re
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"

_URL_PATTERN = re.compile(r"https?://[^\s\"'()]+")
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Deliberate, single, opt-in exception: GroqModelClient (model_client.py) is
# only ever constructed when the CLI is run with --provider groq, never the
# default. Every other non-localhost URL literal anywhere in source is still
# disallowed by the test below.
_ALLOWED_HOSTS_WITH_OPT_IN_EXCEPTION = _ALLOWED_HOSTS | {"api.groq.com"}


def _hostname_of(url: str) -> str:
    without_scheme = re.sub(r"^https?://", "", url)
    host_and_maybe_port = without_scheme.split("/")[0]
    return host_and_maybe_port.split(":")[0]


def test_no_non_localhost_urls_anywhere_in_source() -> None:
    """This tool defaults to never making a network call off the user's own
    machine (architecture §2/§8, §5). Every http(s) URL literal anywhere in
    the source tree must point at localhost/127.0.0.1/::1, with exactly one
    documented, opt-in exception: Groq's API host, used only when the CLI is
    explicitly run with --provider groq (see GroqModelClient in
    model_client.py). No other remote host is permitted.
    """
    offenders: list[tuple[Path, str]] = []
    for path in SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _URL_PATTERN.finditer(text):
            url = match.group(0)
            if _hostname_of(url) not in _ALLOWED_HOSTS_WITH_OPT_IN_EXCEPTION:
                offenders.append((path, url))

    assert not offenders, f"non-localhost URL(s) found in source: {offenders}"


def test_groq_is_the_only_non_localhost_host_and_only_in_model_client() -> None:
    """Guards the opt-in exception itself from spreading: api.groq.com must
    appear in model_client.py (GroqModelClient) only, not somewhere it could
    be reached by accident/default."""
    offenders: list[Path] = []
    for path in SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _URL_PATTERN.finditer(text):
            if _hostname_of(match.group(0)) == "api.groq.com" and path.name != "model_client.py":
                offenders.append(path)

    assert not offenders, f"api.groq.com referenced outside model_client.py: {offenders}"


def test_at_least_one_localhost_url_exists_as_a_sanity_check() -> None:
    """Guards against the grep pattern silently matching nothing (e.g. if the
    Ollama host constant were ever refactored to build the URL dynamically
    without a literal), which would make the test above vacuously pass.
    """
    found_any = False
    for path in SRC_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _URL_PATTERN.finditer(text):
            if _hostname_of(match.group(0)) in _ALLOWED_HOSTS:
                found_any = True
    assert found_any, "expected at least one localhost URL literal in source (e.g. the Ollama host default)"
