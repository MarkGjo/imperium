"""Validate generated AppleScript before osascript; optional one-shot Claude fix."""

from __future__ import annotations

import json
import os
import re

import anthropic
from dotenv import load_dotenv

from chrome_helpers import build_chrome_new_tab_script

load_dotenv()

_BANNED_OPEN_LOCATION_MSG = (
    "BANNED: 'open location' opens a new Chrome window. "
    "Use the build_chrome_new_tab_script() template instead."
)

_BANNED_OPEN_A_CHROME_MSG = (
    "BANNED: shell-style 'open -a … Chrome' opens a separate process/window. "
    "Use the build_chrome_new_tab_script() template instead."
)

# Bad standalone URL placeholders — allow Chrome: `set URL of newTab to`, `properties {URL:"..."}`
# (pattern, short name for errors — never expose raw regex to callers)
_UNDEFINED_CHECKS: list[tuple[str, str]] = [
    (r"\bURL\b(?!\s*:)(?!\s*[\"'])(?!\s+of\b)", "bare identifier URL"),
    (r"\btheURL\b", "theURL"),
    (r"\burlString\b", "urlString"),
    (r"\bwebsite\b(?!\s*[\"'])", "website"),
    (r"\baddress\b(?!\s*[\"':)])", "address"),
    (r"\blocation\b(?!\s*[\"'])", "location"),
    (r"\btheAddress\b", "theAddress"),
    (r"\bsiteURL\b", "siteURL"),
    (r"\bemailAddress\b", "emailAddress"),
    (r"\bmessageBody\b", "messageBody"),
    (r"\bmailBody\b", "mailBody"),
]


def _applescript_without_string_literals(script: str) -> str:
    """
    Strip quoted literals so words like 'address' inside user-facing text
    (e.g. 'email address', mailto bodies) do not trigger undefined-variable checks.
    """
    s = script
    # Double-quoted strings (handles simple escapes)
    s = re.sub(r'"((?:[^"\\]|\\.)*)"', '""', s)
    # Single-quoted strings
    s = re.sub(r"'((?:[^'\\]|\\.)*)'", "''", s)
    return s


def validate_applescript(script: str) -> tuple[bool, str]:
    """Return (ok, error_message)."""
    if re.search(r"\bopen location\b", script, re.I):
        return False, _BANNED_OPEN_LOCATION_MSG
    if re.search(r'open\s+-a\s+["\']?Google Chrome', script, re.I):
        return False, _BANNED_OPEN_A_CHROME_MSG
    if re.search(r'open\s+-a\s+["\']?Chrome\b', script, re.I):
        return False, _BANNED_OPEN_A_CHROME_MSG
    if re.search(r"do\s+shell\s+script\s+[\"'][^\"']*open\s+-a", script, re.I):
        return False, _BANNED_OPEN_A_CHROME_MSG
    scan = _applescript_without_string_literals(script)
    for pattern, name in _UNDEFINED_CHECKS:
        if re.search(pattern, scan):
            return False, (
                f"Undefined or unsafe placeholder variable ({name}); "
                "use quoted URL/string literals instead."
            )
    return True, ""


def _parse_json_fix(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text.strip())


_SAFE_CHROME_TAB_EXAMPLE = build_chrome_new_tab_script("ACTUAL_URL_HERE")


def fix_applescript_with_claude(script: str, validation_error: str) -> tuple[str, str]:
    """One retry: ask Haiku to replace URL variables with literals. Returns (script, action)."""
    script_model = os.getenv("ANTHROPIC_SCRIPT_MODEL", "claude-haiku-4-5").strip()
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    open_loc_hint = ""
    if "open location" in validation_error.lower() or "BANNED: 'open location'" in validation_error:
        open_loc_hint = """
The script uses 'open location' which is BANNED because it opens a new Chrome window forcing the user to sign in.
Replace it with this exact template:
tell application "Google Chrome"
    activate
    delay 0.5
    if (count of windows) > 0 then
        tell front window
            set newTab to make new tab at end of tabs
            set URL of newTab to "ACTUAL_URL_HERE"
            set active tab index to (count of tabs)
        end tell
    else
        make new window
        set URL of active tab of front window to "ACTUAL_URL_HERE"
    end if
end tell
"""
    msg = client.messages.create(
        model=script_model,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": f"""This AppleScript has an error: {validation_error}
Original script: {script}
Fix it by replacing ALL variable references with hardcoded string values.
Every URL must be a quoted string literal like 'https://example.com'
Never use variable names like URL, theURL, urlString, website, address.
Replace any 'open location' calls with the safe new tab pattern (never open location — it creates extra windows):
{_SAFE_CHROME_TAB_EXAMPLE}
{open_loc_hint}
Use the same structure: substitute ACTUAL_URL_HERE with the real https URL string inside the set URL of newTab / active tab lines.
Return ONLY fixed JSON: {{ "script": "FIXED_SCRIPT", "action": "ACTION" }}""",
            }
        ],
    )
    raw = msg.content[0].text
    data = _parse_json_fix(raw)
    return data["script"], data.get("action") or "Fixed AppleScript (URL literals)."
