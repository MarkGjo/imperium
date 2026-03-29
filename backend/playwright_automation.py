"""
Browser DOM automation (Playwright). AppleScript cannot click inside web pages.

Setup (once):
  pip install playwright
  playwright install chrome

Optional: set USE_PLAYWRIGHT=1 in .env (defaults to on if import works).

To drive **your already-open, signed-in Chrome** (new tab, same session) instead of a
separate Playwright window, start Chrome once with remote debugging, e.g.:

  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222

Then set in .env:  PLAYWRIGHT_CDP_URL=http://127.0.0.1:9222
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True  # exported for main.py routing
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None  # type: ignore


def playwright_enabled() -> bool:
    if not PLAYWRIGHT_AVAILABLE:
        return False
    v = os.getenv("USE_PLAYWRIGHT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def google_chrome_executable_path() -> str | None:
    """
    Path to the real Google Chrome.app — avoids Playwright’s bundled Chromium
    (“Chrome for Testing” / headless shell). Override with GOOGLE_CHROME_PATH.
    """
    for key in ("GOOGLE_CHROME_PATH", "PLAYWRIGHT_GOOGLE_CHROME_PATH"):
        env = (os.getenv(key) or "").strip()
        if env and Path(env).is_file():
            return env
    if sys.platform == "darwin":
        p = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if p.is_file():
            return str(p)
    return None


def _chromium_launch_kwargs(extra_args: list[str] | None = None) -> dict:
    """Always prefer real Chrome on Mac; never launch raw bundled Chromium first."""
    args = list(extra_args or [])
    exe = google_chrome_executable_path()
    if exe:
        return {"executable_path": exe, "args": args}
    return {"channel": "chrome", "args": args}


_YOUTUBE_HINT = re.compile(
    r"(youtube\.com|youtu\.be|youtube\b|you\s*tube)",
    re.I,
)
_DOM_INTENT = re.compile(
    r"\b(play|watch|click|tap|video|videos|channel|subscribe|most recent|latest|first|tab|open a new)\b",
    re.I,
)


# Voice/STT often says "clod", "cloud", or "claud" instead of "Claude".
_CLAUDE_HINT = re.compile(
    r"(?:"
    r"claude\.ai|claude\.com|claud\.com|clod\.com|"
    r"\bclaud\b|\bclaude\b|\bclod\b"
    r")",
    re.I,
)
# "Cloud" alone often means Claude in "upload … to Cloud / prompt in Cloud" voice commands.
_CLOUD_MEANS_CLAUDE = re.compile(
    r"\bcloud\b.{0,200}\b(upload|pdf|prompt|assignment|plus\s+button|chat|solve)\b|"
    r"\b(upload|pdf|finder|downloads?).{0,120}\bcloud\b",
    re.I | re.DOTALL,
)
_UPLOAD_INTENT = re.compile(
    r"\b(upload|pdf|\.pdf|attachment|attach|plus|file|finder|downloads?|assignment|solve)\b",
    re.I,
)


def transcript_means_claude_web(transcript: str) -> bool:
    if _CLAUDE_HINT.search(transcript):
        return True
    if _CLOUD_MEANS_CLAUDE.search(transcript):
        return True
    return False


def transcript_wants_claude_upload_flow(transcript: str) -> bool:
    """Claude web + file upload / PDF from Downloads — needs Playwright DOM control."""
    if not transcript_means_claude_web(transcript):
        return False
    if not _UPLOAD_INTENT.search(transcript):
        return False
    # Need something that implies a concrete file or upload action
    if not re.search(
        r"(\.pdf|pdf\b|upload|attach|select|downloads?|assignment)",
        transcript,
        re.I,
    ):
        return False
    return True


def extract_upload_filename(transcript: str) -> str | None:
    """Best-effort PDF filename — avoid greedy capture of the whole sentence."""
    # After select/choose/upload … → short filename ending in .pdf
    m = re.search(
        r"(?:select|choose|pick|upload)\s+(?:the\s+)?(?:file\s+)?(?:called\s+)?"
        r"([A-Za-z0-9][A-Za-z0-9\s\-]{0,120}?\.(?:pdf|PDF))\b",
        transcript,
        re.I,
    )
    if m:
        name = m.group(1).strip()
        if len(name) < 200 and "\n" not in name:
            return name
    # Any plausible short “… .pdf” tail (last match wins — often the real filename)
    hits = re.findall(
        r"\b([A-Za-z0-9][A-Za-z0-9\s\-]{0,100}\.(?:pdf|PDF))\b",
        transcript,
        re.I,
    )
    for name in reversed(hits):
        n = name.strip()
        if len(n) < 150 and not re.search(
            r"\b(finder|downloads?|chrome|button|prompt)\b", n, re.I
        ):
            return n
    return None


def _pdf_compare_key(filename: str) -> str:
    """Map 'Assignment 1.PDF' and 'assignment1.pdf' to the same key."""
    stem = Path(filename).stem
    ext = Path(filename).suffix.lower()
    compact = re.sub(r"[\s_\-]+", "", stem.lower())
    return compact + ext


def resolve_downloads_file(name: str) -> Path | None:
    """Resolve ~/Downloads/name: exact, case-insensitive, then fuzzy PDF match."""
    folder = Path.home() / "Downloads"
    if not folder.is_dir():
        return None
    direct = folder / name
    if direct.is_file():
        return direct
    name_lower = name.lower()
    for f in folder.iterdir():
        if f.is_file() and f.name.lower() == name_lower:
            return f

    want_key = _pdf_compare_key(name)
    pdfs: list[Path] = []
    for f in folder.iterdir():
        if not f.is_file() or f.suffix.lower() != ".pdf":
            continue
        if _pdf_compare_key(f.name) == want_key:
            pdfs.append(f)
    if len(pdfs) == 1:
        return pdfs[0]
    if len(pdfs) > 1:
        return sorted(pdfs, key=lambda p: len(p.name))[0]
    return None


def extract_claude_assignment_prompt(transcript: str) -> str:
    """User instruction for the chat after upload."""
    # Unquoted: "prompt type, solve the assignment …" / "In the prompt type, …"
    m_plain = re.search(
        r"(?:in\s+)?(?:the\s+)?prompt\s+type\s*,\s*(.+?)(?:\.\s*(?:Then|$)|\s+Then\s+click|\s+Then\s+submit|$)",
        transcript,
        re.I | re.DOTALL,
    )
    if m_plain:
        s = m_plain.group(1).strip().rstrip(".")
        if len(s) > 2:
            return s[0].upper() + s[1:] if len(s) > 1 else s
    # Explicit quoted string: "… prompt type, \"Hey…\"" or "type, \"Hey…\""
    qm = re.search(
        r"(?:\bprompt\s+type|type)\s*,\s*[\"'](.+?)[\"']",
        transcript,
        re.I | re.DOTALL,
    )
    if qm:
        s = qm.group(1).strip()
        if len(s) > 2:
            return s
    qm2 = re.search(r"[\"“]([^\"”]{3,400})[\"”]", transcript)
    if qm2:
        s = qm2.group(1).strip()
        if "pdf" not in s.lower() or len(s) < 80:
            return s

    m = re.search(
        r"(?:type\s+in\s+and\s+)?have\s+Claud(?:e)?\s+(.+?)(?:\s+and\s+then\s+click|\s+then\s+click\s+send|$)",
        transcript,
        re.I | re.DOTALL,
    )
    if m:
        s = m.group(1).strip().rstrip(".")
        if len(s) > 8:
            if len(s) < 120 and not s.lower().startswith("please"):
                return "Please read the attached PDF carefully and " + s[0].lower() + s[1:]
            return s
    m = re.search(
        r"(?:have\s+Claude|claude\s+should|so\s+that\s+Claude)\s+(.+?)(?:\s+and\s+then\s+click|\s+then\s+click|$)",
        transcript,
        re.I | re.DOTALL,
    )
    if m:
        s = m.group(1).strip().rstrip(".")
        s = re.sub(r"^\s*(?:type\s+in\s+and\s+)?", "", s, flags=re.I)
        if len(s) > 8:
            return s
    if re.search(r"answer\s+the\s+assignments?", transcript, re.I):
        return (
            "Please read the attached PDF carefully and answer all assignment questions. "
            "Show reasoning where appropriate."
        )
    if re.search(
        r"have\s+(?:this|it|that)\s+solve\s+(?:that\s+)?(?:the\s+)?assignment",
        transcript,
        re.I,
    ):
        return (
            "Please read the attached PDF and solve the assignment completely. "
            "Show your work where appropriate."
        )
    return (
        "Please read the attached PDF and answer the assignment questions based on the document."
    )


async def _claude_attach_pdf(page, path: Path) -> bool:
    """Attach PDF via hidden inputs and/or attach buttons + file chooser (Claude UI changes often)."""
    spath = str(path)
    try:
        await page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)

    # 1) Hidden <input type=file> (often present before clicking +)
    for _ in range(2):
        inputs = page.locator('input[type="file"]')
        n = await inputs.count()
        for i in range(n):
            try:
                await inputs.nth(i).set_input_files(spath, timeout=20000)
                return True
            except Exception:
                continue
        await page.wait_for_timeout(1200)

    # 2) Role-named buttons (English UI)
    for pat in (
        re.compile(r"add\s+files?", re.I),
        re.compile(r"add\s+photos?\s+and\s+files?", re.I),
        re.compile(r"attach", re.I),
        re.compile(r"upload", re.I),
        re.compile(r"from\s+device", re.I),
    ):
        try:
            btn = page.get_by_role("button", name=pat).first
            if await btn.count() == 0:
                continue
            async with page.expect_file_chooser(timeout=18000) as fc_info:
                await btn.click(timeout=8000)
            chooser = await fc_info.value
            await chooser.set_files(spath)
            return True
        except Exception:
            continue

    # 3) aria-label / title on icon buttons (case variants)
    for needle in ("add", "Add", "attach", "Attach", "upload", "Upload", "file", "File"):
        try:
            loc = page.locator(
                f'[aria-label*="{needle}"], [title*="{needle}"], button[aria-label*="{needle}"]'
            ).first
            if await loc.count() == 0:
                continue
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await loc.click(timeout=6000, force=True)
            chooser = await fc_info.value
            await chooser.set_files(spath)
            return True
        except Exception:
            continue

    # 4) Click near composer: first visible button with svg (plus / paperclip)
    try:
        cand = page.locator(
            'div[class*="composer" i] button, [data-testid*="composer" i] button, '
            'footer button, [class*="input" i] ~ div button'
        ).first
        if await cand.count() > 0:
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await cand.click(timeout=5000)
            chooser = await fc_info.value
            await chooser.set_files(spath)
            return True
    except Exception:
        pass

    return False


def _playwright_cdp_url() -> str | None:
    u = (os.getenv("PLAYWRIGHT_CDP_URL") or "").strip()
    return u or None


async def _claude_start_browser(playwright, user_data: str, lk: dict):
    """
    Returns (browser, context, page, via_cdp).
    If PLAYWRIGHT_CDP_URL is set, attaches to your running Chrome (same cookies/tabs profile).
    """
    cdp = _playwright_cdp_url()
    if cdp:
        browser = await playwright.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        return browser, context, page, True
    if user_data:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=user_data,
            headless=False,
            viewport={"width": 1280, "height": 860},
            **lk,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        return None, context, page, False
    browser = await playwright.chromium.launch(headless=False, **lk)
    context = await browser.new_context(viewport={"width": 1280, "height": 860})
    page = await context.new_page()
    return browser, context, page, False


async def run_claude_upload_playwright(transcript: str) -> dict:
    """
    Open claude.ai, upload a file from Downloads, type prompt, send.
    Requires an existing Claude login in the launched profile (or sign in once in the window).
    Set CLAUDE_PLAYWRIGHT_USER_DATA_DIR to a folder for persistent Chrome profile cookies.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "error": "Playwright not installed. Run: pip install playwright && playwright install chrome",
        }

    fname = extract_upload_filename(transcript)
    if not fname:
        return {
            "error": "Could not detect a PDF filename. Say e.g. “select assignment one.pdf from Downloads”.",
        }

    path = resolve_downloads_file(fname)
    prompt = extract_claude_assignment_prompt(transcript)
    start_url = os.getenv("CLAUDE_PLAYWRIGHT_START_URL", "https://claude.ai/new").strip()
    keep_open = float(os.getenv("PLAYWRIGHT_CLAUDE_KEEP_OPEN_SEC", "600"))

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-default-browser-check",
    ]
    lk = _chromium_launch_kwargs(launch_args)

    playwright = await async_playwright().start()
    browser = None
    context = None
    user_data = os.getenv("CLAUDE_PLAYWRIGHT_USER_DATA_DIR", "").strip()
    via_cdp = False

    try:
        browser, context, page, via_cdp = await _claude_start_browser(
            playwright, user_data, lk
        )

        await page.goto(start_url, wait_until="domcontentloaded", timeout=90000)

        if not path:
            await _schedule_close_claude(
                browser, context, playwright, keep_open, via_cdp=via_cdp
            )
            return {
                "transcript": transcript,
                "error": (
                    f"Opened Claude in Chrome but could not find “{fname}” in ~/Downloads "
                    "(add or rename the PDF; “Assignment 1.PDF” matches assignment1.pdf)."
                ),
                "action": (
                    f"Launched Google Chrome → {start_url}. "
                    f"No file matched “{fname}” in Downloads — fix the filename and run again."
                ),
                "osascript_ok": False,
                "method": "playwright_claude",
                "detail": str(Path.home() / "Downloads"),
            }
        await page.wait_for_timeout(2500)

        url = page.url.lower()
        if "login" in url or "sign-in" in url or "signin" in url:
            await _schedule_close_claude(
                browser, context, playwright, keep_open, via_cdp=via_cdp
            )
            return {
                "error": (
                    "Claude opened to a sign-in page. Sign in once in that window, "
                    "or set CLAUDE_PLAYWRIGHT_USER_DATA_DIR in .env to a folder so your login persists."
                ),
            }

        # --- Upload PDF (multi-strategy; Claude UI changes) ---
        uploaded = await _claude_attach_pdf(page, path)

        if not uploaded:
            await _schedule_close_claude(
                browser, context, playwright, keep_open, via_cdp=via_cdp
            )
            return {
                "error": (
                    "Could not attach the PDF (Claude’s page layout may have changed). "
                    "Try updating selectors or sign in if the composer is hidden."
                ),
            }

        await page.wait_for_timeout(2000)

        # --- Type prompt ---
        filled = False
        for sel in (
            "div[contenteditable='true'][data-placeholder]",
            "div[contenteditable='true']",
            "textarea",
            "[role='textbox']",
        ):
            try:
                loc = page.locator(sel).last
                await loc.wait_for(state="visible", timeout=8000)
                await loc.click(timeout=5000)
                try:
                    await loc.fill(prompt)
                except Exception:
                    await page.keyboard.press("Meta+a")
                    await page.keyboard.type(prompt, delay=12)
                filled = True
                break
            except Exception:
                continue

        if not filled:
            try:
                ph = page.get_by_placeholder(re.compile(r"message|reply|ask", re.I)).first
                await ph.wait_for(state="visible", timeout=6000)
                await ph.click()
                await ph.fill(prompt)
                filled = True
            except Exception:
                pass

        if not filled:
            try:
                await page.keyboard.press("Meta+a")
                await page.keyboard.insert_text(prompt)
                filled = True
            except Exception:
                pass

        if not filled:
            await _schedule_close_claude(
                browser, context, playwright, keep_open, via_cdp=via_cdp
            )
            return {
                "error": "Could not find the message box to type your prompt (UI may have changed).",
            }

        await page.wait_for_timeout(400)

        # --- Send ---
        sent = False
        for label in (
            r"Send message",
            r"^Send$",
            r"Submit",
            r"Start",
        ):
            try:
                b = page.get_by_role("button", name=re.compile(label, re.I)).first
                if await b.count() == 0:
                    continue
                await b.click(timeout=8000)
                sent = True
                break
            except Exception:
                continue

        if not sent:
            try:
                await page.keyboard.press("Meta+Enter")
                sent = True
            except Exception:
                pass

        await _schedule_close_claude(
            browser, context, playwright, keep_open, via_cdp=via_cdp
        )

        action = (
            f"Opened Claude, attached {path.name} from Downloads, sent your prompt, "
            f"and left the browser open ~{int(keep_open)}s (PLAYWRIGHT_CLAUDE_KEEP_OPEN_SEC)."
        )
        return {
            "transcript": transcript,
            "action": action,
            "result": "",
            "osascript_ok": True,
            "method": "playwright_claude",
            "typing_ui": True,
            "detail": (
                "If Claude asked for login, use CLAUDE_PLAYWRIGHT_USER_DATA_DIR for a saved session. "
                f"Prompt used: {prompt[:200]}{'…' if len(prompt) > 200 else ''}"
            ),
        }

    except Exception as e:
        try:
            if via_cdp and browser:
                await browser.close()
            else:
                if context:
                    await context.close()
                if browser:
                    await browser.close()
        except Exception:
            pass
        try:
            await playwright.stop()
        except Exception:
            pass
        return {"error": f"Claude Playwright error: {e}"}


