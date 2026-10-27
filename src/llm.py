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
import zlib

import requests

from .config import app_root

from .ratelimit import Budget, RateLimiter

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# What they build, what John built and what was a grind, then a question.
WORD_LIMIT = 50
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
    "You describe what a startup builds, for one line of a cold email. Answer "
    "with only the phrase, nothing else.\n\n"
    "Rules:\n"
    "- It must fit after \"Saw you're\". Start with an -ing verb.\n"
    "- 4 to 12 words, normal grammar with articles.\n"
    "- Say concretely what the product does and for whom, the way you would "
    "tell a friend. Not the vision, not the market.\n"
    "- Plain everyday words, lowercase except names. Commas and hyphens only.\n"
    "- Never use marketing words like mission, revolutionize, empower, "
    "seamless, leverage, innovative, cutting-edge, solutions, platform.\n\n"
    "Good: building phone agents that book appointments for dental offices\n"
    "Good: giving coding agents a place to run for whole teams\n"
    "Bad: revolutionizing the future of work with AI-powered solutions"
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


# John's half of the line, written by hand so nothing about him is ever
# generated: what he built, the part of it that was a grind, and a question
# about how the company handles the same thing. The question is what gives a
# founder a reason to reply; the pain point is what shows he has actually done
# this work. Each pain is one nearly everyone who builds that kind of thing hits.
#
# aspect: (what he built, the annoying part)
FACT_PARTS: dict[str, tuple[str, str]] = {
    "agent-accessible tooling over MCP": (
        "an MCP endpoint for Nora",
        "getting the model to reliably pick the right tool"),
    "autonomous incident briefings": (
        "Vapi voice agents for Eureka",
        "handling interruptions so the agent didn't talk over people"),
    "speech-driven interfaces": (
        "the speech side of Iris",
        "making the voice sound natural instead of flat and robotic"),
    "real-time risk monitoring": (
        "the risk monitoring in Eureka",
        "flagging real problems without drowning people in false alarms"),
    "supply-chain disruption detection": (
        "supply chain disruption detection for Eureka",
        "matching messy news mentions to the right supplier"),
    "brand and product evaluation": (
        "Nora's brand scoring",
        "pulling consistent signals out of product pages that were all built differently"),
    "visual similarity scoring": (
        "visual similarity scoring for Nora",
        "getting CLIP scores to match what a person would call similar"),
    "computer-vision pipelines": (
        "a computer vision pipeline for Iris",
        "keeping it accurate when lighting and camera angles changed"),
}

# Rotated so consecutive emails do not end the same way.
QUESTIONS = (
    "Curious how you handle that.",
    "Would love to hear how you deal with it.",
)

FACT_LINES: dict[str, list[str]] = {
    aspect: [f"When I built {built}, the annoying part was {pain}. {question}"
             for question in QUESTIONS]
    for aspect, (built, pain) in FACT_PARTS.items()
}

# How the company half is introduced. Rotated per company.
OPENERS = ["Saw you're", "Looks like you're", "Saw that you're", "Read that you're"]

# Stems that make a line read as generated or as marketing copy.
BANNED = (
    "mission", "align", "passion", "excit", "thrill", "innovat", "cutting",
    "leverag", "revolution", "resonat", "impress", "journey", "seamless",
    "empower", "transform", "landscape", "game-chang", "unlock", "streamlin",
    "robust", "synerg", "delve", "elevat", "harness", "solution", "platform",
    "next-gen", "world-class", "intersection", "ecosystem", "paradigm",
    "supercharg", "reimagin", "redefin", "pioneer",
)
ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9 ,'\-&/.]+$")


def _pick(options: list[str], key: str) -> str:
    """A stable choice per company, so a retry renders the same line."""
    return options[zlib.crc32(key.encode("utf-8")) % len(options)]


