"""The one customised sentence in each email.

Everything else in the template is fixed text John wrote. This module produces
only the alignment line, in his exact shape:

    [Company]'s mission to [A] (and [B]) aligns with my work in [X] and [Y].

Two hard constraints. It must stay under 25 words, which is enforced here
rather than trusted to the model. And [X] and [Y] must come from ASPECTS below,
a closed list drawn from John's resume and GitHub, so the sentence cannot
invent experience he does not have.

Requests goes through `requests` because Groq sits behind Cloudflare, which
blocks urllib's default user agent with error 1010.
"""

from __future__ import annotations

import re

import requests

from .config import app_root

from .ratelimit import Budget, RateLimiter

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# "<25 words" means 24 is the most that qualifies.
WORD_LIMIT = 24
MAX_RETRIES = 3

def load_aspects() -> list[str]:
    """The claims this install is allowed to make, supplied by its owner.

    Read from config/aspects.txt so a shared build carries nobody's history.
    An empty file means no alignment sentence is ever written, which is the
    safe default: saying nothing beats claiming something untrue.
    """
    path = app_root() / "config" / "aspects.txt"
    if not path.exists():
        return []
    return [l.strip().lower() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


# Cue words for the aspects this project shipped with. Anything the user adds
# falls back to its own significant words, so relevance still gets checked.
DEFAULT_ASPECTS = [
    "real-time risk monitoring",              # Eureka
    "autonomous incident briefings",          # Eureka, Vapi/Twilio voice agents
    "supply-chain disruption detection",      # Eureka
    "live news scoring against supplier graphs",  # Eureka
    "brand and product evaluation",           # Nora
    "visual similarity scoring",              # Nora, CLIP embeddings
    "agent-accessible tooling over MCP",      # Nora
    "computer-vision pipelines",              # Iris
    "speech-driven interfaces",               # Iris
    "shipping full-stack features quickly",   # general, supported by repos
]

ASPECTS = load_aspects() or DEFAULT_ASPECTS


def cues_for(aspect: str) -> set[str]:
    """Keywords that mean a company works in the same territory as an aspect.

    Curated where we have them; otherwise the aspect's own words, minus filler,
    which is weaker but still stops an unrelated match.
    """
    known = ASPECT_CUES.get(aspect)
    if known:
        return known
    stop = {"and", "the", "for", "with", "over", "a", "an", "of", "in", "to", "quickly"}
    return {w for w in re.split(r"[^a-z0-9]+", aspect.lower()) if w and w not in stop and len(w) > 3}


SYSTEM = (
    "You write ONE sentence for a cold internship email, following an exact "
    "template. Output only the sentence, nothing else.\n\n"
    "TEMPLATE: <Company>'s mission to <A> aligns with my work in <X> and <Y>.\n"
    "Optionally: <Company>'s mission to <A> and <B> aligns with my work in "
    "<X> and <Y>.\n\n"
    "RULES:\n"
    "- Under 25 words. This is absolute.\n"
    "- <A> and <B> describe what the company does, in your own plain words.\n"
    "- <X> and <Y> MUST be copied verbatim from the ALLOWED list. Never invent "
    "any other experience.\n"
    "- Pick the two ALLOWED items most genuinely relevant to this company.\n"
    "- NEVER put an ALLOWED item in <A> or <B>. Those describe the company only, "
    "in your own words. ALLOWED items appear ONLY after 'aligns with my work in'.\n"
    "- After the two ALLOWED items, stop. Add nothing further.\n"
    "- Plain text. No markdown, no emoji, no em dash, no flattery.\n\n"
    "EXAMPLE: Huscarl's mission to quantify emerging risks and keep actuarial "
    "models audit-ready aligns with my work in real-time risk monitoring and "
    "autonomous incident briefings."
)


# Models emit typographic punctuation. Plain ASCII travels better in email and
# matches the rest of the template.
UNICODE_PUNCT = {
    "—": "-", "–": "-", "‑": "-", "‒": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ", " ": " ",
}


def to_ascii(text: str) -> str:
    for bad, good in UNICODE_PUNCT.items():
        text = text.replace(bad, good)
    return text


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


HINGE = "aligns with my work in"
DECLINE = "NONE"

# Words that signal a company genuinely works in the same territory as one of
# John's projects. Used as a backstop on top of the model's own judgement: if
# nothing in the company's own description matches the aspect the model chose,
# the connection is asserted rather than evident, and the paragraph is dropped.
ASPECT_CUES: dict[str, set[str]] = {
    "real-time risk monitoring": {
        "risk", "monitor", "alert", "incident", "disrupt", "complian", "fraud",
        "observab", "security", "insur", "underwrit", "audit", "threat", "safety",
    },
    "autonomous incident briefings": {
        "incident", "alert", "call", "voice", "notif", "brief", "escalat",
        "phone", "agent", "outreach", "receptionist", "on-call",
    },
    "supply-chain disruption detection": {
        "supply", "logistic", "shipping", "supplier", "procure", "freight",
        "vendor", "inventory", "warehouse", "manufactur",
    },
    "live news scoring against supplier graphs": {
        "news", "supplier", "signal", "score", "graph", "intelligence", "market",
    },
    "brand and product evaluation": {
        "brand", "product", "commerce", "retail", "market", "creative", "ad",
        "catalog", "shop", "consumer", "design",
    },
    "visual similarity scoring": {
        "image", "visual", "photo", "similar", "embedding", "vision", "catalog",
        "design", "video", "search", "match",
    },
    "agent-accessible tooling over MCP": {
        "agent", "mcp", "tool", "api", "llm", "automat", "workflow", "integrat",
        "developer", "infrastructure", "platform", "data",
    },
    "computer-vision pipelines": {
        "vision", "image", "camera", "video", "robot", "visual", "ocr",
        "document", "scan", "autonomous", "vehicle", "diagnos", "medical",
    },
    "speech-driven interfaces": {
        "voice", "speech", "call", "audio", "conversation", "transcri", "phone",
        "receptionist", "interview", "language", "chat",
    },
    # Deliberately empty: "I ship fast" is not a connection to a mission, so it
    # can never on its own justify keeping the paragraph.
    "shipping full-stack features quickly": set(),
}


def connection_is_evident(sentence: str, company_text: str,
                          aspects: list[str] = ASPECTS) -> bool:
    """True when at least one chosen aspect is echoed by the company's own words."""
    low = sentence.lower()
    if HINGE not in low:
        return False
    work = low.split(HINGE, 1)[1]
    chosen = [a for a in aspects if a.lower() in work]
    blob = (company_text or "").lower()
    return any(
        any(cue in blob for cue in cues_for(aspect))
        for aspect in chosen
    )


def uses_only_allowed(sentence: str, aspects: list[str] = ASPECTS) -> bool:
    """At least one allowed aspect must appear, or the model went off-script."""
    low = sentence.lower()
    return any(aspect.lower() in low for aspect in aspects)


def well_formed(sentence: str, aspects: list[str] = ASPECTS) -> bool:
    """Check the sentence actually has the shape John asked for.

    The failure this catches is the model dropping John's own skills into the
    company's half, producing things like "Synthio Labs's mission to
    agent-accessible tooling over MCP aligns with my work in agent-accessible
    tooling over MCP". That is gibberish, and gibberish is worse than omitting
    the paragraph, so it is rejected here.
    """
    low = sentence.lower()
    if low.count(HINGE) != 1:
        return False
    mission, work = low.split(HINGE, 1)
    mission, work = mission.strip(), work.strip()
    if len(mission.split()) < 4 or not work:
        return False
    # John's skills belong only after the hinge, never in the company's clause.
    if any(a.lower() in mission for a in aspects):
        return False
    used = [a for a in aspects if a.lower() in work]
    if not 1 <= len(used) <= 2:
        return False
    # The work half should be the listed aspects joined by "and", not prose
    # with extra invented claims trailing off it.
    remainder = work
    for aspect in used:
        remainder = remainder.replace(aspect.lower(), "")
    remainder = re.sub(r"[\s,.;]|\band\b", "", remainder)
    return not remainder


class Personalizer:
    def __init__(self, api_key: str, model: str, max_calls: int = 40,
                 per_minute: int = 25, timeout: int = 20):
        self.api_key = api_key
        self.model = model
        self.budget = Budget(max_calls, "groq")
        self.limiter = RateLimiter(per_minute)
        self.timeout = timeout
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def line_for(self, company: str, one_liner: str, description: str,
                 role: str = "") -> str:
        """Return the alignment sentence, or "" to omit the paragraph entirely.

        A generic filler line is worse than no line: it says nothing specific
        and reads like a mail merge, so we drop the paragraph instead.
        """
        if not self.enabled or self.budget.exhausted or not self.budget.take():
            return ""
        prompt = (
            f"Company: {company}\n"
            f"What they do: {one_liner}\n"
            f"More detail: {description[:500]}\n\n"
            "ALLOWED (copy two of these verbatim):\n"
            + "\n".join(f"- {a}" for a in ASPECTS)
            + "\n\nWrite the sentence."
        )
        company_text = f"{company} {one_liner} {description}"

        def declined(text: str) -> bool:
            return text.strip().upper().startswith(DECLINE)

        def acceptable(text: str) -> bool:
            return (bool(text) and word_count(text) <= WORD_LIMIT
                    and uses_only_allowed(text) and well_formed(text)
                    and connection_is_evident(text, company_text))

        sentence = self._ask(prompt)
        # A refusal is a real answer: the model saw no genuine link, so respect
        # it rather than badgering it into inventing one.
        if declined(sentence):
            return ""
        if acceptable(sentence):
            return sentence
        # Retry a few times. The usual failure is an empty response, which
        # happens when the reasoning model spends its whole token budget
        # thinking; that is transient and a fresh attempt normally succeeds.
        # Over-long or off-list output means it improvised experience John does
        # not have, which the nudge below corrects.
        nudge = (
            f"\n\nYour last attempt was empty, too long, or used experience "
            f"outside the ALLOWED list. Answer with ONE sentence under "
            f"{WORD_LIMIT} words, copying two ALLOWED items exactly."
        )
        for _ in range(MAX_RETRIES):
            if not self.budget.take():
                break
            retry = self._ask(prompt + nudge)
            if declined(retry):
                return ""
            if acceptable(retry):
                return retry
        return ""

    def _ask(self, prompt: str) -> str:
        self.limiter.acquire()
        try:
            resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    # gpt-oss is a reasoning model: with a small budget it spends
                    # every token thinking and returns empty content.
                    "max_tokens": 400,
                    "reasoning_effort": "low",
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                return ""
            text = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = to_ascii(text).strip().strip('"').replace("\n", " ")
        return re.sub(r"\s+", " ", text)
