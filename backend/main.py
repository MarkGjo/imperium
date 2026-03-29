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
    data = _parse_json_from_claude(raw)
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


@app.post("/text-command")
async def text_command(data: dict):
    command = data.get("command", "")
    if not command.strip():
        return {"error": "No command provided"}
    try:
        script, action = get_applescript(command)
    except Exception as e:
        return {"transcript": command, "error": f"Claude: {e}"}
    result, _ = run_applescript(script, action)
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
