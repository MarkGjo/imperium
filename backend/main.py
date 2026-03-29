from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app_launcher import (
    build_in_app_action_script,
    build_spotify_in_app_bundle,
    lookup_fallback_url,
    open_native_app,
    parse_compound_command,
    resolve_app_or_web,
)
from applescript_validate import fix_applescript_with_claude, validate_applescript
from chrome_helpers import open_chrome_new_empty_tab, open_url_in_chrome
from url_shortcuts import (
    build_shortcut_context,
    resolve_shortcuts,
)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

try:
    from playwright_automation import (
        PLAYWRIGHT_AVAILABLE,
        playwright_enabled,
        run_claude_upload_playwright,
        run_youtube_playwright,
        transcript_wants_claude_upload_flow,
        transcript_wants_youtube_dom_control,
    )
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    playwright_enabled = lambda: False  # noqa: E731
    transcript_wants_youtube_dom_control = lambda _t: False  # noqa: E731
    transcript_wants_claude_upload_flow = lambda _t: False  # noqa: E731

    async def run_youtube_playwright(_t: str) -> dict:  # type: ignore
        return {"error": "playwright_automation module missing"}

    async def run_claude_upload_playwright(_t: str) -> dict:  # type: ignore
        return {"error": "playwright_automation module missing"}

try:
    from notes_actions import (
        resolve_notes_body,
        run_notes_new_note_and_paste,
        wants_notes_compose_with_text,
    )
except ImportError:
    wants_notes_compose_with_text = lambda _t: False  # noqa: E731
    resolve_notes_body = lambda _t: None  # noqa: E731

    def run_notes_new_note_and_paste(_b: str, _t: str = "") -> dict:  # type: ignore
        return {"error": "notes_actions module missing"}

try:
    from code_file_actions import (
        create_vscode_html_file,
        create_vscode_python_file,
        wants_vscode_html_create,
        wants_vscode_python_create,
    )
except ImportError:
    wants_vscode_python_create = lambda _t: False  # noqa: E731
    wants_vscode_html_create = lambda _t: False  # noqa: E731

    def create_vscode_python_file(_t: str) -> dict:  # type: ignore
        return {"error": "code_file_actions module missing"}

    def create_vscode_html_file(_t: str) -> dict:  # type: ignore
        return {"error": "code_file_actions module missing"}


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    verify_chrome_helpers()
    check_chrome_cdp()
    yield


def verify_chrome_helpers() -> None:
    from chrome_helpers import build_chrome_new_tab_script

    test = build_chrome_new_tab_script("https://test.com")
    assert "count of windows" in test, "chrome_helpers broken"
    assert "open location" not in test, "chrome_helpers uses banned pattern"
    assert "make new tab" in test, "chrome_helpers missing new tab logic"
    print("✅ Chrome helpers verified — new tab mode active")


def check_chrome_cdp() -> bool:
    """
    Checks if Chrome is running with remote debugging enabled.
    If not, prints instructions (Playwright Claude prompt uses CDP).
    """
    try:
        cdp = (os.getenv("PLAYWRIGHT_CDP_URL") or "http://localhost:9222").strip()
        base = cdp.rstrip("/")
        json_url = f"{base}/json"
        response = httpx.get(json_url, timeout=2.0)
        ok = response.status_code == 200
        if ok:
            print("✅ Chrome CDP reachable — Playwright can attach to your session")
        return ok
    except Exception:
        print("⚠️  Chrome CDP not detected at PLAYWRIGHT_CDP_URL.")
        print("To enable: quit Chrome, then run:")
        print(
            "  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
            "--remote-debugging-port=9222 &"
        )
        print("Or run ./start_chrome.sh from the project root. Set PLAYWRIGHT_CDP_URL in .env if needed.")
        return False


app = FastAPI(lifespan=_app_lifespan)


def check_accessibility_permission() -> bool:
    """Return True if System Events is allowed (needed for Spotify keyboard automation)."""
    result = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first process',
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0


