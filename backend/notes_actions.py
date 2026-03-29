"""
Reliable Notes.app automation: pbcopy + ⌘N + ⌘V.

Plain AppleScript `keystroke` of long text often types nothing (focus/timing/quoting).
"""

from __future__ import annotations

import os
import random
import re
import secrets
import subprocess

# Enough variety for long random paragraphs without external deps.
_RANDOM_WORDS = """
the be to of and a in that have I it for not on with he as you do at this but his by from
they we say her she or an will my one all would there their what so up out if about who
get which go me when make can like time no just him know take into year your some could
them see other than then its now look only come its over think also back after use two
way well even new want because any these give day most us is was are were been being
each much such own same few more very still shall should could might must
""".split()


def extract_notes_body(transcript: str) -> str | None:
    """Pull the sentence to put in the note (after type/write/enter)."""
    t = transcript.strip()
    patterns = [
        r"\bnotes\s+type\s*,\s*(.+)$",
        r"\bnotes\s+type\s+(.+)$",
        r"\btype\s*,\s*(.+)$",
        r"\btype\s+(.+)$",
        r"\bwrite\s+(.+)$",
        r"\benter\s+(.+)$",
    ]
    for pat in patterns:
        m = re.search(pat, t, re.I | re.DOTALL)
        if m:
            s = m.group(1).strip().rstrip(".")
            if len(s) >= 1:
                return s
    return None


def wants_random_long_notes_content(transcript: str) -> bool:
    """
    User wants generated filler (random / lorem / N-page), not literal short text.
    Examples: 'random 100-page paragraph', 'lorem ipsum wall of text'.
    """
    t = transcript.lower()
    has_pages = bool(re.search(r"\b\d+\s*-?\s*pages?\b", t))
    has_long = bool(
        re.search(
            r"\b(paragraph|paragraphs|pages?|essay|novel|wall\s+of\s+text|lot\s+of\s+text|long\s+text)\b",
            t,
        )
    )
    has_randomish = bool(
        re.search(
            r"\b(random|lorem|gibberish|nonsense|bogus|fake|filler|dummy)\b",
            t,
        )
    )
    if has_randomish and (has_long or has_pages):
        return True
    if has_pages and has_long:
        return True
    return False


def generate_random_notes_text(transcript: str) -> str:
    """Build pseudo-random English-ish prose up to a size derived from 'N page(s)'."""
    t = transcript.lower()
    m = re.search(r"(\d+)\s*-?\s*pages?", t)
    if m:
        pages = int(m.group(1))
    else:
        # "random paragraph" with no page count — still generate a long note
        pages = int(os.getenv("NOTES_RANDOM_DEFAULT_PAGES", "12"))
    page_cap = int(os.getenv("NOTES_RANDOM_PAGE_CAP", "50"))
    pages = max(1, min(pages, page_cap))

    per_page = int(os.getenv("NOTES_RANDOM_CHARS_PER_PAGE", "2600"))
    max_total = int(os.getenv("NOTES_RANDOM_MAX_CHARS", "100000"))
    target = min(pages * per_page, max_total)

    rng = random.Random(secrets.randbits(128))
    chunks: list[str] = []
    size = 0
    while size < target:
        n_words = rng.randint(10, 28)
        words: list[str] = []
        for _ in range(n_words):
            w = secrets.choice(_RANDOM_WORDS)
            if words and w == words[-1]:
                w = secrets.choice(_RANDOM_WORDS)
            words.append(w)
        sentence = " ".join(words).capitalize() + "."
        chunks.append(sentence)
        size += len(sentence) + 1

    text = " ".join(chunks)
    if len(text) > target:
        text = text[:target]
    # End on word boundary-ish
    sp = text.rfind(" ", 0, target)
    if sp > target * 3 // 4:
        text = text[:sp] + "."
    return text


def resolve_notes_body(transcript: str) -> str | None:
    if wants_random_long_notes_content(transcript):
        return generate_random_notes_text(transcript)
    return extract_notes_body(transcript)


def wants_notes_compose_with_text(transcript: str) -> bool:
    if not re.search(r"\bnotes?\b", transcript, re.I):
        return False
    if not re.search(
        r"\b(type|write|enter|dictate)\b",
        transcript,
        re.I,
    ):
        return False
    return resolve_notes_body(transcript) is not None


def _wants_explicit_save(transcript: str) -> bool:
    return bool(re.search(r"\b(save|saved)\b", transcript, re.I))


def run_notes_new_note_and_paste(body: str, transcript: str = "") -> dict:
    """Activate Notes, new note (⌘N), paste clipboard (filled via pbcopy)."""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    pbcopy_timeout = max(15, min(180, 20 + len(body) // 5000))
    pc = subprocess.run(
        ["/usr/bin/pbcopy"],
        input=body,
        text=True,
        capture_output=True,
        timeout=pbcopy_timeout,
    )
    if pc.returncode != 0:
        err = (pc.stderr or "pbcopy failed").strip()
        return {
            "action": "Could not copy text to the clipboard for Notes.",
            "result": "",
            "osascript_ok": False,
            "method": "notes_pbcopy_paste",
            "detail": err,
        }

    d1 = float(os.getenv("NOTES_AS_DELAY_ACTIVATE", "1.2"))
    d2 = float(os.getenv("NOTES_AS_DELAY_AFTER_NEW", "1.8"))
    d3 = float(os.getenv("NOTES_AS_DELAY_AFTER_PASTE", "0.8"))

    save_block = ""
    if _wants_explicit_save(transcript):
        save_block = f"""
    delay {d3}
    keystroke "s" using command down
"""

    script = f"""tell application "Notes" to activate
delay {d1}
tell application "System Events"
    keystroke "n" using command down
    delay {d2}
    keystroke "v" using command down
{save_block}
end tell
"""
    oa_timeout = max(45, min(300, 60 + len(body) // 3000))
    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=oa_timeout,
    )
    ok = result.returncode == 0
    err = (result.stderr or "").strip()

    pages_requested = ""
    if wants_random_long_notes_content(transcript):
        m = re.search(r"(\d+)\s*-?\s*pages?", transcript, re.I)
        if m:
            pages_requested = f" (~{m.group(1)} pages requested; size capped for Notes performance)"

    action = (
        "Opened Notes, created a new note (⌘N), and pasted your text "
        f"({len(body):,} characters){pages_requested}. Notes saves automatically."
    )
    if _wants_explicit_save(transcript):
        action += " Also pressed ⌘S to save."

    out: dict = {
        "action": action,
        "result": (result.stdout or "").strip(),
        "osascript_ok": ok,
        "method": "notes_pbcopy_paste",
        "typing_ui": True,
        "pasted_chars": len(body),
    }
    if not ok and err:
        out["detail"] = err
    return out