# What has to appear in the company's own words before a project of John's is
# brought up, matched at the start of a word. Ordered by preference when more
# than one fits. Kept narrow on purpose: a stretch reads worse than no line.
MATCH_CUES: dict[str, tuple[str, ...]] = {
    "agent-accessible tooling over MCP": ("agent", "mcp"),
    "autonomous incident briefings": ("on-call", "incident", "receptionist",
                                      "phone call", "calls"),
    "speech-driven interfaces": ("voice", "speech", "speak", "spoken", "audio",
                                 "transcri", "conversation"),
    "real-time risk monitoring": ("risk", "fraud", "complian", "threat",
                                  "security", "monitoring", "alert"),
    "supply-chain disruption detection": ("supply chain", "supplier", "logistic",
                                          "freight", "procurement"),
    "visual similarity scoring": ("visual search", "image search", "similar"),
    # Seeing, not generating: an AI video studio is not computer vision.
    "computer-vision pipelines": ("computer vision", "camera", "ocr", "robot",
                                  "perception", "inspection", "detect"),
    # Not "ecommerce" alone: that shows up in customer lists, as in a
    # bookkeeping firm serving "SMBs, contractors & ecommerce".
    "brand and product evaluation": ("brand", "shopify", "storefront",
                                     "product catalog"),
}


def _hits(cues: tuple[str, ...], text: str) -> int:
    return sum(bool(re.search(r"\b" + re.escape(cue), text, re.I)) for cue in cues)


def match_aspect(one_liner: str, description: str = "",
                 aspects: list[str] = ASPECTS) -> str:
    """The one claim of John's this company plainly relates to, or "".

    Decided here rather than by the model, which turned down agent-tooling
    companies and tied a game studio to voice work. A cue in the one-liner
    counts; the longer description needs three different cues, because a
    passing mention there is not what the company does.
    """
    # aspects.txt is read lowercased, the tables here are not.
    allowed = {a.lower() for a in aspects}
    best, best_score = "", 0
    for aspect, cues in MATCH_CUES.items():
        if aspect.lower() not in allowed or aspect not in FACT_LINES:
            continue
        top = _hits(cues, one_liner or "")
        deep = _hits(cues, description or "")
        if not top and deep < 3:
            continue
        score = 2 * top + deep
        if score > best_score:
            best, best_score = aspect, score
    return best


def clean_what(text: str) -> str:
    """The model's phrase, tidied, or "" when it reads as generated."""
    what = (text or "").strip()
    what = re.sub(r"^(?:what:|saw you're|you're)\s*", "", what, flags=re.I)
    what = what.strip().strip('"').rstrip(".").strip()
    words = what.split()
    if not 4 <= len(words) <= 12 or not words[0].lower().endswith("ing"):
        return ""
    if not ALLOWED_CHARS.match(what) or ".." in what:
        return ""
    if any(stem in what.lower() for stem in BANNED):
        return ""
    return what


def compose(company: str, what: str, aspect: str) -> str:
    # The model lowercases names ("building mage sdk tools"); restore the
    # company's own spelling, and common acronyms.
    if company:
        what = re.sub(r"\b" + re.escape(company) + r"\b", company, what, flags=re.I)
    for acronym in ("AI", "API", "MCP", "SDK", "LLM", "CRM", "GPU"):
        what = re.sub(r"\b" + acronym + r"(s?)\b", acronym + r"\1", what,
                      flags=re.I)
    opener = _pick(OPENERS, company)
    fact = _pick(FACT_LINES[aspect], company + aspect)
    return f"{opener} {what}. {fact}"


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
        """Return the personal line, or "" to omit the paragraph entirely.

        Which of John's projects to mention is decided by match_aspect from
        the company's own words, and his half of the line is fixed text from
        FACT_LINES. The model only writes the short phrase about the company,
        so it cannot claim anything he has not done.
        """
        aspect = match_aspect(one_liner, description)
        # No plain overlap means no line, and no model call spent finding out.
        if (not aspect or not self.enabled or self.budget.exhausted
                or not self.budget.take()):
            return ""
        prompt = (
            f"Company: {company}\n"
            f"What they do: {one_liner}\n"
            f"More detail: {description[:500]}"
        )

        def render(text: str) -> str:
            what = clean_what(text)
            if not what:
                return ""
            line = compose(company, what, aspect)
            return line if word_count(line) <= WORD_LIMIT else ""

        line = render(self._ask(prompt))
        if line:
            return line
        # The usual failure is an empty response, when the reasoning model
        # spends its token budget thinking; a fresh attempt normally works.
        nudge = ("\n\nYour last answer broke the rules. Only the phrase, 4 to 12 "
                 "plain words starting with an -ing verb.")
        for _ in range(MAX_RETRIES):
            if not self.budget.take():
                break
            line = render(self._ask(prompt + nudge))
            if line:
                return line
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
                    "temperature": 0.5,
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
