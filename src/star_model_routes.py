"""Resolve explicit model requests against StarCLI's cached provider catalogs."""

import re

import model_catalog
from cursor_pools import model_pool


PROVIDERS = ("Claude", "Codex", "Cursor", "Kiro", "Antigravity")
ALIASES = {name.lower(): name for name in PROVIDERS}
ALIASES.update({"agy": "Antigravity", "gemini": "Antigravity"})
FAMILIES = ("opus", "sonnet", "haiku", "sol", "astra", "terra", "luna", "gemini", "grok", "fable")
PROVIDER_FAMILIES = {
    "Claude": {"opus", "sonnet", "haiku", "fable"},
    "Codex": {"sol", "astra", "terra", "luna"},
    "Cursor": set(FAMILIES),
    "Kiro": set(FAMILIES) - {"gemini"},
    "Antigravity": {"gemini"},
}
EFFORT_BY_TIER = {"light": "low", "standard": "medium", "heavy": "xhigh"}
EFFORT_ORDER = ("minimal", "none", "low", "medium", "high", "xhigh", "max", "ultra")


def _field(intent, name, default=None):
    if isinstance(intent, dict):
        return intent.get(name, default)
    return getattr(intent, name, default)


def _provider_name(value):
    if not value:
        return None
    return ALIASES.get(str(value).strip().lower())


def _catalog(provider):
    # Read refreshed, cached, or bundled model data. This never invokes a CLI.
    return model_catalog.load_rows({"name": provider})


