"""StarCLI routing: what kind of task a prompt is, and which provider should run it.

Routing never calls a model, so it costs no tokens. A prompt is classified by
keywords and shape into a kind (debug, design, edit, ...) and a tier (light,
standard, heavy). Each installed provider runs one model per tier (star.json).
The router then scores every provider:

    score = q * quality + (1 - q) * (0.7 * balance + 0.3 * (1 - cost)) + sticky

quality is how well the provider handles that kind of task. balance is how
freely its quota can be spent now: for its tightest window, what is left
divided by the share of the window still to run, plus a bonus for quota that
will reset unused within a few hours (use it or lose it). cost is how precious the pool is (star.json): the
flagship subscriptions are worth more than Cursor's Auto pool or Gemini's
separate quota. q leans toward quality as the tier gets heavier, so quick
questions go to cheap idle pools and hard problems go to the strongest model
that still has room. A provider at NEAR percent of any window
is skipped. sticky keeps a conversation on the provider that already holds its
context, which saves the tokens a handoff would resend.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "star.json"

KINDS = ("chat", "search", "mechanical", "edit", "build", "debug", "review", "design", "write", "web")
TIERS = ("light", "standard", "heavy")
NEAR = 90           # percent of a window: never route there
LAST_HOUR = 96      # ... unless it resets within the hour: spend it nearly to the end
EXPIRING = 0.45     # most a window about to reset unused can add to a provider's balance
EXPIRY_HORIZON = 3 * 3600   # "soon" for any window: within three hours of its reset
TIGHT = 75          # percent of a window: route there only when it is clearly best
STICKY = 0.08
QUALITY_WEIGHT = {"light": 0.45, "standard": 0.6, "heavy": 0.72}
WINDOW = {"5h": 5 * 3600, "wk": 7 * 86400}   # anything else is a monthly plan
MONTH = 30 * 86400
STALE = 86400       # meters older than a day count as unknown

# Weighted keyword patterns per kind. The highest total wins; ties go to the
# kind listed first in KINDS order reversed (heavier kinds win ties).
PATTERNS = {
    "debug": [(r"\b(error|exception|traceback|stack ?trace|segfault|panic)\b", 3),
              (r"\b(fails?|failing|failed|broken|crash(es|ing)?|bug(gy)?|regression)\b", 2),
              (r"(doesn'?t|does not|isn'?t|is not|not) (work|working|run|running|load|start)", 2),
              (r"\b(fix|debug|diagnose|why (does|is|did|do|won'?t|can'?t))\b", 2)],
    "review": [(r"\b(review|audit|critique|look over|sanity.check|second opinion)\b", 3),
               (r"\b(security|vulnerab\w*|code smell|best practices?)\b", 2)],
    "design": [(r"\b(architect\w*|design|brainstorm|trade-?offs?|strategy|roadmap)\b", 3),
               (r"\b(plan|approach|how should (i|we)|what'?s the best way|options for|spec)\b", 2)],
    "build": [(r"\b(implement|build|scaffold|prototype|create|set ?up)\b", 2),
              (r"\b(add|make|write) (a |an |the |some )?(new )?(feature|command|cli|app|endpoint|page|screen|"
               r"script|program|function|class|module|component|api|tool|server|tests?)\b", 3),
              (r"\bnew (feature|app|cli|project|service)\b", 2)],
    "edit": [(r"\b(change|update|modify|refactor|replace|tweak|adjust|edit|remove|delete|move|"
              r"convert|port|clean ?up|simplify)\b", 2),
             (r"\b(in|to) [\w./-]+\.(py|ts|tsx|js|jsx|go|rs|rb|java|kt|swift|c|cpp|h|css|html|json|toml|ya?ml|md|sh)\b", 1)],
    "mechanical": [(r"\b(format|lint|prettier|black|bump|sort|typo|boilerplate|rename|install|commit|"
                    r"run (the )?(tests?|build|linter))\b", 3)],
    "search": [(r"\b(find|locate|grep|where (is|are|do|does)|where'?s|which (file|files|module|function))\b", 3),
               (r"\b(list (all|the|every)|show me|search (the )?(code|repo|codebase|project))\b", 2)],
    "write": [(r"\b(readme|docs?|documentation|changelog|email|blog|essay|announcement|write-?up|"
               r"release notes|copy(writing)?|draft|summari[sz]e|summary|explain)\b", 2)],
    "web": [(r"\b(latest|news|search the web|online|on the internet|google|current version|"
             r"newest|this week|today'?s|pricing)\b", 2)],
}
HEAVY = (r"\b(whole|entire|across|codebase|architecture|from scratch|end.to.end|complex|thorough(ly)?|"
         r"deep(ly)?|carefully|production|migrat\w+|overhaul|rewrite|comprehensive|robust|tricky|subtle|"
         r"race condition|concurren\w+|performance|scal\w+)\b")
LIGHT = r"\b(quick(ly)?|simple|just|tiny|small|typo|one.liner|brief(ly)?|short|real quick|tl;?dr)\b"
MCP = (r"\b(mcp|browser|calendar|email|gmail|outlook|slack|notion|linear|jira|figma|"
       r"supabase|vercel|meshy|higgsfield|lovable|google[- ]?(docs?|sheets?|slides?|drive|flow))\b|"
       r"docs\.google\.com|\b(generate|create|make)\b.{0,30}\b(image|video|3d model)\b")


def local_only(text):
    """Omit MCP only for clear local work or self-contained small talk.

    Unknown service names must keep their tools. A short prompt is not evidence
    that it needs only filesystem and shell access.
    """
    if re.search(MCP, text, re.S) or re.search(r"https?://", text):
        return False
    if re.fullmatch(r"\s*(hi|hello|hey|thanks|thank you|ok|okay)[.!? ]*", text):
        return True
    if re.fullmatch(r"\s*(?:what is |calculate )?[\d\s.+*/()%=-]+\??\s*", text):
        return True
    if re.fullmatch(r"\s*(?:please )?run (?:the )?(?:tests?|linter|build)\s*[.!]?", text):
        return True
    return bool(re.search(r"\b(this (?:repo|repository|codebase)|local (?:file|files|code|tests?)|"
                          r"without (?:external |mcp )?tools)\b", text))


@dataclass
class Task:
    kind: str
    tier: str
    reasons: list = field(default_factory=list)
    needs_mcp: bool = False


@dataclass
class Choice:
    provider: str
    tier: str
    model: str
    effort: str
    score: float
    quality: float
    balance: float
    why: str
    meter: str = ""
    pool: str = ""


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding="utf-8"))["providers"]


def classify(prompt, tier=None):
    """Kind and tier of a prompt, by keyword weight and length. Free: no model call."""
    text = prompt.lower()
    needs_mcp = not local_only(text)
    scores = {k: 0 for k in KINDS}
    for kind, patterns in PATTERNS.items():
        for pattern, weight in patterns:
            if re.search(pattern, text):
                scores[kind] += weight
    if "\n" in prompt.strip() and re.search(r"(traceback|error:|exception|at .+:\d+|line \d+)", text):
        scores["debug"] += 3   # a pasted stack trace
    kind = max(reversed(KINDS), key=lambda k: scores[k])
    reasons = []
    if scores[kind] == 0:
        kind = "chat"
        reasons.append("no task keywords: a question")
    else:
        reasons.append(f"{kind} keywords")
    if needs_mcp:
        reasons.append("keep MCP available unless the task is clearly local")

    if tier in TIERS:
        return Task(kind, tier, reasons + [f"tier pinned to {tier}"], needs_mcp=needs_mcp)
    words = len(prompt.split())
    heavy = len(re.findall(HEAVY, text))
    light = bool(re.search(LIGHT, text))
    if heavy >= 2 or words > 220 or (heavy and kind in ("design", "build", "debug", "review")):
        tier = "heavy"
        reasons.append("large or subtle work" if heavy else "long brief")
    elif kind == "design" and words > 25:
        tier = "heavy"
        reasons.append("design deserves the strongest model")
    elif (light and words < 40) or (kind in ("chat", "search", "mechanical") and words <= 14):
        tier = "light"
        reasons.append("small and self-contained")
    else:
        tier = "standard"
    return Task(kind, tier, reasons, needs_mcp=needs_mcp)


def windows(entry, now, pool=None):
    """[(label, used%, resets, length)] for a provider's meter, or None when unknown or stale."""
    if not isinstance(entry, dict) or now - float(entry.get("fetched", 0) or 0) > STALE:
        return None
    found = []
    for w in entry.get("windows") or []:
        try:
            label, pct = str(w.get("label", "")), float(w["pct"])
            resets = float(w["resets"]) if w.get("resets") else None
        except (KeyError, TypeError, ValueError):
            continue
        if pool and label != pool:
            continue
        if resets and resets <= now:
            pct = 0.0   # rolled over since it was read
        found.append((label, pct, resets, WINDOW.get(label, MONTH)))
    return found or None


