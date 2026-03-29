import asyncio
import json
import os
import re
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile
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
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")


@app.get("/")
async def root():
    return {"status": "Mac is ready for commands"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/meta.json")
async def meta_json():
    return Response(status_code=204)


def _extract_transcript_text(result: dict) -> str:
    if not result:
        return ""
    text = result.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    transcripts = result.get("transcripts")
    if isinstance(transcripts, list) and transcripts:
        t0 = transcripts[0]
        if isinstance(t0, dict):
            inner = t0.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _sniff_audio_mime(audio_bytes: bytes) -> str | None:
    if len(audio_bytes) >= 12 and audio_bytes[4:8] == b"ftyp":
        return "audio/mp4"
    if len(audio_bytes) >= 4 and audio_bytes[0] == 0x1A and audio_bytes[1] == 0x45:
        return "audio/webm"
    if len(audio_bytes) >= 3 and audio_bytes[0:3] == b"ID3":
        return "audio/mpeg"
    return None


async def transcribe_audio(
    audio_bytes: bytes, filename: str, content_type: str | None = None
) -> str:
    if not ELEVENLABS_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    lower = filename.lower()
    ctype = "audio/webm"
    if lower.endswith(".m4a") or lower.endswith(".mp4") or lower.endswith(".aac"):
        ctype = "audio/mp4"
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in ("audio/mp4", "audio/m4a", "audio/aac", "audio/x-m4a"):
            ctype = "audio/mp4"
        elif ct == "audio/webm":
            ctype = "audio/webm"
    sniffed = _sniff_audio_mime(audio_bytes)
    if sniffed:
        ctype = sniffed

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": ELEVENLABS_KEY},
            files={"file": (filename, audio_bytes, ctype)},
            data={"model_id": "scribe_v1"},
            timeout=60.0,
        )
        try:
            result = response.json()
        except json.JSONDecodeError:
            response.raise_for_status()
            return ""
        if response.status_code >= 400:
            if result.get("status") == "audio_too_short" or result.get("code") == "audio_too_short":
                raise RuntimeError(
                    "Recording too short for ElevenLabs — hold the mic at least 1–2 seconds, then release."
                )
            msg_l = str(result.get("message") or "").lower()
            if (
                result.get("code") == "empty_file"
                or result.get("status") == "empty_file"
                or ("empty" in msg_l and "file" in msg_l)
            ):
                raise RuntimeError(
                    "Recording was empty or unreadable — hold the mic 2+ seconds, speak clearly, then release."
                )
            if result.get("code") == "invalid_audio":
                raise RuntimeError(
                    "Audio format issue — try again; if on iPhone, update the page and record again."
                )
            detail = result.get("detail") or result.get("message") or str(result)
            raise RuntimeError(f"ElevenLabs STT failed ({response.status_code}): {detail}")
        return _extract_transcript_text(result)


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


async def speak_text(text: str) -> bool:
    """
    Sends text to ElevenLabs TTS and plays it on the Mac.
    Used to speak file contents back to the user.
    """
    if not ELEVENLABS_KEY or not text or not text.strip():
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM",
                headers={
                    "xi-api-key": ELEVENLABS_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text[:2000],
                    "model_id": "eleven_monolingual_v1",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    },
                },
                timeout=30.0,
            )

            if response.status_code != 200:
                return False

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = tmp.name

            subprocess.run(
                ["afplay", tmp_path],
                check=True,
            )
            os.unlink(tmp_path)
            return True

    except Exception as e:
        print(f"TTS error: {e}")
        return False


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
    # Fast model for JSON+AppleScript; override with ANTHROPIC_SCRIPT_MODEL if needed.
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