if not check_accessibility_permission():
    print(
        "WARNING: Accessibility permission may be required for full automation "
        "(Spotify play / System Events)."
    )
    print(
        "Grant access: System Settings → Privacy & Security → Accessibility — "
        "add Terminal or the app running Python."
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


@app.get("/")
async def root():
    return {"status": "Mac is ready for commands"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/meta.json")
async def meta_json():
    return Response(status_code=204)


def _with_shortcuts(d: dict, found: dict[str, str]) -> dict:
    out = dict(d)
    out["shortcuts_resolved"] = found
    return out


def clean_transcript(transcript: str) -> str:
    """Strip parenthetical noise (e.g. STT artifacts) before routing."""
    if not transcript:
        return ""
    t = re.sub(r"\([^)]*\)", "", transcript)
    t = re.sub(r"\s+", " ", t)
    return t.strip().strip(".")


def _transcript_wants_file_read(transcript: str) -> bool:
    """
    Returns True if the transcript is asking to read or list files in Downloads.
    """
    t = transcript.lower().strip()

    list_phrases = [
        "list files",
        "what files",
        "show files",
        "what's in downloads",
        "what is in downloads",
        "list downloads",
        "list my downloads",
        "show downloads",
        "files in downloads",
        "files do i have",
    ]
    if any(p in t for p in list_phrases):
        return True

    wants_app, _ = _transcript_wants_app_open(transcript)
    has_ext = bool(
        re.search(r"\.(?:txt|md|csv|json|py|js|html|log)\b", t)
    )
    if wants_app and not has_ext and "downloads" not in t:
        if not re.search(r"\b(file|document)\b", t):
            return False

    file_indicators = [
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".py",
        ".js",
        ".html",
        ".log",
        "downloads",
        "called",
        "named",
    ]
    has_file_reference = any(f in t for f in file_indicators) or bool(
        re.search(r"\bfile\b", t)
    ) or bool(re.search(r"\bdocument\b", t))

    read_triggers = [
        r"\bread\s+my\b",
        r"\bread\s+the\s+file\b",
        r"\b(read|open|show|load)\s+the\s+[\w\-]+",
        r"\bopen\s+the\s+file\b",
        r"\bopen\s+my\s+file\b",
        r"\bwhat\s+does\b",
        r"\bwhat\s*'\s*s\s+in\b",
        r"\bshow\s+me\b",
        r"\bload\s+the\s+file\b",
        r"\bload\s+my\b",
        r"\b(read|open|show|load)\s+(?!the\s)(?:my\s+)?([a-z0-9][\w\-]*)\s*$",
    ]
    has_read_intent = any(re.search(p, t) for p in read_triggers)

    if not has_read_intent:
        return False
    if has_file_reference:
        return True
    return bool(
        re.search(
            r"\b(read|open|show|load)\s+(?!the\s)(?:my\s+)?([a-z0-9][\w\-]*)\s*$",
            t,
        )
    )


def _transcript_wants_atomic_spotify_play(transcript: str) -> bool:
    """Single-flow Spotify play without multi-step splitting (no 'then' chain)."""
    tl = transcript.lower()
    if "then" in tl or "and then" in tl:
        return False
    if "spotify" not in tl:
        return False
    return bool(re.search(r"\b(play|listen to|put on)\b", tl))


def _extract_spotify_play_query(transcript: str) -> str:
    from app_launcher import parse_compound_command

    c = parse_compound_command(transcript)
    if c.get("is_compound") and "spotify" in (c.get("app_name") or "").lower():
        return (c.get("secondary_action") or "").strip()
    m = re.search(
        r"(?:play|listen to|put on)\s+(.+?)(?:\s+on\s+spotify|\s*$)",
        transcript,
        re.I,
    )
    if m:
        return m.group(1).strip().rstrip(".!?")
    return ""


_SIMPLE_OPEN_INTENT = re.compile(
    r"\b(open|go to|goto|navigate to|launch|visit|show me|take me to)\b",
    re.I,
)


def _transcript_wants_simple_open(transcript: str) -> bool:
    return bool(_SIMPLE_OPEN_INTENT.search(transcript))


def _transcript_wants_claude_prompt(transcript: str) -> tuple[bool, str]:
    """
    Detects if the user wants to send a prompt to Claude.ai.
    Returns (is_claude_prompt, extracted_prompt). Last may be "" if intent
    is clear but text could not be extracted (caller may open claude.ai).
    """
    transcript_lower = transcript.lower().strip()

    if "claude" not in transcript_lower and "claud" not in transcript_lower:
        return False, ""

    action_phrases = (
        "type in the prompt",
        "type the prompt",
        "type in",
        "send the prompt",
        "send prompt",
        "ask claude",
        "prompt claude",
        "tell claude",
        "enter the prompt",
    )
    has_action = any(p in transcript_lower for p in action_phrases)
    if not has_action:
        has_action = bool(
            re.search(r"\b(submit|send|write|input)\b", transcript_lower)
        )
    if not has_action:
        has_action = bool(re.search(r"\btype\b", transcript_lower))
    if not has_action:
        has_action = bool(re.search(r"\benter\b", transcript_lower))
    if not has_action:
        return False, ""

    quoted = re.search(
        r'["\u201c\u201d\u2018\u2019](.+?)["\u201c\u201d\u2018\u2019]',
        transcript,
        re.DOTALL,
    )
    if quoted:
        return True, quoted.group(1).strip()

    claude_colon = re.search(
        r"claude\s*:\s*(.+)$", transcript, re.I | re.DOTALL
    )
    if claude_colon:
        extracted = claude_colon.group(1).strip()
        extracted = re.sub(
            r"\s+and\s+(then\s+)?(submit|send|click|press|hit)\s*.*$",
            "",
            extracted,
            flags=re.I,
        ).strip()
        if len(extracted) > 2:
            return True, extracted

    after_prompt = re.search(
        r"(?:prompt[,:\s]+)(.+?)(?:\s+and\s+(?:then\s+)?(?:submit|send|click|press).*)?$",
        transcript,
        re.I | re.DOTALL,
    )
    if after_prompt:
        extracted = after_prompt.group(1).strip()
        extracted = re.sub(
            r"\s+and\s+(then\s+)?(submit|send|click|press|hit)\s*$",
            "",
            extracted,
            flags=re.I,
        ).strip()
        if len(extracted) > 2:
            return True, extracted

    after_type = re.search(
        r"(?:type\s+in|type|write|input|enter)\s+(?:the\s+)?(?:prompt\s+)?[,:\s]*(.+?)(?:\s+and\s+(?:then\s+)?(?:submit|send|click|press).*)?$",
        transcript,
        re.I | re.DOTALL,
    )
    if after_type:
        extracted = after_type.group(1).strip()
        extracted = re.sub(
            r"\s+and\s+(then\s+)?(submit|send|click|press|hit)\s*$",
            "",
            extracted,
            flags=re.I,
        ).strip()
        if (
            extracted
            and extracted.lower() != "claude"
            and len(extracted) > 2
        ):
            return True, extracted

    after_ask = re.search(
        r"ask\s+claude\s+(?:to\s+)?(.+?)(?:\s+and\s+(?:then\s+)?(?:submit|send|click).*)?$",
        transcript,
        re.I | re.DOTALL,
    )
    if after_ask:
        extracted = after_ask.group(1).strip()
        if len(extracted) > 2:
            return True, extracted

    send_msg = re.search(
        r"send\s+(?:the\s+)?(?:prompt|message)\s*[:,]?\s*(.+)$",
        transcript,
        re.I | re.DOTALL,
    )
    if send_msg:
        extracted = send_msg.group(1).strip()
        extracted = re.sub(
            r"\s+and\s+(then\s+)?(submit|send|click|press|hit)\s*$",
            "",
            extracted,
            flags=re.I,
        ).strip()
        if len(extracted) > 1:
            return True, extracted

    return True, ""


_APP_OPEN_TRIGGERS = (
    "pull up",
    "bring up",
    "switch to",
    "open",
    "launch",
    "start",
    "run",
    "load",
)


def _transcript_wants_app_open(transcript: str) -> tuple[bool, str]:
    """True if the user wants to open a named app (not a generic website/browser request)."""
    transcript_lower = transcript.lower().strip().rstrip(".!?")
    web_only = (
        "website",
        "webpage",
        "web",
        "browser",
        "tab",
        "chrome",
        "safari",
        "url",
        "link",
        "in browser",
        "in chrome",
        "in safari",
    )
    for trigger in sorted(_APP_OPEN_TRIGGERS, key=len, reverse=True):
        pattern = rf"\b{re.escape(trigger)}\s+(.+)$"
        match = re.search(pattern, transcript_lower)
        if not match:
            continue
        app_name = match.group(1).strip()
        app_name = re.sub(r"\s+app$", "", app_name, flags=re.I).strip()
        if not app_name:
            continue
        if any(w in app_name for w in web_only):
            continue
        return True, app_name
    return False, ""


def _parse_json_from_claude(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text.strip())


def get_applescript(command: str, shortcut_context: str = "") -> tuple[str, str]:
    script_model = os.getenv(
        "ANTHROPIC_SCRIPT_MODEL", "claude-haiku-4-5"
    ).strip()
    sc = shortcut_context.strip()
    shortcut_block = f"{sc}\n" if sc else ""
    message = claude.messages.create(
        model=script_model,
        max_tokens=1200,
        messages=[
            {
                "role": "user",
                "content": f"""You are an expert Mac automation engineer.
Convert voice commands into AppleScript that executes on macOS.

{shortcut_block}
RULE ZERO — CHROME WINDOWS (NON-NEGOTIABLE):
NEVER use these in any AppleScript you generate:
- open location "..."  ← BANNED, opens new window
- make new window  ← BANNED unless (count of windows) = 0
- do shell script "open -a Google Chrome ..."  ← BANNED

ALWAYS use this EXACT template for opening ANY URL in Chrome:
tell application "Google Chrome"
    activate
    delay 0.5
    if (count of windows) > 0 then
        tell front window
            set newTab to make new tab at end of tabs
            set URL of newTab to "HARDCODED_URL_HERE"
            set active tab index to (count of tabs)
        end tell
    else
        make new window
        set URL of active tab of front window to "HARDCODED_URL_HERE"
    end if
end tell

This is the ONLY acceptable way to open a URL in Chrome.
No exceptions. No variations.

CRITICAL RULES:
1. Return ONLY valid JSON with "script" and "action" fields
2. NEVER use variable names for URLs — always inline as hardcoded strings in the Chrome script below
3. NEVER use "open location" for Chrome — it opens a new window and logs the user out of session
4. NEVER use make new window if Chrome already has at least one window
5. action must honestly describe what will happen

BANNED WORDS — never use these as AppleScript variable names (always inline as quoted string literals):
URL, address, theURL, urlString, website, location, theAddress, siteURL, emailAddress,
messageBody, mailBody, theMessage, recipient, subject (as a variable), message (as a variable).

NATIVE APP RULE:
If the user asks to open an app by name, prefer using:
tell application "APP NAME" to activate
This opens the native Mac app if installed.
Only use Chrome + URL if the user specifically says
'website', 'web', 'in browser', or 'in Chrome'.
Examples:
- 'open spotify' → tell application "Spotify" to activate
- 'open spotify website' → Chrome with https://open.spotify.com (or shortcut URL)
- 'open notion' → tell application "Notion" to activate
- 'open notion in browser' → Chrome with notion.so URL template

CRITICAL CHROME RULE — NEVER open a new Chrome window when one already exists:
Always check if Chrome has existing windows first.
Use the same template as RULE ZERO for every URL open in Google Chrome (replace HARDCODED_URL_HERE with the real https URL string).

Never use open location — it creates a new window.
Never use make new window if a window already exists.
Always put the actual URL as a hardcoded string — never a variable.

CHROME & BROWSER RULES (URLs):
- YouTube search: https://www.youtube.com/results?search_query=TERM (encode spaces as +)
- YouTube channel: https://www.youtube.com/@CHANNELNAME/videos
- Google search: https://www.google.com/search?q=TERM
- Never try to click buttons — user navigates by URL in the tab you opened
- Multi-tab: repeat the same tell block for each URL (or chain multiple new tabs inside one tell front window)

EMAIL RULES:
- Use Mail app for sending emails
- Always set subject, body, recipient before sending
- Call send at the end
- If no recipient given use placeholder and say so in action

iMESSAGE / MESSAGES RULES:
- To send an iMessage, you MUST use a phone number or email address, NOT a contact name
- If user says "text [name]" or "message [name]", ask them to provide the phone number in the action response
- If user provides a phone number, use this EXACT template:
tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant "+1XXXXXXXXXX" of targetService
    send "MESSAGE_TEXT_HERE" to targetBuddy
end tell
- Replace +1XXXXXXXXXX with the actual phone number (include country code)
- If no phone number provided, just open Messages app and explain in action that user needs to provide phone number
- Example: "text 555-123-4567 saying hello" → use participant "+15551234567"

EMAIL WITH ATTACHMENT — use this EXACT template:
tell application "Mail"
    set theMessage to make new outgoing message with properties {{subject:"SUBJECT_HERE", content:"BODY_HERE", visible:true}}
    tell theMessage
        make new to recipient at end of to recipients with properties {{address:"EMAIL_HERE"}}
        make new attachment with properties {{file name:POSIX file "/Users/jasmansidhu/Desktop/FILENAME_HERE"}} at after the last paragraph
    end tell
    activate
end tell
- Replace SUBJECT_HERE, BODY_HERE, EMAIL_HERE, FILENAME_HERE with actual values
- For files on Desktop, use: /Users/jasmansidhu/Desktop/filename.ext
- For files in Downloads, use: /Users/jasmansidhu/Downloads/filename.ext
- For files in Documents, use: /Users/jasmansidhu/Documents/filename.ext
- Always include the file extension (.txt, .pdf, .rtf, etc.)

FILE & FOLDER (Finder): use path to desktop folder / Finder objects — avoid broken POSIX folder paths with trailing slashes (Finder -1728).

SYSTEM RULES:
- Volume: set volume output volume X where X is 0-100
- Screenshots: do shell script "screencapture ~/Desktop/screenshot.png"
- Opening apps: tell application "APP NAME" to activate
- Quitting: tell application "APP NAME" to quit

HONEST ACTION EXAMPLES:
- "Opened YouTube search for MrBeast — click the video you want"
- "Opened Claude.ai at https://claude.ai"
- "Set system volume to 40%"

Command: {command}

Return ONLY this JSON — no markdown, no explanation:
{{
    "script": "YOUR APPLESCRIPT WITH ALL URLS HARDCODED AS STRINGS",
    "action": "Honest description of what will happen"
}}""",
            }
        ],
    )
    raw = message.content[0].text
    print(f"CLAUDE RAW RESPONSE:\n{raw[:1000]}{'...' if len(raw) > 1000 else ''}")
    try:
        data = _parse_json_from_claude(raw)
    except json.JSONDecodeError as e:
        print(f"JSON PARSE ERROR: {e}")
        print(f"FULL RESPONSE:\n{raw}")
        raise
    return data["script"], data["action"]


def extract_url_from_transcript(transcript: str) -> str | None:
    m = re.search(r"https?://[^\s\]\)]+", transcript, re.I)
    if m:
        return m.group(0).rstrip(".,;)")
    tl = transcript.lower()
    if "google.com" in tl.replace(" ", ""):
        return "https://www.google.com"
    if re.search(r"\bgo to google\b", tl) or re.search(r"\bgoogle\.com\b", tl):
        return "https://www.google.com"
    if "youtube.com" in tl.replace(" ", "") or re.search(r"\byoutube\.com\b", tl):
        return "https://www.youtube.com"
    if re.search(r"\byoutu\.be\b", tl):
        return "https://www.youtube.com"
    return None


_COMPLEX_CLI_SKIP = re.compile(
    r"\b("
    r"navigate|navigation|play|playing|played|video|videos|channel|channels|"
    r"tab|tabs|click|scroll|subscribe|playlist|upload|uploads|recent|search|"
    r"watch|mrbeast|youtube|youtu|open a new|new tab|and then|most recent|"
    r"latest|first video|second|upload\b"
    r")\b",
    re.I,
)


def transcript_needs_full_automation(transcript: str) -> bool:
    """If True, skip the dumb `open -a Chrome` shortcut — use Claude + AppleScript."""
    t = transcript.strip()
    if len(t) > 130:
        return True
    if t.count(",") >= 2:
        return True
    if _COMPLEX_CLI_SKIP.search(t):
        return True
    if t.count(" and ") >= 2:
        return True
    return False


def transcript_mentions_chrome(transcript: str) -> bool:
    t = transcript.lower()
    return "chrome" in t or "google chrome" in t


def _osascript_pipe(script: str) -> dict:
    """Run AppleScript via stdin (no validation)."""
    script = script.strip()
    if not script:
        return {"stdout": "", "stderr": "empty script", "returncode": 1}
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stderr = (result.stderr or "").strip()
        if result.returncode != 0 and stderr:
            print(f"AppleScript error: {stderr}")
        return {
            "stdout": result.stdout or "",
            "stderr": stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        print("AppleScript: command timed out")
        return {
            "stdout": "",
            "stderr": "Error: AppleScript timed out after 30s",
            "returncode": 1,
        }
    except OSError as e:
        print(f"AppleScript run failed: {e}")
        return {
            "stdout": "",
            "stderr": f"Error: {e}",
            "returncode": 1,
        }


def run_open_google_chrome(url: str | None = None) -> dict:
    """New tab in existing Chrome window — uses chrome_helpers only (never `open -a` / `open location`)."""
    if url:
        ok, err = open_url_in_chrome(url)
    else:
        ok, err = open_chrome_new_empty_tab()
    if not ok:
        print(f"Chrome tab open failed: {err}")
        return {"stdout": "", "stderr": err or "", "returncode": 1}
    return {"stdout": "", "stderr": "", "returncode": 0}


async def attempt_chrome_open_cli(
    transcript: str, found_shortcuts: dict[str, str] | None = None
) -> dict | None:
    """
    Simple open / navigate + shortcut URL or Chrome + URL / blank tab.
    Always uses open_url_in_chrome (new tab on existing window).
    """
    if transcript_needs_full_automation(transcript):
        return None

    transcript_lower = transcript.lower()
    found = found_shortcuts or {}

    if _transcript_wants_simple_open(transcript):
        target_url = None
        for keyword in sorted(found.keys(), key=len, reverse=True):
            if keyword in transcript_lower:
                target_url = found[keyword]
                break
        if target_url:
            ok, err = open_url_in_chrome(target_url)
            if not ok:
                print(f"Chrome tab open failed: {err}")
                return None
            return {
                "transcript": transcript,
                "action": f"Opened {target_url} in new Chrome tab",
                "result": "",
                "osascript_ok": True,
                "method": "chrome_new_tab",
            }

    if not transcript_mentions_chrome(transcript):
        return None

    url = extract_url_from_transcript(transcript)
    if url:
        ok, err = open_url_in_chrome(url)
        if not ok:
            print(f"Chrome tab open failed: {err}")
            return None
        return {
            "transcript": transcript,
            "action": f"Opened a new Chrome tab (your existing window) → {url}",
            "result": "",
            "osascript_ok": True,
            "method": "chrome_new_tab",
        }

    ok, err = open_chrome_new_empty_tab()
    if not ok:
        print(f"Chrome tab open failed: {err}")
        return None
    return {
        "transcript": transcript,
        "action": "Opened a new tab in Google Chrome (your existing window)",
        "result": "",
        "osascript_ok": True,
        "method": "chrome_new_tab",
    }


def run_applescript(script: str, original_action: str = "") -> tuple[dict, str]:
    """Validate (one Claude fix retry), then run AppleScript via stdin."""
    script = script.strip()
    if not script:
        return {"stdout": "", "stderr": "empty script", "returncode": 1}, original_action

    ok, err = validate_applescript(script)
    if not ok:
        try:
            script, fixed_action = fix_applescript_with_claude(script, err)
        except Exception as e:
            return (
                {
                    "stdout": "",
                    "stderr": f"AppleScript validation: {err}. Fix failed: {e}",
                    "returncode": 1,
                },
                original_action,
            )
        ok2, err2 = validate_applescript(script)
        if not ok2:
            return (
                {
                    "stdout": "",
                    "stderr": f"AppleScript invalid after fix: {err2}",
                    "returncode": 1,
                },
                original_action,
            )
        return _osascript_pipe(script), fixed_action

    return _osascript_pipe(script), original_action


def _wants_project_creation(command: str) -> bool:
    """Detect if user wants to create a coding project/app."""
    cl = command.lower()
    create_words = ["create", "make", "build", "generate", "code"]
    project_words = ["app", "application", "project", "website", "calculator", "game", "todo", "page", "site"]
    has_create = any(w in cl for w in create_words)
    has_project = any(w in cl for w in project_words)
    return has_create and has_project


def _generate_project_with_claude(command: str) -> dict:
    """Use Claude to generate a complete project with multiple files."""
    project_model = os.getenv("ANTHROPIC_SCRIPT_MODEL", "claude-haiku-4-5").strip()
    
    message = claude.messages.create(
        model=project_model,
        max_tokens=8000,
        messages=[
            {
                "role": "user",
                "content": f"""You are creating a web project based on this request: {command}

Create a SINGLE index.html file that contains EVERYTHING inline:
- All CSS in a <style> tag
- All JavaScript in a <script> tag  
- Complete, working, polished code

Requirements:
- Dark modern UI with good styling
- Fully functional (not placeholder code)
- Mobile-friendly
- Professional looking

Return ONLY valid JSON in this exact format:
{{
    "project_name": "FolderName",
    "description": "One sentence description",
    "index_html": "<!DOCTYPE html>... complete HTML file content ..."
}}

CRITICAL: 
- Escape all quotes inside the HTML string properly
- Use single quotes inside HTML attributes when possible
- The index_html must be valid JSON string (escape newlines as \\n, quotes as \\")
- Return ONLY the JSON, no markdown code blocks"""
            }
        ],
    )
    
    raw = message.content[0].text
    print(f"PROJECT GENERATION RESPONSE LENGTH: {len(raw)}")
    
    # Try to parse as JSON first
    try:
        # Clean up markdown code blocks
        clean = raw.strip()
        if clean.startswith("```"):
            lines = clean.split("\n", 1)
            if len(lines) > 1:
                clean = lines[1]
        if clean.rstrip().endswith("```"):
            clean = clean.rstrip()[:-3].rstrip()
        
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start != -1 and end > start:
            clean = clean[start:end]
        
        return json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"JSON parsing failed: {e}")
        print("Extracting HTML directly...")
        
        # Try to get project name from response
        name_match = re.search(r'"project_name"\s*:\s*"([^"]+)"', raw)
        project_name = name_match.group(1) if name_match else "WebApp"
        
        # The HTML is likely escaped in JSON format - extract it
        html_match = re.search(r'"index_html"\s*:\s*"(.*?)(?:"\s*[,}]|\Z)', raw, re.DOTALL)
        if html_match:
            html_escaped = html_match.group(1)
            # Unescape JSON string escapes
            html_content = html_escaped.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
            if '<!DOCTYPE' in html_content or '<html' in html_content.lower():
                print(f"Extracted HTML: {len(html_content)} chars")
                return {
                    "project_name": project_name,
                    "description": "Generated web application",
                    "index_html": html_content
                }
        
        # Fallback: look for raw HTML (unescaped)
        html_match = re.search(r'<!DOCTYPE html>.*?</html>', raw, re.DOTALL | re.IGNORECASE)
        if html_match:
            return {
                "project_name": project_name,
                "description": "Generated web application",
                "index_html": html_match.group(0)
            }
        
        # Last resort: look for any HTML-like content
        if "<html" in raw.lower():
            start = raw.lower().find("<html")
            end = raw.lower().rfind("</html>") + 7
            if end > start:
                return {
                    "project_name": project_name,
                    "description": "Generated web application", 
                    "index_html": raw[start:end]
                }
        
        print(f"RAW RESPONSE SAMPLE: {raw[:1000]}")
        raise ValueError("Could not extract HTML from response")


def _wants_git_command(command: str) -> bool:
    """Detect if user wants to run a git command."""
    cl = command.lower()
    
    # Explicit git commands - check these FIRST
    git_phrases = [
        "git status", 
        "git diff", 
        "git pull",
        "git push",
        "push to github", 
        "commit changes", 
        "commit my changes",
        "commit the changes",
        "push changes",
        "pull changes",
        "pull from github",
        "commit in ",
        "push in ",
        "status for ",
        "status of ",
        "with message",  # "commit ... with message ..."
    ]
    if any(phrase in cl for phrase in git_phrases):
        return True
    
    # Pattern: "commit ... message" (git commit message pattern)
    if re.search(r'\bcommit\b.*\bmessage\b', cl):
        return True
    
    # Pattern: "push [something] to github"
    if re.search(r'\bpush\b.*\bto\s+github\b', cl):
        return True
    
    # If it's a text/SMS message command, don't route to git
    text_indicators = ["text ", "send a text", "send text", "sms ", "imessage ", "send a message"]
    if any(t in cl for t in text_indicators):
        return False
    
    # If it mentions opening Messages app, don't route to git
    if "open " in cl and "messages" in cl:
        return False
        
    return False


def _parse_git_command(command: str) -> dict:
    """Parse a natural language git command into action and parameters."""
    cl = command.lower()
    
    result = {
        "action": None,
        "message": None,
        "folder": None,
    }
    
    # Detect action
    if "push" in cl:
        result["action"] = "push"
    elif "pull" in cl:
        result["action"] = "pull"
    elif "status" in cl:
        result["action"] = "status"
    elif "diff" in cl:
        result["action"] = "diff"
    elif "commit" in cl:
        result["action"] = "commit"
        # Extract commit message
        patterns = [
            r'message\s+["\']?([^"\']+)["\']?',
            r'saying\s+["\']?([^"\']+)["\']?',
            r'with\s+["\']([^"\']+)["\']',
            r'commit\s+["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, command, re.I)
            if match:
                result["message"] = match.group(1).strip()
                break
        if not result["message"]:
            # Try to get text after "message" or "saying"
            after_msg = re.search(r'(?:message|saying|with message)\s+(.+?)(?:\s+(?:in|for|to)\s|$)', command, re.I)
            if after_msg:
                result["message"] = after_msg.group(1).strip().strip('"\'')
    
    # Detect folder
    folder_patterns = [
        r'(?:in|for|from)\s+(?:the\s+)?(?:folder\s+)?["\']?([^"\']+?)["\']?\s*(?:folder|project|repo)?(?:\s|$)',
        r'(?:folder|project|repo)\s+(?:called\s+)?["\']?([^"\']+)["\']?',
        r'\bpush\s+([A-Za-z0-9_-]+)\s+to\s+github\b',  # "push ProjectName to github"
        r'\bstatus\s+(?:for|of)\s+([A-Za-z0-9_-]+)\b',  # "status for ProjectName"
    ]
    for pattern in folder_patterns:
        match = re.search(pattern, command, re.I)
        if match:
            folder = match.group(1).strip()
            # Clean up common words
            folder = re.sub(r'\s*(folder|project|repo|repository|changes?|and|then|push).*$', '', folder, flags=re.I).strip()
            if folder and len(folder) > 1:
                result["folder"] = folder
                break
    
    return result


@app.post("/git-command")
async def git_command(data: dict):
    """Execute git commands remotely."""
    command = data.get("command", "")
    if not command.strip():
        return {"error": "No command provided"}
    
    print(f"\n{'='*60}")
    print(f"GIT COMMAND: {command}")
    print(f"{'='*60}")
    
    parsed = _parse_git_command(command)
    action = parsed.get("action")
    message = parsed.get("message")
    folder_name = parsed.get("folder")
    
    print(f"PARSED: action={action}, message={message}, folder={folder_name}")
    
    if not action:
        return {"transcript": command, "error": "Could not understand git command. Try: 'commit changes with message fixed the bug' or 'push to github'"}
    
    # Determine working directory
    desktop = Path.home() / "Desktop"
    work_dir = None
    
    if folder_name:
        # Check Desktop first
        potential_path = desktop / folder_name
        if potential_path.exists() and potential_path.is_dir():
            work_dir = potential_path
        else:
            # Check Documents
            docs_path = Path.home() / "Documents" / folder_name
            if docs_path.exists() and docs_path.is_dir():
                work_dir = docs_path
            else:
                # Try to find it
                for search_dir in [desktop, Path.home() / "Documents", Path.home() / "Projects"]:
                    if search_dir.exists():
                        for item in search_dir.iterdir():
                            if item.is_dir() and folder_name.lower() in item.name.lower():
                                work_dir = item
                                break
                    if work_dir:
                        break
    
    if not work_dir:
        # Default to most recently modified git repo on Desktop
        git_repos = []
        if desktop.exists():
            for item in desktop.iterdir():
                if item.is_dir() and (item / ".git").exists():
                    git_repos.append(item)
        
        if git_repos:
            # Sort by modification time, most recent first
            git_repos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            work_dir = git_repos[0]
        else:
            return {"transcript": command, "error": "No git repository found. Specify a folder: 'commit changes in MyProject with message updated code'"}
    
    # Verify it's a git repo
    if not (work_dir / ".git").exists():
        return {"transcript": command, "error": f"'{work_dir.name}' is not a git repository"}
    
    print(f"WORKING DIR: {work_dir}")
    
    try:
        if action == "status":
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=30
            )
            status_output = result.stdout.strip() or "No changes"
            return {
                "transcript": command,
                "action": f"Git status for {work_dir.name}:\n{status_output}",
                "osascript_ok": result.returncode == 0,
                "result": status_output
            }
        
        elif action == "diff":
            result = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=30
            )
            diff_output = result.stdout.strip() or "No changes"
            return {
                "transcript": command,
                "action": f"Git diff for {work_dir.name}:\n{diff_output}",
                "osascript_ok": result.returncode == 0,
                "result": diff_output
            }
        
        elif action == "commit":
            if not message:
                return {"transcript": command, "error": "Please provide a commit message: 'commit changes with message your message here'"}
            
            # Stage all changes
            subprocess.run(["git", "add", "-A"], cwd=str(work_dir), capture_output=True, timeout=30)
            
            # Check if there's anything to commit
            status = subprocess.run(["git", "status", "--porcelain"], cwd=str(work_dir), capture_output=True, text=True, timeout=30)
            if not status.stdout.strip():
                return {"transcript": command, "action": f"No changes to commit in {work_dir.name}", "osascript_ok": True}
            
            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                return {
                    "transcript": command,
                    "action": f"Committed to {work_dir.name} with message: '{message}'",
                    "osascript_ok": True,
                    "result": result.stdout
                }
            else:
                return {
                    "transcript": command,
                    "error": f"Commit failed: {result.stderr}",
                    "osascript_ok": False
                }
        
        elif action == "push":
            result = subprocess.run(
                ["git", "push"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                return {
                    "transcript": command,
                    "action": f"Pushed {work_dir.name} to GitHub",
                    "osascript_ok": True,
                    "result": result.stdout or result.stderr
                }
            else:
                return {
                    "transcript": command,
                    "error": f"Push failed: {result.stderr}",
                    "osascript_ok": False
                }
        
        elif action == "pull":
            result = subprocess.run(
                ["git", "pull"],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                return {
                    "transcript": command,
                    "action": f"Pulled latest changes for {work_dir.name}",
                    "osascript_ok": True,
                    "result": result.stdout
                }
            else:
                return {
                    "transcript": command,
                    "error": f"Pull failed: {result.stderr}",
                    "osascript_ok": False
                }
        
    except subprocess.TimeoutExpired:
        return {"transcript": command, "error": "Git command timed out"}
    except Exception as e:
        return {"transcript": command, "error": str(e)}
    
    return {"transcript": command, "error": "Unknown git action"}


@app.post("/create-project")
async def create_project(data: dict):
    """Create a complete web project from a description."""
    command = data.get("command", "")
    if not command.strip():
        return {"error": "No project description provided"}
    
    print(f"\n{'='*60}")
    print(f"CREATE PROJECT: {command}")
    print(f"{'='*60}")
    
    try:
        project = _generate_project_with_claude(command)
        project_name = project.get("project_name", "MyProject")
        description = project.get("description", "")
        index_html = project.get("index_html", "")
        
        # Create folder on Desktop
        desktop = Path.home() / "Desktop"
        project_path = desktop / project_name
        project_path.mkdir(exist_ok=True)
        
        # Write the HTML file
        html_file = project_path / "index.html"
        html_file.write_text(index_html)
        
        print(f"Created: {project_path}")
        print(f"Files: index.html ({len(index_html)} chars)")
        
        # Open in VS Code
        subprocess.run(["open", "-a", "Visual Studio Code", str(project_path)], check=False)
        
        # Open in Chrome
        subprocess.run(["open", "-a", "Google Chrome", str(html_file)], check=False)
        
        return {
            "transcript": command,
            "action": f"Created {project_name} on Desktop with index.html, opened in VS Code and Chrome",
            "project_path": str(project_path),
            "description": description,
            "osascript_ok": True
        }
        
    except json.JSONDecodeError as e:
        print(f"JSON ERROR: {e}")
        return {"transcript": command, "error": f"Failed to parse project: {e}"}
    except Exception as e:
        print(f"ERROR: {e}")
        return {"transcript": command, "error": str(e)}


def _wants_spotify_control(command: str) -> bool:
    """Detect if user wants to control Spotify."""
    cl = command.lower()
    if "spotify" not in cl:
        return False
    action_words = ["play", "search", "open", "pause", "skip", "next", "previous", "shuffle", "repeat", "library", "playlist", "liked"]
    return any(w in cl for w in action_words)


def _parse_spotify_command(command: str) -> dict:
    """Parse a Spotify command."""
    cl = command.lower()
    result = {"action": None, "query": None, "playlist": None}
    
    # Detect action
    if "pause" in cl or "stop" in cl:
        result["action"] = "pause"
    elif "skip" in cl or "next" in cl:
        result["action"] = "next"
    elif "previous" in cl or "back" in cl:
        result["action"] = "previous"
    elif "shuffle" in cl:
        result["action"] = "shuffle"
    elif "play" in cl or "search" in cl:
        result["action"] = "play"
        
        # Check for playlist
        playlist_match = re.search(r'(?:play|open)\s+(?:my\s+)?(?:playlist\s+)?["\']?([^"\']+?)["\']?\s+playlist', command, re.I)
        if playlist_match:
            result["playlist"] = playlist_match.group(1).strip()
            result["action"] = "playlist"
        elif "liked songs" in cl or "liked" in cl:
            result["action"] = "liked"
        else:
            # Extract song/artist query
            patterns = [
                r'play\s+(.+?)(?:\s+on\s+spotify|\s*$)',
                r'search\s+(?:for\s+)?(.+?)(?:\s+on\s+spotify|\s*$)',
                r'spotify\s+(?:and\s+)?play\s+(.+?)(?:\s*$)',
            ]
            for pattern in patterns:
                match = re.search(pattern, command, re.I)
                if match:
                    query = match.group(1).strip()
                    # Clean up common words
                    query = re.sub(r'\s*(?:on spotify|in spotify).*$', '', query, flags=re.I).strip()
                    if query and query.lower() != "spotify":
                        result["query"] = query
                        break
    
    return result


@app.post("/spotify-control")
async def spotify_control(data: dict):
    """Control Spotify with search and playback."""
    command = data.get("command", "")
    if not command.strip():
        return {"error": "No command provided"}
    
    print(f"\n{'='*60}")
    print(f"SPOTIFY: {command}")
    print(f"{'='*60}")
    
    parsed = _parse_spotify_command(command)
    action = parsed.get("action")
    query = parsed.get("query")
    playlist = parsed.get("playlist")
    
    print(f"PARSED: action={action}, query={query}, playlist={playlist}")
    
    if action == "pause":
        script = 'tell application "Spotify" to pause'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return {"transcript": command, "action": "Paused Spotify", "osascript_ok": result.returncode == 0}
    
    elif action == "next":
        script = 'tell application "Spotify" to next track'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return {"transcript": command, "action": "Skipped to next track", "osascript_ok": result.returncode == 0}
    
    elif action == "previous":
        script = 'tell application "Spotify" to previous track'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return {"transcript": command, "action": "Playing previous track", "osascript_ok": result.returncode == 0}
    
    elif action == "shuffle":
        script = '''
        tell application "Spotify"
            set shuffling to not shuffling
            if shuffling then
                return "on"
            else
                return "off"
            end if
        end tell
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        state = result.stdout.strip()
        return {"transcript": command, "action": f"Shuffle turned {state}", "osascript_ok": result.returncode == 0}
    
    elif action == "liked":
        # Open liked songs
        script = '''
        tell application "Spotify"
            activate
            delay 0.5
        end tell
        tell application "System Events"
            tell process "Spotify"
                keystroke "l" using {command down, shift down}
            end tell
        end tell
        delay 1
        tell application "Spotify" to play
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        return {"transcript": command, "action": "Playing your Liked Songs", "osascript_ok": result.returncode == 0}
    
    elif action == "play" and query:
        # Search and play
        escaped_query = query.replace('"', '\\"').replace("'", "'")
        script = f'''
        tell application "Spotify"
            activate
            delay 1
        end tell
        tell application "System Events"
            tell process "Spotify"
                -- Open search with Cmd+K (Spotify's search shortcut)
                keystroke "k" using {{command down}}
                delay 0.5
                -- Clear existing text
                keystroke "a" using {{command down}}
                delay 0.2
                -- Type search query
                keystroke "{escaped_query}"
                delay 1.5
                -- Press Enter to play top result
                key code 36
            end tell
        end tell
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        
        if result.returncode == 0:
            return {
                "transcript": command,
                "action": f"Searching and playing '{query}' on Spotify",
                "osascript_ok": True
            }
        else:
            return {
                "transcript": command,
                "error": f"Spotify control failed: {result.stderr}",
                "osascript_ok": False,
                "hint": "Make sure Spotify is installed and you've granted Accessibility permissions in System Settings > Privacy & Security > Accessibility"
            }
    
    elif action == "playlist" and playlist:
        # Search for playlist
        escaped_playlist = playlist.replace('"', '\\"').replace("'", "'")
        script = f'''
        tell application "Spotify"
            activate
            delay 1
        end tell
        tell application "System Events"
            tell process "Spotify"
                keystroke "k" using {{command down}}
                delay 0.5
                keystroke "a" using {{command down}}
                delay 0.2
                keystroke "{escaped_playlist} playlist"
                delay 1.5
                -- Navigate down to first result
                key code 125
                delay 0.3
                -- Press Enter to open playlist
                key code 36
                delay 1
                -- Press play button (space or Enter)
                key code 36
            end tell
        end tell
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        return {
            "transcript": command,
            "action": f"Playing playlist '{playlist}'",
            "osascript_ok": result.returncode == 0
        }
    
    else:
        # Just open Spotify
        script = 'tell application "Spotify" to activate'
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return {"transcript": command, "action": "Opened Spotify", "osascript_ok": True}


def _wants_text_message(command: str) -> bool:
    """Detect if user wants to send a text/iMessage."""
    cl = command.lower()
    patterns = [
        r'\btext\s+\w+',
        r'\bmessage\s+\w+',
        r'\bsend\s+(?:a\s+)?(?:text|message|sms)',
        r'\bitext\s+',
        r'\bimessage\s+',
    ]
    return any(re.search(p, cl) for p in patterns)


def _parse_text_message(command: str) -> dict:
    """Parse a text message command into recipient and message."""
    result = {"recipient": None, "message": None, "is_phone": False}
    
    # Check for phone number pattern
    phone_match = re.search(r'(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})', command)
    if phone_match:
        # Clean up phone number
        phone = re.sub(r'[^\d+]', '', phone_match.group(1))
        if not phone.startswith('+'):
            if len(phone) == 10:
                phone = '+1' + phone
            elif len(phone) == 11 and phone.startswith('1'):
                phone = '+' + phone
        result["recipient"] = phone
        result["is_phone"] = True
    
    # Extract message content
    message_patterns = [
        r'(?:saying|say|with message|message:?)\s+["\']?(.+?)["\']?\s*$',
        r'(?:text|message)\s+(?:\S+\s+)?(?:saying|say)\s+["\']?(.+?)["\']?\s*$',
    ]
    for pattern in message_patterns:
        match = re.search(pattern, command, re.I)
        if match:
            result["message"] = match.group(1).strip().strip('"\'')
            break
    
    # Extract recipient name if no phone number
    if not result["is_phone"]:
        # Pattern: "text [name] saying..." or "message [name] saying..."
        name_match = re.search(r'(?:text|message|imessage|send\s+(?:a\s+)?(?:text|message)\s+to)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\s+(?:saying|say|with)', command, re.I)
        if name_match:
            result["recipient"] = name_match.group(1).strip()
    
    return result


def _lookup_contact_phone(name: str) -> str | None:
    """Look up a contact's phone number from Contacts app."""
    script = f'''
    tell application "Contacts"
        try
            set matchingPeople to (every person whose name contains "{name}")
            if (count of matchingPeople) > 0 then
                set thePerson to item 1 of matchingPeople
                set thePhones to phones of thePerson
                if (count of thePhones) > 0 then
                    return value of item 1 of thePhones
                end if
            end if
        end try
        return ""
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10
        )
        phone = result.stdout.strip()
        if phone:
            # Clean up phone number
            phone = re.sub(r'[^\d+]', '', phone)
            if not phone.startswith('+'):
                if len(phone) == 10:
                    phone = '+1' + phone
                elif len(phone) == 11 and phone.startswith('1'):
                    phone = '+' + phone
            return phone
    except Exception as e:
        print(f"Contact lookup error: {e}")
    return None


@app.post("/send-message")
async def send_message(data: dict):
    """Send an iMessage to a contact by name or phone number."""
    command = data.get("command", "")
    if not command.strip():
        return {"error": "No command provided"}
    
    print(f"\n{'='*60}")
    print(f"SEND MESSAGE: {command}")
    print(f"{'='*60}")
    
    parsed = _parse_text_message(command)
    recipient = parsed.get("recipient")
    message = parsed.get("message")
    is_phone = parsed.get("is_phone", False)
    
    print(f"PARSED: recipient={recipient}, message={message}, is_phone={is_phone}")
    
    if not recipient:
        return {"transcript": command, "error": "Could not find recipient. Try: 'text John saying hello' or 'text 555-123-4567 saying hello'"}
    
    if not message:
        return {"transcript": command, "error": "Could not find message. Try: 'text John saying hello'"}
    
    # Look up phone number if name was provided
    phone = recipient if is_phone else None
    contact_name = None if is_phone else recipient
    
    if not is_phone:
        print(f"Looking up contact: {recipient}")
        phone = _lookup_contact_phone(recipient)
        if phone:
            print(f"Found phone: {phone}")
            contact_name = recipient
        else:
            return {
                "transcript": command,
                "error": f"Could not find '{recipient}' in your Contacts. Make sure the name matches a contact, or use their phone number directly."
            }
    
    # Send the message
    escaped_message = message.replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{phone}" of targetService
        send "{escaped_message}" to targetBuddy
    end tell
    '''
    
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0:
            display_recipient = contact_name if contact_name else phone
            return {
                "transcript": command,
                "action": f"Sent message to {display_recipient}: \"{message}\"",
                "osascript_ok": True
            }
        else:
            return {
                "transcript": command,
                "error": f"Failed to send: {result.stderr}",
                "osascript_ok": False
            }
    except Exception as e:
        return {"transcript": command, "error": str(e)}


@app.post("/text-command")
async def text_command(data: dict):
    command = data.get("command", "")
    if not command.strip():
        return {"error": "No command provided"}
    
    print(f"\n{'='*60}")
    print(f"COMMAND: {command}")
    print(f"{'='*60}")
    
    # Handle "end all sessions" / "close everything" commands
    cl = command.lower()
    if any(phrase in cl for phrase in ["end all sessions", "close everything", "close all apps", "quit everything", "quit all apps", "close all windows"]):
        print("ROUTING TO: end-all-sessions")
        script = '''
        tell application "System Events"
            set appList to name of every application process whose visible is true and name is not "Finder"
        end tell
        repeat with appName in appList
            try
                tell application appName to quit
            end try
        end repeat
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        return {
            "transcript": command,
            "action": "Closed all open applications",
            "osascript_ok": result.returncode == 0
        }
    
    # Route git commands FIRST (before text message to avoid "with message" conflict)
    if _wants_git_command(command):
        print("ROUTING TO: git-command")
        return await git_command(data)
    
    # Route Spotify commands to the dedicated handler
    if _wants_spotify_control(command):
        print("ROUTING TO: spotify-control")
        return await spotify_control(data)
    
    # Route text/iMessage commands to the dedicated handler
    if _wants_text_message(command):
        print("ROUTING TO: send-message")
        return await send_message(data)
    
    # Route project creation requests to the dedicated handler
    if _wants_project_creation(command):
        print("ROUTING TO: create-project")
        return await create_project(data)
    
    try:
        script, action = get_applescript(command)
        print(f"ACTION: {action}")
        print(f"SCRIPT:\n{script[:500]}{'...' if len(script) > 500 else ''}")
    except Exception as e:
        print(f"ERROR: {e}")
        return {"transcript": command, "error": f"Claude: {e}"}
    
    result, _ = run_applescript(script, action)
    
    if result["returncode"] != 0:
        print(f"APPLESCRIPT ERROR: {result.get('stderr', '')}")
    else:
        print("SUCCESS")
    print(f"{'='*60}\n")
    
    return {
        "transcript": command,
        "action": action,
        "result": result["stdout"],
        "osascript_ok": result["returncode"] == 0,
        "osascript_error": result.get("stderr", "")
    }


app.mount(
    "/app",
    StaticFiles(directory=str(FRONTEND_DIR), html=True),
    name="frontend",
)


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "8000"))
    _host = os.getenv("UVICORN_HOST", "0.0.0.0").strip()
    _reload = os.getenv("UVICORN_RELOAD", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    _run_kw: dict = {
        "host": _host,
        "port": _port,
        "reload": _reload,
    }
    if _reload:
        _run_kw["reload_dirs"] = [str(BASE_DIR)]
    uvicorn.run("main:app", **_run_kw)
