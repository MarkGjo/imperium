"""
Parse voice transcripts into ordered CommandStep objects for sequential execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass
class CommandStep:
    """Represents a single parsed step from a voice command."""

    action: str
    target: str
    content: str
    raw: str
    index: int


# Words that split a command into multiple steps (applied with additional passes in split_into_steps)
STEP_SPLITTERS = [
    r",\s*\band\s+then\b",
    r"\band\s+then\b",
    # Split before "print/show/…" so step 2 keeps the verb (lookahead — do not eat "print")
    r"\band\s+(?=print\b)",
    r"\band\s+(?=show\b)",
    r"\band\s+(?=display\b)",
    r"\band\s+(?=read\b)",
    r"\band\s+(?=speak\b)",
    r"\bthen\b",
    r"\bafter\s+that\b",
    r"\bnext\b",
    r"\bafterwards\b",
    r"\bfollowed\s+by\b",
    r"\bonce\s+(?:that'?s?\s+)?done\b",
    r"\bafter\s+(?:it'?s?\s+)?(?:open(?:ed)?|load(?:ed)?|ready)\b",
]

# Split "Open X and type …" / "… and play …" into separate steps
STEP_SPLITTERS_AND_ACTION = (
    r"\band\s+(?=type\s+in\s+the\s+prompt|type\s+in\b|type\b|write\b|submit\b|send\b|"
    r"play\b|search\b|set\s+volume|take\s+a\s+screenshot|go\s+to\b|open\s+)"
)

ACTION_MAP = {
    "open": [
        "navigate to",
        "go to",
        "switch to",
        "take me to",
        "pull up",
        "bring up",
        "launch",
        "start",
        "load",
        "open",
    ],
    "type": [
        "type in the prompt",
        "type in",
        "type",
        "write in",
        "fill in",
        "paste",
        "input",
        "enter",
        "write",
        "put in",
    ],
    "submit": [
        "click the send button",
        "press the send button",
        "click send",
        "hit send",
        "press send",
        "press enter",
        "hit enter",
        "click enter",
        "click submit",
        "submit",
        "send",
    ],
    "search": [
        "search for",
        "look up",
        "look for",
        "find me",
        "search up",
        "search",
        "find",
    ],
    "play": [
        "play the song",
        "play song",
        "play the track",
        "play track",
        "listen to",
        "put on",
        "queue",
        "play",
    ],
    "close": [
        "shut down",
        "shut",
        "exit",
        "quit",
        "close",
        "kill",
    ],
    "scroll": [
        "scroll to the bottom",
        "scroll to top",
        "scroll down",
        "scroll up",
        "scroll",
    ],
    "click": [
        "select",
        "tap",
        "hit",
        "press",
        "click",
    ],
    "volume": [
        "decrease volume",
        "increase volume",
        "turn volume",
        "volume to",
        "set volume",
        "unmute",
        "mute",
    ],
    "screenshot": [
        "take a screenshot",
        "screen capture",
        "capture screen",
        "screenshot",
    ],
    "read": [
        "print its contents",
        "print the contents",
        "read",
    ],
}

TARGET_MAP = {
    "claude": ["claude.ai", "claude"],
    "spotify": ["spotify"],
    "chrome": ["google chrome", "chrome", "browser"],
    "youtube": ["youtube", "yt"],
    "gmail": ["google mail", "gmail", "mail"],
    "vscode": ["visual studio code", "vs code", "vscode", "code editor"],
    "notes": ["apple notes", "notes", "note"],
    "terminal": ["command line", "terminal", "shell"],
    "finder": ["file manager", "finder", "files"],
    "system": ["computer", "brightness", "mac", "system"],
}


def _keyword_in_step(kw: str, step_lower: str) -> bool:
    """Match keyword without matching substrings inside words (e.g. 'load' in 'downloads')."""
    kw = kw.lower()
    if " " in kw:
        return kw in step_lower
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", step_lower))


def _best_keyword_match(step_lower: str, mapping: dict[str, list[str]]) -> str:
    best_action = "unknown"
    best_len = -1
    for name, keywords in mapping.items():
        for kw in sorted(keywords, key=len, reverse=True):
            if _keyword_in_step(kw, step_lower) and len(kw) > best_len:
                best_len = len(kw)
                best_action = name
    return best_action


def split_into_steps(transcript: str) -> List[str]:
    """Split transcript into step strings (multi-pass)."""
    t = transcript.strip()
    if not t:
        return []

    splitter_pattern = "|".join(STEP_SPLITTERS)
    phase1 = re.split(splitter_pattern, t, flags=re.IGNORECASE)

    out: list[str] = []
    for part in phase1:
        phase2 = re.split(STEP_SPLITTERS_AND_ACTION, part, flags=re.IGNORECASE)
        for chunk in phase2:
            cleaned = chunk.strip().strip(",").strip(".").strip()
            if cleaned and len(cleaned) > 1:
                out.append(cleaned)

    return out if out else [t]


def identify_action(step: str) -> str:
    return _best_keyword_match(step.lower().strip(), ACTION_MAP)


def identify_target(step: str) -> str:
    return _best_keyword_match(step.lower().strip(), TARGET_MAP)


def extract_content(step: str, action: str) -> str:
    step_lower = step.lower().strip()
    content = ""

    if action == "type":
        comma_match = re.search(r"[,：:]\s*(.+)$", step)
        if comma_match:
            content = comma_match.group(1).strip()
        else:
            remaining = step_lower
            for kw in sorted(ACTION_MAP["type"], key=len, reverse=True):
                if remaining.startswith(kw):
                    remaining = remaining[len(kw) :].strip()
                    break
            all_targets: list[str] = []
            for keywords in TARGET_MAP.values():
                all_targets.extend(keywords)
            for kw in sorted(all_targets, key=len, reverse=True):
                remaining = remaining.replace(kw, "", 1).strip() if kw in remaining else remaining
            remaining = re.sub(
                r"^(the\s+)?(prompt|message|text|question|query)[,:\s]*",
                "",
                remaining,
            ).strip()
            content = remaining

    elif action == "play":
        by_match = re.search(
            r"(?:play|song|track)\s+(?:the\s+)?(?:song\s+)?(.+?)\s+by\s+(.+?)$",
            step_lower,
        )
        if by_match:
            song = by_match.group(1).strip()
            artist = by_match.group(2).strip()
            content = f"{song} by {artist}"
        else:
            play_match = re.search(
                r"(?:play|put on|listen to|queue)\s+(?:the\s+)?(?:song\s+)?(.+?)$",
                step_lower,
            )
            if play_match:
                content = play_match.group(1).strip()

    elif action == "search":
        search_match = re.search(
            r"(?:search|find|look up|search for|find me)\s+(?:for\s+)?(.+?)$",
            step_lower,
        )
        if search_match:
            content = search_match.group(1).strip()

    elif action == "open":
        remaining = step_lower
        for kw in sorted(ACTION_MAP["open"], key=len, reverse=True):
            if remaining.startswith(kw):
                remaining = remaining[len(kw) :].strip()
                break
        content = remaining

    elif action == "read":
        remaining = step_lower
        for kw in sorted(ACTION_MAP["read"], key=len, reverse=True):
            if remaining.startswith(kw):
                remaining = remaining[len(kw) :].strip()
                break
        content = remaining

    elif action == "volume":
        m = re.search(r"(\d{1,3})\s*%?", step_lower)
        if m:
            content = m.group(1)
        else:
            content = step_lower

    elif action == "screenshot":
        content = ""

    elif action == "submit":
        content = ""

    content = content.strip().strip(",").strip(".").strip()
    content = re.sub(r"\s+", " ", content)
    return content


def parse_command(transcript: str) -> List[CommandStep]:
    """Parse full transcript into ordered CommandStep objects."""
    raw_steps = split_into_steps(transcript)
    parsed_steps: list[CommandStep] = []
    last_known_target = "unknown"

    for i, raw_step in enumerate(raw_steps):
        action = identify_action(raw_step)
        target = identify_target(raw_step)
        if target == "unknown" and last_known_target != "unknown":
            target = last_known_target
        else:
            if target != "unknown":
                last_known_target = target

        content = extract_content(raw_step, action)

        step = CommandStep(
            action=action,
            target=target,
            content=content,
            raw=raw_step,
            index=i,
        )
        parsed_steps.append(step)
        print(
            f"  Step {i + 1}: action={action} target={target} "
            f"content={content!r} raw={raw_step!r}"
        )

    return parsed_steps