@app.post("/voice-command")
async def voice_command(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    filename = audio.filename or "audio.webm"

    if len(audio_bytes) < 400:
        return {
            "error": "Audio was empty or too small — hold the mic 2+ seconds, speak, then release.",
        }

    try:
        transcript = await transcribe_audio(
            audio_bytes, filename, content_type=audio.content_type
        )
    except Exception as e:
        return {"error": str(e)}

    transcript = clean_transcript(transcript)
    print(f"Heard: {transcript}")

    if not transcript:
        return {"error": "Could not understand audio"}

    _, found_shortcuts = resolve_shortcuts(transcript)
    shortcut_context = build_shortcut_context(found_shortcuts)

    # ── Atomic workflows (full transcript — before parse_command / step splitting) ──
    from gmail_handler import (
        extract_gmail_fields,
        run_gmail_compose_atomic,
        transcript_wants_gmail_compose,
    )

    if transcript_wants_gmail_compose(transcript):
        gf = extract_gmail_fields(transcript)
        if gf.get("to"):
            print("Handled via: atomic — gmail_handler (Playwright)")
            gout = await run_gmail_compose_atomic(transcript)
            return _with_shortcuts(gout, found_shortcuts)

    claude_prompt_check_early = _transcript_wants_claude_prompt(transcript)
    is_claude_prompt_early, extracted_prompt_early = claude_prompt_check_early
    if is_claude_prompt_early:
        if not extracted_prompt_early:
            ok, err = open_url_in_chrome("https://claude.ai")
            print(
                "Handled via: atomic — playwright_claude_fallback "
                "(opened Claude.ai; prompt text not extracted)"
            )
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "action": (
                        "Opened Claude.ai — could not extract prompt text, "
                        "please type manually"
                    ),
                    "method": "playwright_claude_fallback",
                    "osascript_ok": ok,
                    "result": "",
                    "error": None if ok else err,
                },
                found_shortcuts,
            )
        if not PLAYWRIGHT_AVAILABLE:
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": (
                        "Install Playwright: pip install playwright && "
                        "playwright install chrome"
                    ),
                    "osascript_ok": False,
                },
                found_shortcuts,
            )
        if not playwright_enabled():
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": (
                        "Claude prompt automation needs Playwright. "
                        "Set USE_PLAYWRIGHT=1 in .env"
                    ),
                    "osascript_ok": False,
                },
                found_shortcuts,
            )
        from playwright_claude import send_prompt_to_claude

        ok, action_msg = await send_prompt_to_claude(extracted_prompt_early)
        print("Handled via: atomic — playwright_claude (Claude prompt)")
        return _with_shortcuts(
            {
                "transcript": transcript,
                "action": action_msg if ok else "Claude prompt automation failed",
                "method": "playwright_claude",
                "osascript_ok": ok,
                "success": ok,
                "result": "",
                "error": None if ok else action_msg,
            },
            found_shortcuts,
        )

    if _transcript_wants_file_read(transcript):
        from file_reader import handle_file_read

        file_result = await handle_file_read(transcript)

        if file_result.get("success") and file_result.get("voice_content"):
            await speak_text(file_result["voice_content"])
        elif file_result.get("success") and file_result.get("content"):
            await speak_text(file_result["content"][:2000])

        print("Handled via: atomic — file_reader (Downloads)")
        return _with_shortcuts(
            {
                "transcript": transcript,
                "action": file_result["action"],
                "method": file_result["method"],
                "success": file_result.get("success", False),
                "content": file_result.get("content", ""),
                "filename": file_result.get("filename", ""),
                "word_count": file_result.get("word_count", 0),
                "truncated": file_result.get("truncated", False),
                "error": file_result.get("error"),
            },
            found_shortcuts,
        )

    if _transcript_wants_atomic_spotify_play(transcript):
        from command_parser import CommandStep
        from step_executor import execute_spotify_play

        sp_content = _extract_spotify_play_query(transcript)
        if sp_content:
            print("Handled via: atomic — Spotify play (single flow)")
            sp_step = CommandStep(
                action="play",
                target="spotify",
                content=sp_content,
                raw=transcript,
                index=0,
            )
            sp_result = await execute_spotify_play(sp_step)
            sp_result["transcript"] = transcript
            return _with_shortcuts(sp_result, found_shortcuts)

    from command_parser import parse_command
    from step_executor import execute_steps

    print(f"\n{'='*60}")
    print(f"TRANSCRIPT: {transcript}")
    print(f"SHORTCUTS: {found_shortcuts}")
    print("PARSING INTO STEPS...")
    steps = parse_command(transcript)
    print(f"FOUND {len(steps)} STEP(S)")
    print(f"{'='*60}\n")

    if len(steps) > 1:
        result = await execute_steps(
            steps,
            found_shortcuts,
            shortcut_context,
            get_applescript,
            run_applescript,
        )
        print("Handled via: multi-step (command_parser + step_executor)")
        err_msg = (
            "; ".join(result["errors"])
            if result.get("errors")
            else None
        )
        return _with_shortcuts(
            {
                "transcript": transcript,
                "action": result.get("action", ""),
                "method": result.get("method", "multi_step"),
                "errors": result.get("errors"),
                "steps_completed": result.get("steps_completed", 0),
                "steps_total": result.get("steps_total", len(steps)),
                "steps_parsed": len(steps),
                "osascript_ok": result.get("osascript_ok", True),
                "result": "",
                "error": err_msg,
                "success": result.get("osascript_ok", False),
            },
            found_shortcuts,
        )

    if transcript_wants_claude_upload_flow(transcript):
        if not PLAYWRIGHT_AVAILABLE:
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": "Install Playwright: pip install playwright && playwright install chrome",
                    "osascript_ok": False,
                },
                found_shortcuts,
            )
        if not playwright_enabled():
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": "Claude upload automation needs Playwright. Set USE_PLAYWRIGHT=1 in .env",
                    "osascript_ok": False,
                },
                found_shortcuts,
            )
        pw = await run_claude_upload_playwright(transcript)
        if pw.get("method") == "playwright_claude":
            print("Handled via: Playwright (Claude upload + prompt)")
            return _with_shortcuts(pw, found_shortcuts)
        if pw.get("error"):
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": pw["error"],
                    "hint": (
                        "Ensure ~/Downloads has the PDF, you’re logged into claude.ai in the opened window, "
                        "or set CLAUDE_PLAYWRIGHT_USER_DATA_DIR to persist login. "
                        "Official chat URL is https://claude.ai (not claude.com)."
                    ),
                    "osascript_ok": False,
                },
                found_shortcuts,
            )

    if transcript_wants_youtube_dom_control(transcript):
        if not PLAYWRIGHT_AVAILABLE:
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": "Install Playwright: pip install playwright && playwright install chromium",
                    "osascript_ok": False,
                },
                found_shortcuts,
            )
        if not playwright_enabled():
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": "Browser clicks need Playwright enabled. Set USE_PLAYWRIGHT=1 in .env",
                    "osascript_ok": False,
                },
                found_shortcuts,
            )

    if playwright_enabled() and transcript_wants_youtube_dom_control(transcript):
        pw = await run_youtube_playwright(transcript)
        if pw.get("method") == "playwright":
            print("Handled via: Playwright (DOM)")
            return _with_shortcuts(pw, found_shortcuts)
        if pw.get("error"):
            # Do not fall through to Claude (~30–60s) for click intents — wrong tool.
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "error": pw["error"],
                    "hint": "Run: playwright install chromium (or fix the error above). For URL-only, say: open youtube.com in Chrome.",
                    "osascript_ok": False,
                },
                found_shortcuts,
            )

    compound = parse_compound_command(transcript)
    is_compound = bool(compound.get("is_compound"))

    if not wants_vscode_html_create(transcript) and not wants_vscode_python_create(
        transcript
    ):
        if is_compound:
            app_name = compound["app_name"]
            sec_action = compound["secondary_action"]
            sec_type = compound["secondary_type"]
            fb = lookup_fallback_url(app_name, found_shortcuts)
            method, app_action, script_or_path = resolve_app_or_web(app_name, fb)

            if method != "not_found":
                spotify_fb1: str | None = None
                spotify_fb2: str | None = None
                spotify_human: str | None = None
                if "spotify" in app_name.lower() and method == "native_app":
                    bundle = await build_spotify_in_app_bundle(sec_action)
                    if bundle:
                        in_app, spotify_fb1, spotify_fb2, spotify_human = bundle
                    else:
                        in_app = None
                else:
                    in_app = await build_in_app_action_script(
                        app_name,
                        sec_action,
                        sec_type,
                        spotify_use_native=(method == "native_app"),
                    )

                if (
                    in_app
                    and method == "chrome_tab"
                    and (
                        "youtube" in app_name.lower()
                        or "spotify" in app_name.lower()
                    )
                ):
                    print(
                        "Handled via: native_app_compound (web search tab; "
                        "skipping homepage)"
                    )
                    ex = _osascript_pipe(in_app)
                    return _with_shortcuts(
                        {
                            "transcript": transcript,
                            "action": f"{app_action} → {sec_action}",
                            "method": "native_app_compound",
                            "osascript_ok": ex["returncode"] == 0,
                            "result": (ex["stdout"] or "").strip(),
                            "error": ex["stderr"] if ex["returncode"] != 0 else None,
                        },
                        found_shortcuts,
                    )

                if in_app is None:
                    try:
                        print("Compound: falling back to Claude AppleScript")
                        full_script, claude_action = get_applescript(
                            transcript, shortcut_context
                        )
                        exec_result, action_out = run_applescript(
                            full_script, claude_action
                        )
                        return _with_shortcuts(
                            {
                                "transcript": transcript,
                                "action": action_out,
                                "method": "applescript_compound",
                                "osascript_ok": exec_result["returncode"] == 0,
                                "result": exec_result["stdout"],
                                "error": exec_result["stderr"]
                                if exec_result["returncode"] != 0
                                else None,
                            },
                            found_shortcuts,
                        )
                    except Exception as e:
                        print(f"Compound AppleScript fallback failed: {e}")

                elif in_app is not None:
                    results: list[str] = []
                    if method == "native_app":
                        print("Handled via: native_app_compound (native + in-app)")
                        ok, msg = open_native_app(script_or_path)
                        results.append(msg)
                        if not ok:
                            return _with_shortcuts(
                                {
                                    "transcript": transcript,
                                    "action": msg,
                                    "method": "native_app_compound",
                                    "osascript_ok": False,
                                    "error": msg,
                                    "result": "",
                                },
                                found_shortcuts,
                            )
                    elif method == "chrome_tab":
                        print("Handled via: native_app_compound (Chrome base + in-app)")
                        ex0 = _osascript_pipe(script_or_path)
                        results.append(
                            app_action
                            if ex0["returncode"] == 0
                            else (ex0["stderr"] or "Chrome step failed")
                        )
                        if ex0["returncode"] != 0:
                            return _with_shortcuts(
                                {
                                    "transcript": transcript,
                                    "action": results[0],
                                    "method": "native_app_compound",
                                    "osascript_ok": False,
                                    "error": ex0["stderr"],
                                    "result": "",
                                },
                                found_shortcuts,
                            )

                    await asyncio.sleep(2.5)
                    ex2 = _osascript_pipe(in_app)
                    if ex2["returncode"] != 0 and spotify_fb1:
                        print(
                            "Spotify play track failed; trying keyboard fallback 1"
                        )
                        ex2 = _osascript_pipe(spotify_fb1)
                    if ex2["returncode"] != 0 and spotify_fb2:
                        print(
                            "Spotify keyboard fallback 1 failed; trying fallback 2"
                        )
                        ex2 = _osascript_pipe(spotify_fb2)
                    if ex2["returncode"] == 0:
                        if spotify_human:
                            results.append(spotify_human)
                        else:
                            results.append(f"Then: {sec_action}")
                        combined_action = " → ".join(results)
                    else:
                        combined_action = (
                            results[0] + f" (could not automate: {sec_action})"
                        )
                    return _with_shortcuts(
                        {
                            "transcript": transcript,
                            "action": combined_action,
                            "method": "native_app_compound",
                            "osascript_ok": ex2["returncode"] == 0,
                            "result": (ex2["stdout"] or "").strip(),
                            "error": ex2["stderr"] if ex2["returncode"] != 0 else None,
                        },
                        found_shortcuts,
                    )

    wants_app, spoken_app_name = _transcript_wants_app_open(transcript)
    if (
        wants_app
        and spoken_app_name
        and not is_compound
        and not wants_vscode_html_create(transcript)
        and not wants_vscode_python_create(transcript)
    ):
        fb = lookup_fallback_url(spoken_app_name, found_shortcuts)
        method, app_action, script_or_path = resolve_app_or_web(spoken_app_name, fb)
        if method == "native_app":
            print("Handled via: native app launcher")
            ok, msg = open_native_app(script_or_path)
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "action": msg,
                    "method": "native_app",
                    "osascript_ok": ok,
                    "result": script_or_path if ok else "",
                    "error": None if ok else msg,
                },
                found_shortcuts,
            )
        if method == "chrome_tab":
            print("Handled via: native app fallback → Chrome tab")
            ex = _osascript_pipe(script_or_path)
            return _with_shortcuts(
                {
                    "transcript": transcript,
                    "action": app_action,
                    "method": "chrome_tab",
                    "osascript_ok": ex["returncode"] == 0,
                    "result": (ex["stdout"] or "").strip(),
                    "error": ex["stderr"] if ex["returncode"] != 0 else None,
                },
                found_shortcuts,
            )
        # not_found: no .app and no shortcut URL — continue to Chrome CLI / AppleScript

    quick = await attempt_chrome_open_cli(transcript, found_shortcuts)
    if quick:
        print("Handled via: Chrome new-tab (CLI)")
        return _with_shortcuts(quick, found_shortcuts)

    if wants_vscode_html_create(transcript):
        try:
            print("Handled via: HTML on Desktop + VS Code + browser")
            out = create_vscode_html_file(transcript, shortcut_context)
            out["transcript"] = transcript
            return _with_shortcuts(out, found_shortcuts)
        except Exception as e:
            print(f"VS Code / HTML file path failed ({e}), falling back to AppleScript")

    if wants_vscode_python_create(transcript):
        try:
            print("Handled via: Python file on Desktop + VS Code")
            out = create_vscode_python_file(transcript, shortcut_context)
            out["transcript"] = transcript
            return _with_shortcuts(out, found_shortcuts)
        except Exception as e:
            print(f"VS Code / Python file path failed ({e}), falling back to AppleScript")

    if wants_notes_compose_with_text(transcript):
        body = resolve_notes_body(transcript)
        if body:
            print("Handled via: Notes (pbcopy + paste)")
            out = run_notes_new_note_and_paste(body, transcript)
            out["transcript"] = transcript
            return _with_shortcuts(out, found_shortcuts)

    try:
        script, action = get_applescript(transcript, shortcut_context)
    except Exception as e:
        return _with_shortcuts(
            {"transcript": transcript, "error": f"Claude: {e}"},
            found_shortcuts,
        )

    print(f"Action: {action}")
    print(f"Script: {script}")

    exec_result, action_out = run_applescript(script, action)
    action = action_out
    if exec_result["returncode"] != 0:
        print(f"osascript stderr: {exec_result['stderr']}")
        fallback = await attempt_chrome_open_cli(transcript, found_shortcuts)
        if fallback:
            print("AppleScript failed; succeeded via Chrome new-tab (CLI)")
            return _with_shortcuts(fallback, found_shortcuts)

    payload: dict = {
        "transcript": transcript,
        "action": action,
        "result": exec_result["stdout"],
        "osascript_ok": exec_result["returncode"] == 0,
        "method": "applescript",
    }
    if exec_result["returncode"] != 0:
        err = exec_result["stderr"] or "AppleScript failed (no details)"
        payload["osascript_error"] = err
        hint = (
            " On Mac: System Settings → Privacy & Security → Accessibility — add Terminal "
            "(or the app running Python). Automation: allow controlling Google Chrome."
        )
        if "Not authorized" in err or "-1743" in err or "not allowed" in err.lower():
            payload["hint"] = hint.strip()
    return _with_shortcuts(payload, found_shortcuts)


app.mount(
    "/app",
    StaticFiles(directory=str(FRONTEND_DIR), html=True),
    name="frontend",
)


if __name__ == "__main__":
    # Auto-reload on .py changes (no manual restart). Set UVICORN_RELOAD=0 to disable.
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
