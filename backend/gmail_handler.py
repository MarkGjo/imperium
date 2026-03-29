"""
Gmail compose + send via Playwright attached to existing Chrome (CDP).

Requires Chrome with --remote-debugging-port and PLAYWRIGHT_CDP_URL in .env.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def transcript_wants_gmail_compose(transcript: str) -> bool:
    tl = transcript.lower()
    triggers = [
        "compose",
        "write an email",
        "send an email",
        "new email",
        "draft an email",
        "email to",
    ]
    if not any(t in tl for t in triggers):
        return False
    return bool(_EMAIL.search(transcript))


def extract_gmail_fields(transcript: str) -> dict[str, str | None]:
    """Extract recipient, subject, body from natural-language transcripts."""
    t = transcript.strip()
    to_addr: str | None = None
    m = re.search(
        r"to\s+(?:the\s+)?(?:recipient\s+)?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        t,
        re.I,
    )
    if m:
        to_addr = m.group(1)
    else:
        em = _EMAIL.search(t)
        if em:
            to_addr = em.group(0)

    subject: str | None = None
    for p in (
        r"subject\s+it\s+as\s+['\"\u201c\u201d]?([^'\"\u201c\u201d]+?)['\"\u201c\u201d]?(?=\s+message|\s+body|\s+and\s+send|$)",
        r"subject\s+(?:is|line\s+(?:is\s+)?)['\"\u201c\u201d]([^'\"\u201c\u201d]+)['\"\u201c\u201d]",
        r"subject\s+line\s+['\"\u201c\u201d]?([^'\"\u201c\u201d\n]+?)['\"\u201c\u201d]?(?=\s+message|\s+body|\s+and|\s*$)",
        r"subject\s+([^.]+?)(?=\s+message\s+|\s+body\s+|\s+and\s+send|$)",
    ):
        m = re.search(p, t, re.I | re.DOTALL)
        if m:
            subject = m.group(1).strip()
            if subject:
                break

    body: str | None = None
    for p in (
        r"message\s+(?:is\s+)?['\"\u201c\u201d]([^'\"\u201c\u201d]+)['\"\u201c\u201d]",
        r"(?:body|saying|that\s+says)\s+['\"\u201c\u201d]([^'\"\u201c\u201d]+)['\"\u201c\u201d]",
        r"message\s+(.+?)(?=\s+and\s+send|\s*$)",
    ):
        m = re.search(p, t, re.I | re.DOTALL)
        if m:
            body = m.group(1).strip()
            if body:
                break

    return {"to": to_addr, "subject": subject or "", "body": body or ""}


def _cdp_url() -> str:
    return (os.getenv("PLAYWRIGHT_CDP_URL") or "http://localhost:9222").strip()


def playwright_gmail_enabled() -> bool:
    v = os.getenv("USE_PLAYWRIGHT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


async def run_gmail_compose_atomic(transcript: str) -> dict[str, Any]:
    """
    Full compose + send in one flow: attach CDP → Gmail → compose → fill → send.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "transcript": transcript,
            "action": "Playwright not installed",
            "method": "gmail_playwright",
            "success": False,
            "error": "pip install playwright && playwright install chromium",
            "osascript_ok": False,
        }

    if not playwright_gmail_enabled():
        return {
            "transcript": transcript,
            "action": "Gmail automation disabled (USE_PLAYWRIGHT)",
            "method": "gmail_playwright",
            "success": False,
            "error": "Set USE_PLAYWRIGHT=1 in .env",
            "osascript_ok": False,
        }

    fields = extract_gmail_fields(transcript)
    to_email = fields.get("to")
    if not to_email or not isinstance(to_email, str):
        return {
            "transcript": transcript,
            "action": "Could not find recipient email in transcript",
            "method": "gmail_playwright",
            "success": False,
            "error": "Missing recipient email",
            "osascript_ok": False,
        }

    subject = (fields.get("subject") or "").strip() or "(no subject)"
    body = (fields.get("body") or "").strip() or ""

    print(f"[gmail] atomic compose → to={to_email!r} subject={subject!r} body_len={len(body)}")

    cdp = _cdp_url()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp, timeout=15000)
        except Exception as e:
            return {
                "transcript": transcript,
                "action": f"Could not attach to Chrome at {cdp}",
                "method": "gmail_playwright",
                "success": False,
                "error": str(e),
                "hint": "Start Chrome with --remote-debugging-port=9222 and set PLAYWRIGHT_CDP_URL.",
                "osascript_ok": False,
            }

        context = browser.contexts[0] if browser.contexts else None
        if not context:
            return {
                "transcript": transcript,
                "action": "No browser context from CDP",
                "method": "gmail_playwright",
                "success": False,
                "error": "No Chrome context — open Chrome first.",
                "osascript_ok": False,
            }

        page = None
        for pg in context.pages:
            try:
                u = pg.url or ""
                if "mail.google.com" in u or "gmail.com" in u:
                    page = pg
                    break
            except Exception:
                continue

        if not page:
            page = await context.new_page()
            await page.goto(
                "https://mail.google.com/mail/u/0/#inbox",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        else:
            await page.bring_to_front()
            await page.wait_for_load_state("domcontentloaded", timeout=30000)

        await asyncio.sleep(2.5)

        try:
            await _gmail_compose_and_send(page, to_email, subject, body)
        except Exception as e:
            print(f"[gmail] failure: {e!s}")
            return {
                "transcript": transcript,
                "action": f"Gmail automation failed: {e!s}",
                "method": "gmail_playwright",
                "success": False,
                "error": str(e),
                "osascript_ok": False,
            }

        return {
            "transcript": transcript,
            "action": f"Sent email to {to_email} — subject: {subject[:60]}{'…' if len(subject) > 60 else ''}",
            "method": "gmail_playwright",
            "success": True,
            "osascript_ok": True,
            "result": "",
            "error": None,
        }


async def _gmail_compose_and_send(
    page: Any, to_email: str, subject: str, body: str
) -> None:
    """Assume Gmail loaded; open compose, fill, send."""
    # Compose button (Gmail UI variants)
    compose_selectors = [
        "div[role='button'][gh='cm']",
        "[gh='cm']",
        "div[role='button'][data-tooltip*='Compose']",
        "div[role='button']:has-text('Compose')",
    ]
    clicked = False
    for sel in compose_selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=8000)
            await loc.click(timeout=5000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        raise RuntimeError("Could not find Gmail Compose button — is Gmail fully loaded?")

    await asyncio.sleep(2.0)

    # To field
    to_locators = [
        "input[name='to']",
        "textarea[name='to']",
        "input[peoplekit-id]",
        "textarea[aria-label*='To']",
        "input[aria-label*='To']",
    ]
    filled_to = False
    for sel in to_locators:
        try:
            box = page.locator(sel).first
            await box.wait_for(state="visible", timeout=10000)
            await box.fill(to_email, timeout=5000)
            filled_to = True
            break
        except Exception:
            continue
    if not filled_to:
        raise RuntimeError("Could not fill To field")

    await asyncio.sleep(0.6)

    # Subject
    subj_selectors = ["input[name='subjectbox']", "input[placeholder*='Subject']", "input[aria-label*='Subject']"]
    for sel in subj_selectors:
        try:
            s = page.locator(sel).first
            await s.wait_for(state="visible", timeout=5000)
            await s.fill(subject, timeout=5000)
            break
        except Exception:
            continue

    await asyncio.sleep(0.5)

    # Body — contenteditable
    body_selectors = [
        "div[aria-label='Message Body']",
        "div[aria-label*='essage Body']",
        "div[contenteditable='true'][g_editable]",
        "div[role='textbox'][contenteditable='true']",
    ]
    filled_body = False
    for sel in body_selectors:
        try:
            b = page.locator(sel).first
            await b.wait_for(state="visible", timeout=8000)
            await b.click(timeout=3000)
            await b.fill(body, timeout=5000)
            filled_body = True
            break
        except Exception:
            continue
    if not filled_body and body:
        try:
            await page.keyboard.type(body, delay=15)
        except Exception:
            pass

    await asyncio.sleep(0.8)

    # Send
    send_clicked = False
    for sel in (
        "div[role='button'][data-tooltip*='Send']",
        "div[role='button'][aria-label*='Send']",
        "div[aria-label='Send']",
    ):
        try:
            btn = page.locator(sel).first
            await btn.wait_for(state="visible", timeout=6000)
            await btn.click(timeout=5000)
            send_clicked = True
            break
        except Exception:
            continue
    if not send_clicked:
        await page.keyboard.press("Meta+Enter")
        await asyncio.sleep(1.0)

    # Confirmation: toast or compose closed
    try:
        await page.wait_for_selector(
            "text=/Message sent|Sending/",
            timeout=15000,
        )
    except Exception:
        await asyncio.sleep(2.0)