async def _schedule_close_claude(
    browser,
    context,
    playwright,
    delay_sec: float,
    *,
    via_cdp: bool = False,
) -> None:
    async def _run() -> None:
        await asyncio.sleep(max(5.0, delay_sec))
        try:
            if via_cdp:
                if browser:
                    await browser.close()
            else:
                if context:
                    await context.close()
                if browser:
                    await browser.close()
        except Exception:
            pass
        try:
            await playwright.stop()
        except Exception:
            pass

    asyncio.create_task(_run())


def transcript_wants_youtube_dom_control(transcript: str) -> bool:
    """Heuristic: YouTube + intent to do more than passively open a URL (click/play/etc.)."""
    if transcript_wants_claude_upload_flow(transcript):
        return False
    t = transcript.lower()
    if not _YOUTUBE_HINT.search(t):
        return False
    return bool(_DOM_INTENT.search(t))


def extract_youtube_search_query(transcript: str) -> str | None:
    """Best-effort search terms; None = open home / first shelf video."""
    m = re.search(
        r"(?:search|find|for|about)\s+(?:youtube\s+)?(?:for\s+)?(.+?)(?:\.|$)",
        transcript,
        re.I | re.DOTALL,
    )
    if m:
        q = m.group(1).strip()
        q = re.sub(r"\s+(and|then|on youtube).*$", "", q, flags=re.I).strip()
        if len(q) > 2:
            return q
    return None