def spare(found, now):
    """How freely a provider's quota can be spent now: 0..1, plus up to EXPIRING
    for quota that will reset unused.

    Capacity comes from the tightest window. Per window, the burn rate it can
    sustain is what is left over the share of the window still to run: 46%
    left with a fifth of the window to go can be spent at more than twice an
    even pace. Little absolute headroom still counts against it, since one
    large task can use several percent.

    On top of that, a window that resets soon with quota left is use it or lose
    it: Codex with 40% of its 5-hour window left and an hour to go should be
    run close to empty, as long as its weekly window is not the tight one. That
    quota earns a bonus in proportion to how much is left and the square of how
    close the reset is (within EXPIRY_HORIZON), scaled down when another window
    is tight."""
    if not found:
        return 0.4, "usage unknown"
    worst, note, values = None, "", {}
    for label, pct, resets, length in found:
        left = max(0.0, 100 - pct) / 100
        remaining = 1.0 if not resets else min(1.0, max(0.02, (resets - now) / length))
        rate = left / remaining
        value = min(1.0, rate / 0.9) ** 0.7 * (0.4 + 0.6 * min(1.0, left / 0.3))   # on pace or under: 1
        if pct >= TIGHT and remaining > 0.25:
            value *= 0.6
        values[label] = value
        if worst is None or value < worst:
            worst = value
            pace = "under pace" if rate > 1.1 else "over pace" if rate < 0.9 else "on pace"
            note = f"{100 - pct:.0f}% of {label} left, {pace}"
    bonus, expiring = 0.0, None
    for label, pct, resets, length in found:
        if not resets:
            continue
        left = max(0.0, 100 - pct) / 100
        # squared, so the bonus gathers in the last hour or two rather than
        # rewarding any window that is merely half run
        soon = (1 - min(1.0, max(0.0, (resets - now) / min(length, EXPIRY_HORIZON)))) ** 2
        others = min([v for k, v in values.items() if k != label] or [1.0])
        gain = left * soon * min(1.0, others / 0.8)
        if gain > bonus:
            bonus, expiring = gain, (label, left, resets)
    bonus = min(EXPIRING, bonus)
    if expiring and bonus >= 0.05:
        label, left, resets = expiring
        note = f"{left:.0%} of {label} expires in {until_text(resets - now)}, spend it"
    return worst + bonus, note


