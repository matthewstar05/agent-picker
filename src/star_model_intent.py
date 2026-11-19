"""Cheap, deterministic extraction of an explicit StarCLI provider or model."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Intent:
    """A user-specified route, or an error when explicit selectors conflict."""

    provider: str = None
    family: str = None
    version: str = None
    effort: str = None
    ultracode: bool = False
    label: str = ""
    model_id: str = None
    error: str = None
    provider_explicit: bool = False


_PROVIDERS = {
    "claude": "Claude", "codex": "Codex", "cursor": "Cursor", "kiro": "Kiro",
    "agy": "Antigravity", "antigravity": "Antigravity", "gemini": "Antigravity",
}
_GROK_BOT = r"grok[ _‐‑–-]*bot"
_PROVIDER_PATTERN = _GROK_BOT + "|" + "|".join(_PROVIDERS)
_GROK_TASK = re.compile(
    r"^(?:please\s+)?(?:check|scan|read|find|look|review|summari[sz]e|list|search|show|open|tell|help|"
    r"fix|update|write|run)\b",
    re.IGNORECASE,
)
_FAMILIES = ("sonnet", "haiku", "astra", "terra", "luna", "opus", "sol", "grok", "gemini", "fable")
_EFFORT_MAP = {
    "xhigh": "xhigh", "x high": "xhigh", "extra high": "xhigh", "ultra": "ultra",
    "maximum": "max", "max": "max", "high": "high", "medium": "medium", "low": "low",
    "minimal": "minimal", "none": "none",
}
_EFFORT_PATTERN = r"(?:x[- ]?high|extra[- ]high|ultra|maximum|max|high|medium|low|minimal|none)"
_MODEL_PATTERN = (
    r"(?P<family>" + "|".join(_FAMILIES) + r")"
    r"(?:[\s._-]*(?P<version>\d{1,2}(?:[._-]\d{1,2})?))?"
    r"(?:[\s._-]+(?P<effort>" + _EFFORT_PATTERN + r"))?"
    r"(?:\s+(?P<uc>uc|ultracode))?"
)
_MODEL = re.compile(r"^\s*" + _MODEL_PATTERN, re.IGNORECASE)
_EXACT = re.compile(
    r"^\s*(?P<id>(?:gpt[- ]?)?\d{1,2}(?:[.-]\d{1,2})?[- ](?:sol|astra|terra|luna)"
    r"|claude[- ](?:opus|sonnet|haiku|fable)[- ]\d{1,2}(?:[.-]\d{1,2})?)"
    r"(?:[- ](?P<effort>xhigh|ultra|max|high|medium|low))?"
    r"(?:\s+(?P<uc>uc|ultracode))?",
    re.IGNORECASE,
)
_EXACT_GEMINI = re.compile(
    r"^\s*gemini[- ](?P<version>\d{1,2}(?:[.-]\d{1,2})?)[- ](?P<variant>flash|pro|ultra)"
    r"(?:[- ](?P<effort>" + _EFFORT_PATTERN + r"))?",
    re.IGNORECASE,
)
_PROVIDER_MODIFIER = re.compile(r"^(?:(?P<effort>" + _EFFORT_PATTERN + r")\b)?"
                               r"\s*(?:(?P<uc>uc|ultracode)\b)?", re.IGNORECASE)
_TOKEN_BOUNDARY = r"(?=\s|[,/:.!?;]|$)"
_PROVIDER_AT_START = re.compile(r"^\s*(?:the\s+)?(" + _PROVIDER_PATTERN + r")" + _TOKEN_BOUNDARY,
                                re.IGNORECASE)
_DIRECTIVE = re.compile(
    r"\b(?:use|using|ask|have|route\s+to|choose|select|run\s+(?:this\s+)?on)\s+",
    re.IGNORECASE,
)
_CONNECTORS = re.compile(r"^(?:please|for|to|and|then|on|about|task|request|uc|ultracode)\b", re.IGNORECASE)


def _clean(prompt):
    """Remove examples and code while retaining a code-formatted selector."""
    prompt = re.sub(r"```.*?```|~~~.*?~~~", " ", prompt, flags=re.DOTALL)

    def selector_text(match):
        before = prompt[max(0, match.start() - 32):match.start()]
        if re.search(r"\b(?:use|using|ask|have|with|route\s+to|choose|select)\s*$", before, re.IGNORECASE):
            content = match.group(2)
            if _candidate(content):
                return content
        return " "

    return re.sub(r"(?<!\w)([`'\"])([^\n]*?)\1", selector_text, prompt)


def provider_name(text):
    name = text.strip().lower()
    if re.fullmatch(_GROK_BOT, name):
        return "Grok Bot"
    return _PROVIDERS.get(name)


def _parse_selector(text):
    """Parse only a provider/model token at the beginning of a candidate phrase."""
    leading = len(text) - len(text.lstrip())
    text = re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)", "", text.strip())
    gemini = _EXACT_GEMINI.match(text)
    if gemini:
        version = gemini.group("version").replace("-", ".")
        model_id = "gemini-" + version + "-" + gemini.group("variant").lower()
        effort = _EFFORT_MAP.get((gemini.group("effort") or "").replace("-", " ").lower())
        if effort:
            model_id += "-" + gemini.group("effort").lower().replace(" ", "-")
        return "gemini", version, effort, False, model_id, gemini.end() + leading
    exact = _EXACT.match(text)
    if exact:
        raw_id = exact.group("id").lower().replace(" ", "-")
        family_match = re.search(r"(sol|astra|terra|luna|opus|sonnet|haiku|fable)", raw_id)
        version_match = re.search(r"(\d{1,2}(?:[.-]\d{1,2})?)", raw_id)
        if family_match and version_match:
            family = family_match.group(1)
            version = version_match.group(1).replace("-", ".")
            if len(version) == 2 and "." not in version:
                version = version[0] + "." + version[1]
            model_id = ("gpt-" + version + "-" + family if raw_id.startswith(("gpt-", "\d"))
                        else "claude-" + family + "-" + version)
            return family, version, _EFFORT_MAP.get((exact.group("effort") or "").replace("-", " ").lower()), \
                bool(exact.group("uc")), model_id, exact.end()

    model = _MODEL.match(text)
    if not model:
        return None
    family = model.group("family").lower()
    version = model.group("version")
    if version:
        version = version.replace("_", ".").replace("-", ".")
        if len(version) == 2 and "." not in version:
            version = version[0] + "." + version[1]
    effort = _EFFORT_MAP.get((model.group("effort") or "").replace("-", " ").lower())
    return family, version, effort, bool(model.group("uc")), None, model.end() + leading


def _candidate(text):
    """Return a valid provider or model selector at the start of text."""
    text = re.sub(r"(?<!\w)[*_`]+|[*_`]+(?!\w)", "", text.strip())
    provider_match = _PROVIDER_AT_START.match(text)
    provider = provider_name(provider_match.group(1)) if provider_match else None
    offset = provider_match.end() if provider_match else 0
    remainder = text[offset:]
    if provider:
        remainder = re.sub(r"^\s*(?:[/,:]|with)\s*", "", remainder, flags=re.IGNORECASE)
    model = _parse_selector(remainder) if remainder else None
    if provider and model:
        family, version, effort, ultracode, model_id, end = model
        tail = remainder[end:].strip()
        if (tail and tail[0] not in ",.!?:" and not _CONNECTORS.match(tail)
                and not text[offset:].lstrip().startswith((",", ":"))):
            return None
        return provider, family, version, effort, ultracode, model_id, True
    if provider:
        tail = remainder.strip()
        modifier = _PROVIDER_MODIFIER.match(tail)
        effort = _EFFORT_MAP.get((modifier.group("effort") or "").replace("-", " ").lower())
        ultracode = bool(modifier.group("uc"))
        modifier_tail = tail[modifier.end():].strip()
        if ((effort or ultracode) and
                (not modifier_tail or _CONNECTORS.match(modifier_tail) or modifier_tail[0] in ",.!?:")):
            return provider, None, None, effort, ultracode, None, True
        if (tail and tail[0] not in ",.!?:" and not _CONNECTORS.match(tail)
                and not text[offset:].lstrip().startswith((",", ":"))
                and not (provider == "Grok Bot" and _GROK_TASK.match(tail))):
            return None
        return provider, None, None, None, False, None, True
    if model:
        family, version, effort, ultracode, model_id, end = model
        tail = text[end:].strip()
        if tail and tail[0] not in ",.!?:" and not _CONNECTORS.match(tail):
            return None
        if family in ("sol", "astra", "terra", "luna"):
            provider = "Codex"
        elif family == "gemini":
            provider = "Antigravity"
        return provider, family, version, effort, ultracode, model_id, False
    return None


def _intent(candidate):
    provider, family, version, effort, ultracode, model_id, provider_explicit = candidate
    label_parts = [provider, (family + (" " + version if version else "")) if family else None]
    if effort:
        label_parts.append(effort)
    if ultracode:
        label_parts.append("ultracode")
    return Intent(provider, family, version, effort, ultracode,
                  " ".join(part for part in label_parts if part), model_id,
                  provider_explicit=provider_explicit)


def parse(prompt):
    """Return a deterministic explicit route, or an error for conflicting routes."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    cleaned = _clean(prompt)
    candidates = []

    def add(text):
        candidate = _candidate(text)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    # Provider or model labels followed by a colon are concise prompt prefixes.
    prefix = re.match(r"^\s*(?P<selector>[^\n:]{1,80}):", cleaned)
    if prefix:
        add(prefix.group("selector"))
    address = re.match(r"^\s*(" + _PROVIDER_PATTERN + r")\s*,", cleaned, re.IGNORECASE)
    if address:
        add(address.group(1))
    grok_address = re.match(r"^\s*(@?)(" + _GROK_BOT + r")(?:\s*[,.:]\s*|\s+)(?=\S)",
                            cleaned, re.IGNORECASE)
    if grok_address and (grok_address.group(1) or _GROK_TASK.match(cleaned[grok_address.end():])):
        add(grok_address.group(2))

    for directive in _DIRECTIVE.finditer(cleaned):
        before = cleaned[max(0, directive.start() - 24):directive.start()]
        if re.search(r"\b(?:do\s+not|don't|dont|never|avoid)\s*$", before, re.IGNORECASE):
            continue
        tail = cleaned[directive.end():directive.end() + 160].split("\n", 1)[0]
        if re.search(r"\bor\b", tail, re.IGNORECASE):
            for alternative in re.split(r"\bor\b", tail, maxsplit=1, flags=re.IGNORECASE):
                add(alternative)
        else:
            add(tail)

    # "with opus 5.5" is also an explicit selector, including after a task phrase.
    for with_match in re.finditer(r"\bwith\s+", cleaned, re.IGNORECASE):
        before = cleaned[max(0, with_match.start() - 40):with_match.start()]
        if re.search(r"\bcompar(?:e|ed|ing)\b", before, re.IGNORECASE):
            continue
        tail = cleaned[with_match.end():with_match.end() + 160].split("\n", 1)[0]
        candidate = _candidate(tail)
        same_model_already_selected = candidate and candidate[1] and any(
            existing[1:6] == candidate[1:6] for existing in candidates if existing[1]
        )
        if candidate and (candidate[1] or candidate[0] == "Grok Bot") and not same_model_already_selected:
            add(tail)

    if not candidates:
        return None
    # A family-only selector such as Opus can accompany a compatible provider
    # selector. Two different providers or models require a deliberate choice.
    providers = {candidate[0] for candidate in candidates if candidate[0]}
    models = {(candidate[1], candidate[2], candidate[3], candidate[4], candidate[5])
              for candidate in candidates if candidate[1]}
    if len(providers) > 1 or len(models) > 1:
        return Intent(error="Multiple conflicting provider or model choices were specified")
    provider = next(iter(providers), None)
    if models:
        model = next(iter(models))
    else:
        provider_candidate = next((candidate for candidate in candidates if candidate[0]), None)
        model = (None, None, provider_candidate[3], provider_candidate[4], None) if provider_candidate else \
            (None, None, None, False, None)
    family, version, effort, ultracode, model_id = model
    if not provider and family in ("sol", "astra", "terra", "luna"):
        inferred = "Codex"
    elif not provider and family == "gemini":
        inferred = "Antigravity"
    else:
        inferred = None
    explicit_provider = any(candidate[0] and candidate[6] for candidate in candidates)
    return _intent((provider or inferred, family, version, effort, ultracode, model_id, explicit_provider))