async def _close_later(browser, playwright, delay_sec: float) -> None:
    await asyncio.sleep(delay_sec)
    try:
        await browser.close()
    except Exception:
        pass
    try:
        await playwright.stop()
    except Exception:
        pass


async def run_youtube_playwright(transcript: str) -> dict:
    """
    Launch visible Chrome via Playwright, open YouTube, click the first video title we find.
    Browser auto-closes after watch_seconds (keeps API responsive; extend if you want longer).
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": "Playwright not installed. Run: pip install playwright && playwright install chrome"}

    query = extract_youtube_search_query(transcript)
    watch_seconds = float(os.getenv("PLAYWRIGHT_WATCH_SECONDS", "120"))

    playwright = await async_playwright().start()
    browser = None
    launch_args = ["--disable-blink-features=AutomationControlled"]
    lk = _chromium_launch_kwargs(launch_args)
    try:
        cdp = _playwright_cdp_url()
        if cdp:
            browser = await playwright.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
        else:
            browser = await playwright.chromium.launch(headless=False, **lk)
            page = await browser.new_page()
    except Exception as e:
        try:
            await playwright.stop()
        except Exception:
            pass
        return {
            "error": f"Could not launch browser for automation: {e}. Run: playwright install chromium",
        }
    try:
        if query:
            url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            action_note = f"YouTube search: “{query}” — playing first result"
        else:
            url = "https://www.youtube.com/"
            action_note = "YouTube home — playing first visible video"

        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(800)

        clicked = False
        for sel in (
            "ytd-video-renderer a#video-title",
            "ytd-rich-item-renderer a#video-title",
            "a#video-title",
        ):
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=8000)
                await loc.click(timeout=6000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            await browser.close()
            await playwright.stop()
            return {
                "error": "Could not find a video link on the page (selectors/UI may have changed).",
            }

        asyncio.create_task(_close_later(browser, playwright, watch_seconds))

        return {
            "transcript": transcript,
            "action": action_note,
            "result": "",
            "osascript_ok": True,
            "method": "playwright",
            "detail": f"DOM automation started; browser will close after ~{int(watch_seconds)}s (set PLAYWRIGHT_WATCH_SECONDS).",
        }
    except Exception as e:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await playwright.stop()
        except Exception:
            pass
        return {"error": f"Playwright error: {e}"}