def _normalized(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _row_family(row):
    text = (str(row.get("id", "")) + " " + str(row.get("name", ""))).lower()
    for family in FAMILIES:
        if re.search(r"(?<![a-z])" + re.escape(family) + r"(?![a-z])", text):
            return family
    return None


def _version(text):
    value = str(text or "").lower()
    match = re.search(
        r"(?:opus|sonnet|haiku|sol|astra|terra|luna|gemini|grok|fable)[ -]?([0-9]{1,2})[ .-]?([0-9])?",
        value)
    if not match:
        match = re.search(
            r"(?:gpt[- ]?)?([0-9]{1,2})[ .-]([0-9])[- ]?(?:sol|astra|terra|luna)\b", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _version_text(value):
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and value:
        return tuple(int(part) for part in value)
    match = re.fullmatch(r"\s*(\d{1,2})(?:[.-](\d))?\s*", str(value))
    return (int(match.group(1)), int(match.group(2) or 0)) if match else _version(value)


def _available_efforts(row):
    efforts = row.get("efforts")
    variants = row.get("variants") if isinstance(row.get("variants"), dict) else {}
    available = [level for level in EFFORT_ORDER
                 if isinstance(efforts, list) and level in efforts
                 or isinstance(variants.get(level), str)]
    return available


def _model_for(row, requested_effort, tier):
    efforts = _available_efforts(row)
    if requested_effort and requested_effort not in efforts:
        return None
    variants = row.get("variants") if isinstance(row.get("variants"), dict) else {}
    base = row["id"]
    effort = requested_effort or EFFORT_BY_TIER.get(tier, "medium")
    if efforts:
        if not requested_effort:
            if effort not in efforts:
                # Use the closest supported effort for automatic tier choice.
                index = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else 3
                effort = min(efforts, key=lambda level: (
                    abs(EFFORT_ORDER.index(level) - index), -EFFORT_ORDER.index(level)))
        variant = variants.get(effort)
        if isinstance(variant, str):
            return variant, effort
        # Catalogs such as Kiro list efforts without encoded variants.
        return base, effort
    return variants.get("provider default", base), ""


def _configured_family(config, provider, tier):
    selected = config.get(provider, {}).get("tiers", {}).get(tier, {})
    if not selected:
        tiers = config.get(provider, {}).get("tiers", {})
        selected = next(iter(tiers.values()), {})
    return _row_family({"id": selected.get("model", ""), "name": ""})


def _choose_row(rows, family, version, all_versions):
    compatible = [row for row in rows if _row_family(row) == family]
    if version is not None:
        compatible = [row for row in compatible if _version(row.get("id")) == version
                      or _version(row.get("name")) == version]
    elif all_versions:
        compatible = [row for row in compatible if _version(row.get("id")) == all_versions
                      or _version(row.get("name")) == all_versions]
    if not compatible:
        return None
    compatible.sort(key=lambda row: (
        0 if row.get("id") == row.get("name") else 1,
        tuple(-part for part in (_version(row.get("id")) or _version(row.get("name")) or (0, 0))),
        str(row.get("id", ""))))
    return compatible[0]


def _matches_model_id(row, model_id):
    wanted = _normalized(model_id)
    if _normalized(row.get("id")) == wanted:
        return True
    variants = row.get("variants") if isinstance(row.get("variants"), dict) else {}
    return any(_normalized(value) == wanted for value in variants.values() if isinstance(value, str))


def _candidate(config, provider, tier, row, effort):
    configured = config[provider].get("tiers", {}).get(tier, {})
    result = dict(configured)
    selected = _model_for(row, effort, tier)
    if selected is None:
        return None
    result.update(model=selected[0], effort=selected[1])
    if provider == "Cursor":
        result["pool"] = model_pool(selected[0]) or result.get("pool") or "cr"
        if result.get("pool") != configured.get("pool"):
            result.pop("cost", None)
    return result


def resolve(config, tier, intent, installed=None):
    """Return candidate provider tier overrides for a parsed explicit selector.

    Candidate output is quota-agnostic. Star's ranker removes exhausted pools
    and asks the user to defer when every compatible route is unavailable.
    """
    provider_name = _field(intent, "provider")
    if provider_name == "Grok Bot":
        if installed is not None and provider_name not in installed:
            return {}
        if any(_field(intent, field) for field in ("family", "model_id", "effort", "ultracode")):
            return {}
        model = config.get(provider_name, {}).get("tiers", {}).get("standard")
        return {provider_name: model} if model else {}
    requested_provider = _provider_name(provider_name)
    family = _field(intent, "family")
    exact_id = _field(intent, "model_id")
    requested_version = _version_text(_field(intent, "version"))
    requested_effort = _field(intent, "effort")
    ultracode = bool(_field(intent, "ultracode", False))
    provider_explicit = bool(_field(intent, "provider_explicit", False))
    installed = set(installed) if installed is not None else set(config)

    # Ultracode's delegation workflow is Claude-only even without a model name.
    if ultracode:
        requested_provider = "Claude"
        family = family or _configured_family(config, "Claude", tier)

    if exact_id and not family:
        exact = _normalized(exact_id)
        rows_by_provider = {
            provider: [row for row in _catalog(provider) if provider in installed
                       if _matches_model_id(row, exact)]
            for provider in PROVIDERS
        }
        # A full ID must match the advertised ID after punctuation/case folding.
        matching = {provider: rows for provider, rows in rows_by_provider.items() if rows}
        if requested_provider:
            matching = {provider: rows for provider, rows in matching.items()
                        if provider == requested_provider}
        if ultracode:
            matching = {provider: rows for provider, rows in matching.items()
                        if provider == "Claude"}
        result = {}
        for provider, rows in matching.items():
            if provider not in config or not config[provider].get("tiers"):
                continue
            choice = _candidate(config, provider, tier, rows[0], requested_effort)
            if choice is not None:
                if ultracode:
                    choice["ultracode"] = True
                result[provider] = choice
        return result

    provider_only = requested_provider and provider_explicit and not exact_id and (
        not _field(intent, "family")
        or requested_provider == "Antigravity" and family == "gemini"
        and requested_version is None)
    if not family:
        if not requested_provider:
            return {}
        family = _configured_family(config, requested_provider, tier)
        if not family:
            selected = config.get(requested_provider, {}).get("tiers", {}).get(tier)
            return {requested_provider: dict(selected)} if selected else {}

    if ultracode:
        providers = ["Claude"]
    elif provider_only and requested_provider in ("Cursor", "Kiro"):
        selected = config.get(requested_provider, {}).get("tiers", {}).get(tier)
        if not selected:
            return {}
        if not requested_effort:
            return {requested_provider: dict(selected)}
        row = next((candidate for candidate in _catalog(requested_provider)
                    if _matches_model_id(candidate, selected.get("model"))), None)
        choice = _candidate(config, requested_provider, tier, row, requested_effort) if row else None
        return {requested_provider: choice} if choice else {}
    elif provider_only and requested_provider == "Claude":
        providers = ["Claude", "Kiro", "Cursor"]
    elif provider_only and requested_provider == "Antigravity":
        providers = ["Antigravity", "Cursor"]
    elif provider_only and requested_provider == "Codex":
        providers = [requested_provider, "Cursor", "Kiro"]
    else:
        providers = list(PROVIDERS)
    providers = [provider for provider in providers if provider in installed]

    catalogs = {}
    for provider in providers:
        rows = _catalog(provider)
        if exact_id:
            rows = [row for row in rows if _matches_model_id(row, exact_id)]
        if requested_effort:
            rows = [row for row in rows
                    if _model_for(row, requested_effort, tier) is not None]
        catalogs[provider] = rows
    versions = [
        _version(row.get("id")) or _version(row.get("name"))
        for provider in providers
        for row in catalogs[provider]
        if _row_family(row) == family
    ]
    if (requested_provider == "Codex" and family == "sol" and not provider_explicit
            and requested_provider not in catalogs):
        versions.extend(
            _version(row.get("id")) or _version(row.get("name"))
            for row in _catalog("Codex") if _row_family(row) == family)
    latest = max((value for value in versions if value is not None), default=None)
    # A named provider with a family shorthand pins version choice to that
    # provider's newest available catalog version, then fallback providers must
    # offer that same version.
    if provider_explicit and family and requested_provider in catalogs and not provider_only:
        primary_versions = [
            _version(row.get("id")) or _version(row.get("name"))
            for row in catalogs[requested_provider]
            if _row_family(row) == family
        ]
        latest = max((value for value in primary_versions if value is not None),
                     default=latest)
    chosen_version = requested_version or latest
    if provider_only and requested_provider in ("Claude", "Codex"):
        # Provider-only selectors retain each available CLI's own current tier
        # model version, allowing older compatible providers as quota fallbacks.
        chosen_version = None
    primary_tiers = config.get(requested_provider, {}).get("tiers", {})
    primary_tier_config = primary_tiers.get(tier) or next(iter(primary_tiers.values()), {})
    primary_model = primary_tier_config.get("model")
    primary_version = _version(primary_model)
    result = {}
    for provider in providers:
        if provider not in config or family not in PROVIDER_FAMILIES.get(provider, set()):
            continue
        rows = catalogs[provider]
        direct_provider = provider_only and provider == requested_provider
        row = None
        if direct_provider and provider == "Codex":
            row = next((candidate for candidate in rows
                        if primary_model and _matches_model_id(candidate, primary_model)), None)
            if row is None:
                row = _choose_row(rows, family, None, None)
        elif provider_only and requested_provider == "Antigravity":
            if direct_provider:
                direct_model = primary_model
                row = next((candidate for candidate in rows
                            if direct_model and _matches_model_id(candidate, direct_model)), None)
                if row is None:
                    row = _choose_row(rows, family, primary_version, primary_version)
            else:
                row = _choose_row(rows, family, primary_version, primary_version)
                if row is None and requested_version is None:
                    row = _choose_row(rows, family, None, latest)
        elif provider_only and requested_provider == "Claude":
            if provider == requested_provider:
                direct_model = primary_model
                row = next((candidate for candidate in rows
                            if direct_model and _matches_model_id(candidate, direct_model)), None)
            if row is None:
                row = _choose_row(rows, family, None, None)
        else:
            row = _choose_row(rows, family, chosen_version, chosen_version)

        # A Claude-only selector should still offer each installed Claude CLI.
        # Keep its own tier model where possible, and use that CLI's best Claude
        # model when its default tier is Auto or a non-Claude family.
        if provider_only and requested_provider == "Claude" and row is None:
            alternatives = [candidate for candidate in rows
                            if _row_family(candidate) in ("opus", "sonnet", "haiku")]
            preferred = _configured_family(config, provider, tier)
            if preferred and _choose_row(rows, preferred, None, None):
                preferred_family = preferred
                row = _choose_row(rows, preferred_family, None, None)
            elif alternatives and provider == "Cursor":
                row = _choose_row(rows, "opus", None, None) or _choose_row(rows, "sonnet", None, None)
            elif alternatives:
                row = _choose_row(rows, "sonnet", None, None) or _choose_row(rows, "opus", None, None)
        if row is None:
            continue
        choice = _candidate(config, provider, tier, row, requested_effort)
        if choice is None:
            continue
        if ultracode and provider != "Claude":
            continue
        if ultracode:
            choice["ultracode"] = True
        result[provider] = choice

    # Any requested family shorthand must use one common latest version, even
    # when vendor IDs spell it differently (5-5 vs 5.5).
    return result
