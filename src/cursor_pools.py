"""Identify the Cursor allowance used by a selected model."""

import re

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def model_pool(model):
    model = str(model or "").strip().lower()
    if not model:
        return None
    if model == "auto" or re.match(r"^(?:grok-4\.\d|composer(?:-|$))", model):
        return "cr"
    return "ex"


def argv_pool(argv):
    for i, word in enumerate(argv):
        if word == "--model" and i + 1 < len(argv):
            return model_pool(argv[i + 1])
        if word.startswith("--model="):
            return model_pool(word.split("=", 1)[1])
    return None


def screen_pool(text):
    """Read the native model footer, including an in-session /model change.

    Require its permission label so model names in ordinary chat are ignored.
    Cursor can wrap the context size and effort onto the following line.
    """
    for line in reversed(ANSI.sub("", text).splitlines()):
        if not re.search(r"\s{2,}(?:Run Everything|Ask Every Time|Plan Mode|Auto Run|Default)(?:\s|$)", line, re.I):
            continue
        model = line.strip()
        if re.match(r"^(?:Cursor\s+)?(?:Grok\s+4\.\d|Composer\s*\d|Auto)(?:\s|$)", model, re.I):
            return "cr"
        if re.match(r"^(?:(?:Claude\s+)?(?:Opus|Sonnet|Haiku)\s+\d|GPT[- ]|Gemini\s+\d|o[134][ -])", model, re.I):
            return "ex"
    return None