def until_text(seconds):
    m = max(1, int(seconds // 60))
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h < 24 else f"{h // 24}d {h % 24}h"


def limit_for(resets, now):
    """The percent at which a window is off limits: NEAR, or LAST_HOUR when it
    resets within the hour, so quota about to expire can be spent nearly to the end."""
    return LAST_HOUR if resets and 0 < resets - now <= 3600 else NEAR


def blocked(found, now=None):
    """The window that rules a provider out, as text, or None."""
    now = time.time() if now is None else now
    for label, pct, resets, _ in found or []:
        if pct >= limit_for(resets, now):
            return f"{pct:.0f}% of its {label} limit used"
    return None


def rank(task, usage, installed, config=None, now=None, current=None, pinned=None, down=(), images=False):
    """Every usable provider for the task, best first, and why each was skipped.

    usage: the agent-picker usage cache ({name: {"windows": [...], "fetched": t}}).
    current: the provider holding this conversation's context. pinned: route
    only there. down: providers that failed this session. images: the turn has
    attached images, so only providers marked "images" in star.json take it."""
    config = config or load_config()
    now = time.time() if now is None else now
    choices, skipped = [], {}
    for name, spec in config.items():
        if name not in installed:
            continue
        if pinned and name != pinned:
            continue
        if name in down:
            skipped[name] = "failed earlier this session"
            continue
        if images and not spec.get("images"):
            skipped[name] = "can't look at attached images"
            continue
        only = spec.get("only")
        if only and task.kind not in only and not pinned:
            skipped[name] = f"kept for {', '.join(only)} work"
            continue
        tiers = spec["tiers"]
        tier = task.tier if task.tier in tiers else None
        if tier is None:
            if pinned:
                tier = max(tiers, key=TIERS.index)
            else:
                skipped[name] = f"no {task.tier} model"
                continue
        model = tiers[tier]
        found = windows(usage.get(name), now, model.get("pool"))
        stop = blocked(found, now)
        if stop:
            skipped[name] = stop
            continue
        quality = spec["quality"].get(task.kind, 0.7)
        if tier == "light" and len(tiers) > 1:
            quality *= 0.92   # the cheaper model is a little weaker
        balance, meter = spare(found, now)
        cost = model.get("cost", spec.get("cost", 1.0))
        q = QUALITY_WEIGHT[task.tier]
        score = q * quality + (1 - q) * (0.7 * balance + 0.3 * (1 - cost))
        bits = [f"{task.kind} fit {quality:.2f}", meter]
        if current == name:
            score += STICKY
            bits.append("holds this conversation")
        choices.append(Choice(name, tier, model["model"], model.get("effort", ""), round(score, 4),
                              quality, balance, " · ".join(bits), meter, model.get("pool", "")))
    choices.sort(key=lambda c: -c.score)
    return choices, skipped
