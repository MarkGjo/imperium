"""
Claude.ai prompt delivery via Playwright + Chrome CDP (existing logged-in session).

Requires Chrome started with --remote-debugging-port=9222 and PLAYWRIGHT_CDP_URL set.
Does not launch a separate browser — only connects over CDP.
"""

from __future__ import annotations

import asyncio
import os
import re

try:
    from playwright.async_api import async_playwright

    _PLAYWRIGHT_OK = True
except ImportError:
    async_playwright = None  # type: ignore
    _PLAYWRIGHT_OK = False


def _cdp_url() -> str:
    return (os.getenv("PLAYWRIGHT_CDP_URL") or "http://localhost:9222").strip()


def _clipboard_paste_into_chrome(text: str) -> tuple[bool, str]:
    """
    No CDP: activate Chrome, paste from clipboard (Cmd+V).
    Use after a Claude tab is already open (e.g. open step just ran).
    """
    import subprocess

    try:
        subprocess.run(
            ["pbcopy"],
            input=(text or "").encode("utf-8"),
            check=False,
        )
    except OSError as e:
        return False, f"pbcopy failed: {e!s}"

    script = """tell application "Google Chrome"
    activate
    delay 1.2
end tell
tell application "System Events"
    keystroke "v" using command down
end tell"""
    try:
        r = subprocess.run(
            ["osascript", "-"],
            input=script.strip() + "\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip() or "osascript paste failed"
        preview = text if len(text) <= 100 else text[:97] + "…"
        return (
            True,
            f"Pasted into front Chrome window (no CDP): “{preview}”. "
            "If text went to the wrong field, click the Claude message box and try again, "
            "or start Chrome with --remote-debugging-port=9222 for Playwright.",
        )
    except OSError as e:
        return False, str(e)


async def _find_composer_handle(claude_page):
    input_field = None
    for sel in (
        "div[contenteditable='true'][data-placeholder]",
        "div[contenteditable='true']",
        "textarea",
        "[role='textbox']",
        "div.ProseMirror",
    ):
        try:
            loc = claude_page.locator(sel).last
            await loc.wait_for(state="visible", timeout=8000)
            input_field = await loc.element_handle()
            if input_field:
                break
        except Exception:
            continue

    if not input_field:
        try:
            ph = claude_page.get_by_placeholder(re.compile(r"message|reply|ask", re.I)).first
            await ph.wait_for(state="visible", timeout=6000)
            input_field = await ph.element_handle()
        except Exception:
            pass

    return input_field


async def type_in_claude(text: str) -> tuple[bool, str]:
    """Types text into Claude composer without submitting (separate step from Enter)."""
    text = (text or "").strip()
    if not text:
        return False, "Empty text"

    if not _PLAYWRIGHT_OK or async_playwright is None:
        return False, "Playwright is not installed."

    playwright = None
    try:
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(_cdp_url())
        except Exception:
            ok, msg = _clipboard_paste_into_chrome(text)
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass
            return (ok, msg) if ok else (False, msg)

        try:
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            claude_page = None
            for page in context.pages:
                try:
                    if "claude.ai" in (page.url or "").lower():
                        claude_page = page
                        await page.bring_to_front()
                        break
                except Exception:
                    continue
            if not claude_page:
                claude_page = await context.new_page()
                await claude_page.goto(
                    (os.getenv("CLAUDE_PLAYWRIGHT_START_URL") or "https://claude.ai/new").strip(),
                    wait_until="domcontentloaded",
                    timeout=90000,
                )
                await asyncio.sleep(2)
            await claude_page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(0.3)

            input_field = await _find_composer_handle(claude_page)
            if not input_field:
                return False, "Could not find Claude input field"

            await input_field.click()
            await claude_page.keyboard.press("Meta+a")
            await asyncio.sleep(0.1)
            await input_field.type(text, delay=30)
            await asyncio.sleep(0.3)
            return True, f"Typed into Claude: '{text}'"
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    except Exception as e:
        return False, f"Error typing in Claude: {e!s}"
    finally:
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass


async def press_enter_in_claude() -> bool:
    """Presses Enter in the focused Claude tab to submit."""
    if not _PLAYWRIGHT_OK or async_playwright is None:
        return False

    playwright = None
    try:
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(_cdp_url())
        except Exception:
            return False

        try:
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            claude_page = None
            for page in context.pages:
                try:
                    if "claude.ai" in (page.url or "").lower():
                        claude_page = page
                        await page.bring_to_front()
                        break
                except Exception:
                    continue
            if not claude_page:
                return False
            await claude_page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
            return True
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    except Exception:
        return False
    finally:
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass


async def send_prompt_to_claude(prompt: str) -> tuple[bool, str]:
    """
    Attach to existing Chrome via CDP, focus claude.ai, type the prompt, send.

    Returns (success, action_or_error_message).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return False, "Empty prompt"

    if not _PLAYWRIGHT_OK or async_playwright is None:
        return False, "Playwright is not installed."

    cdp = _cdp_url()

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp)
            except Exception:
                ok, msg = _clipboard_paste_into_chrome(prompt)
                if not ok:
                    return False, msg
                import subprocess

                subprocess.run(
                    ["osascript", "-"],
                    input='delay 0.3\ntell application "System Events" to key code 36\n',
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return True, f"Sent prompt to Claude (no CDP; clipboard + Enter): '{prompt}'"

            try:
                contexts = browser.contexts
                context = contexts[0] if contexts else await browser.new_context()

                claude_page = None
                for page in context.pages:
                    try:
                        u = (page.url or "").lower()
                        if "claude.ai" in u:
                            claude_page = page
                            await page.bring_to_front()
                            break
                    except Exception:
                        continue

                if not claude_page:
                    claude_page = await context.new_page()
                    await claude_page.goto(
                        (os.getenv("CLAUDE_PLAYWRIGHT_START_URL") or "https://claude.ai/new").strip(),
                        wait_until="domcontentloaded",
                        timeout=90000,
                    )
                    await asyncio.sleep(2)

                await claude_page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(0.5)

                input_field = await _find_composer_handle(claude_page)

                if not input_field:
                    return False, "Could not find Claude message input (UI may have changed)."

                await input_field.click()
                await claude_page.keyboard.press("Meta+a")
                await asyncio.sleep(0.1)
                await input_field.type(prompt, delay=30)
                await asyncio.sleep(0.5)

                await claude_page.keyboard.press("Enter")
                await asyncio.sleep(0.5)

                return True, f"Sent prompt to Claude: '{prompt}'"
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    except Exception as e:
        return False, f"Playwright error: {e!s}"
