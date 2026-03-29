"""
Execute parsed CommandStep sequences (used when a voice command splits into multiple steps).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Any, Callable
from urllib.parse import quote_plus

from command_parser import CommandStep

OsascriptFn = Callable[..., Any]


def _playwright_enabled() -> bool:
    v = os.getenv("USE_PLAYWRIGHT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _run_osascript(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["osascript", "-"],
        input=script.strip() + "\n",
        capture_output=True,
        text=True,
        timeout=120,
    )


async def wait_for_step_type(action: str, method: str | None = None) -> None:
    """Fixed waits after a step succeeds so the next step does not race."""
    wait_times: dict[str, float] = {
        "open": 3.0,
        "type": 0.5,
        "submit": 1.0,
        "play": 2.0,
        "search": 2.5,
        "volume": 0.3,
        "screenshot": 0.5,
        "read": 0.0,
        "close": 0.5,
    }
    w = wait_times.get(action, 1.0)
    if action == "open" and method == "chrome_new_tab":
        w = max(w, 3.0)
    elif action == "open" and method == "native_app":
        w = max(w, 2.5)
    if w > 0:
        await asyncio.sleep(w)


async def _confirm_open_step_if_possible(method: str | None) -> None:
    """Best-effort: ensure Chrome tab exists after URL open (CDP ping)."""
    if method != "chrome_new_tab":
        return
    if _playwright_enabled():
        try:
            from playwright.async_api import async_playwright

            cdp = (os.getenv("PLAYWRIGHT_CDP_URL") or "http://localhost:9222").strip()
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(cdp, timeout=8000)
                ctx = browser.contexts[0] if browser.contexts else None
                if ctx and ctx.pages:
                    for pg in ctx.pages[:5]:
                        try:
                            u = pg.url or ""
                            if u.startswith("http"):
                                await pg.wait_for_load_state(
                                    "domcontentloaded", timeout=5000
                                )
                                return
                        except Exception:
                            continue
        except Exception:
            pass
    await asyncio.sleep(1.0)


async def execute_with_confirmation(
    step: CommandStep,
    found_shortcuts: dict[str, str],
    shortcut_context: str,
    get_applescript_fn: Callable[..., Any],
    run_applescript_fn: Callable[..., Any],
) -> dict[str, Any]:
    max_retries = 2
    last: dict[str, Any] = {}
    for attempt in range(max_retries):
        try:
            result = await execute_single_step(
                step,
                found_shortcuts,
                shortcut_context,
                get_applescript_fn,
                run_applescript_fn,
            )
        except Exception as e:
            last = {
                "success": False,
                "error": str(e),
                "action": str(e),
                "method": "step_exception",
            }
            if attempt < max_retries - 1:
                print(
                    f"[STEP {step.index + 1}] attempt {attempt + 1} failed: {e!s}; retrying…"
                )
                await asyncio.sleep(1.0)
            continue

        last = result
        if result.get("success"):
            method = result.get("method")
            await wait_for_step_type(step.action, method)
            if step.action == "open":
                await _confirm_open_step_if_possible(method)
            return result

        if attempt < max_retries - 1:
            err = result.get("error") or result.get("action") or "failed"
            print(
                f"[STEP {step.index + 1}] attempt {attempt + 1} failed: {err}; retrying…"
            )
            await asyncio.sleep(1.0)

    return last


async def execute_steps(
    steps: list[CommandStep],
    found_shortcuts: dict[str, str],
    shortcut_context: str,
    get_applescript_fn: Callable[..., Any],
    run_applescript_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Run steps strictly one at a time; confirm each before the next."""
    results: list[str] = []
    errors: list[str] = []
    methods_used: list[str] = []

    for step in steps:
        print(
            f"\n[STEP {step.index + 1}/{len(steps)}] "
            f"action={step.action} target={step.target} content={step.content!r}"
        )
        print(f"[STEP {step.index + 1}] Executing…")

        result = await execute_with_confirmation(
            step,
            found_shortcuts,
            shortcut_context,
            get_applescript_fn,
            run_applescript_fn,
        )

        if result.get("success"):
            act = result.get("action")
            if act:
                results.append(act)
            methods_used.append(result.get("method", "unknown"))
            print(f"[STEP {step.index + 1}] ✅ Complete: {act}")
        else:
            err = result.get("error") or result.get("action") or "failed"
            errors.append(f"Step {step.index + 1}: {err}")
            print(f"[STEP {step.index + 1}] ❌ Failed: {err}")
            if step.action in ("open", "type"):
                print(
                    f"[STEP {step.index + 1}] Critical action '{step.action}' failed — aborting chain"
                )
                break

        if step.index < len(steps) - 1:
            print(f"[STEP {step.index + 1}] Waiting before next step…")
            await asyncio.sleep(1.5)

    return {
        "action": " → ".join(results) if results else "No steps completed",
        "method": "+".join(dict.fromkeys(methods_used)) if methods_used else "multi_step",
        "errors": errors if errors else None,
        "steps_completed": len(results),
        "steps_total": len(steps),
        "osascript_ok": len(errors) == 0,
    }


async def execute_single_step(
    step: CommandStep,
    found_shortcuts: dict[str, str],
    shortcut_context: str,
    get_applescript_fn: Callable[..., Any],
    run_applescript_fn: Callable[..., Any],
) -> dict[str, Any]:
    if step.action == "read":
        return await execute_read_file(step)
    if step.action == "submit":
        return await execute_submit(step)
    if step.action == "type":
        return await execute_type(step)
    if step.action == "play" and step.target == "spotify":
        return await execute_spotify_play(step)
    if step.action == "search":
        return await execute_search(step, found_shortcuts)
    if step.action == "open":
        return await execute_open(step, found_shortcuts)
    if step.action == "screenshot":
        return await execute_screenshot()
    if step.action == "volume":
        return await execute_volume(step)
    return await execute_applescript_fallback(
        step, shortcut_context, get_applescript_fn, run_applescript_fn
    )


async def execute_submit(step: CommandStep) -> dict[str, Any]:
    if step.target == "claude" and _playwright_enabled():
        try:
            from playwright_claude import press_enter_in_claude

            success = await press_enter_in_claude()
            if success:
                return {
                    "action": "Submitted prompt in Claude.ai",
                    "method": "playwright_submit",
                    "success": True,
                }
        except Exception:
            pass

    script = """tell application "System Events"
    key code 36
end tell"""
    ex = _run_osascript(script)
    return {
        "action": "Pressed Enter to submit",
        "method": "applescript_enter",
        "success": ex.returncode == 0,
        "error": ex.stderr if ex.returncode != 0 else None,
    }


async def execute_type(step: CommandStep) -> dict[str, Any]:
    if not step.content.strip():
        return {
            "action": "No content to type",
            "method": "type_skip",
            "success": False,
            "error": "Could not extract text to type",
        }

    if step.target == "claude" and _playwright_enabled():
        try:
            from playwright_claude import type_in_claude

            success, action = await type_in_claude(step.content)
            method = (
                "clipboard_paste_chrome"
                if success and "Pasted into front Chrome" in action
                else "playwright_type"
            )
            return {
                "action": action,
                "method": method,
                "success": success,
                "error": None if success else action,
            }
        except Exception as e:
            return {
                "action": f"Failed to type in Claude: {e!s}",
                "method": "playwright_type_failed",
                "success": False,
                "error": str(e),
            }

    try:
        subprocess.run(
            ["pbcopy"],
            input=step.content.encode("utf-8"),
            check=False,
        )
        script = """tell application "System Events"
    keystroke "v" using command down
end tell"""
        ex = _run_osascript(script)
        return {
            "action": f"Pasted text: '{step.content[:80]}{'…' if len(step.content) > 80 else ''}'",
            "method": "clipboard_paste",
            "success": ex.returncode == 0,
            "error": ex.stderr if ex.returncode != 0 else None,
        }
    except Exception as e:
        return {
            "action": "Paste failed",
            "method": "clipboard_paste_failed",
            "success": False,
            "error": str(e),
        }


async def execute_read_file(step: CommandStep) -> dict[str, Any]:
    """Read a text file from ~/Downloads (file_reader) and echo to server terminal."""
    from file_reader import handle_file_read

    result = await handle_file_read(step.raw)
    if result.get("success") and result.get("content"):
        print("──────── file read (voice step) ────────")
        print(result["content"])
        print("──────── end file read ────────")
    return {
        "action": result.get("action", "Read file"),
        "method": result.get("method", "file_read_step"),
        "success": bool(result.get("success")),
        "error": result.get("error"),
    }


async def execute_open(step: CommandStep, found_shortcuts: dict[str, str]) -> dict[str, Any]:
    from app_launcher import find_installed_app, lookup_fallback_url, open_native_app
    from chrome_helpers import open_url_in_chrome
    from url_shortcuts import SHORTCUTS, url_from_shortcut_substrings

    target_key = step.target if step.target != "unknown" else ""
    spoken = target_key or (step.content.split()[0] if step.content else "")
    raw_l = step.raw.lower()

    if "download" in raw_l and ("finder" in raw_l or target_key == "finder"):
        try:
            subprocess.run(
                ["open", os.path.expanduser("~/Downloads")],
                check=True,
                timeout=60,
            )
            await asyncio.sleep(1.0)
            return {
                "action": "Opened Downloads folder in Finder",
                "method": "open_downloads_folder",
                "success": True,
            }
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    for name in (target_key, spoken, step.content):
        if not name:
            continue
        app_path = find_installed_app(name)
        if app_path:
            ok, msg = open_native_app(app_path)
            await asyncio.sleep(0.3)
            return {"action": msg, "method": "native_app", "success": ok, "error": None if ok else msg}

    url = None
    if target_key:
        url = lookup_fallback_url(target_key, found_shortcuts) or SHORTCUTS.get(target_key)
    if not url and spoken:
        url = lookup_fallback_url(spoken, found_shortcuts) or SHORTCUTS.get(spoken)
    if not url:
        url = url_from_shortcut_substrings(step.raw)

    if url:
        ok, err = open_url_in_chrome(url)
        await asyncio.sleep(0.3)
        return {
            "action": f"Opened {url} in Chrome",
            "method": "chrome_new_tab",
            "success": ok,
            "error": err if not ok else None,
        }

    return {
        "action": f"Could not find app or URL for: {step.raw!r}",
        "method": "open_failed",
        "success": False,
        "error": "Unknown open target",
    }


async def execute_spotify_play(step: CommandStep) -> dict[str, Any]:
    from app_launcher import build_spotify_in_app_bundle

    if not step.content.strip():
        return {
            "action": "No song query",
            "method": "spotify_play_failed",
            "success": False,
            "error": "Empty play content",
        }

    bundle = await build_spotify_in_app_bundle(f"play {step.content}")
    if not bundle:
        return {
            "action": "Could not build Spotify play",
            "method": "spotify_play_failed",
            "success": False,
        }

    primary, fb1, fb2, human = bundle
    ex = _run_osascript(primary)
    if ex.returncode != 0 and fb1:
        ex = _run_osascript(fb1)
    if ex.returncode != 0 and fb2:
        ex = _run_osascript(fb2)

    return {
        "action": human or "Played on Spotify",
        "method": "spotify_play",
        "success": ex.returncode == 0,
        "error": ex.stderr if ex.returncode != 0 else None,
    }


async def execute_search(step: CommandStep, found_shortcuts: dict[str, str]) -> dict[str, Any]:
    from chrome_helpers import open_url_in_chrome

    q = quote_plus(step.content) if step.content else ""
    if step.target == "youtube":
        url = f"https://www.youtube.com/results?search_query={q}"
    else:
        url = f"https://www.google.com/search?q={q}"

    ok, err = open_url_in_chrome(url)
    return {
        "action": f"Searched for '{step.content}'",
        "method": "chrome_search",
        "success": ok,
        "error": err if not ok else None,
    }


async def execute_screenshot() -> dict[str, Any]:
    script = 'do shell script "screencapture ~/Desktop/screenshot.png"'
    ex = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {
        "action": "Screenshot saved to Desktop (screenshot.png)",
        "method": "applescript_screenshot",
        "success": ex.returncode == 0,
        "error": ex.stderr if ex.returncode != 0 else None,
    }


async def execute_volume(step: CommandStep) -> dict[str, Any]:
    raw = step.raw + " " + step.content
    level_match = re.search(r"\d{1,3}", raw)
    level = int(level_match.group()) if level_match else 50
    level = max(0, min(100, level))
    script = f"set volume output volume {level}"
    ex = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "action": f"Volume set to {level}%",
        "method": "applescript_volume",
        "success": ex.returncode == 0,
        "error": ex.stderr if ex.returncode != 0 else None,
    }


async def execute_applescript_fallback(
    step: CommandStep,
    shortcut_context: str,
    get_applescript_fn: Callable[..., Any],
    run_applescript_fn: Callable[..., Any],
) -> dict[str, Any]:
    try:
        script, action = get_applescript_fn(step.raw, shortcut_context)
        exec_result, updated_action = run_applescript_fn(script, action)
        ok = exec_result.get("returncode", 1) == 0
        return {
            "action": updated_action or action,
            "method": "applescript",
            "success": ok,
            "error": exec_result.get("stderr") if not ok else None,
        }
    except Exception as e:
        return {
            "action": f"Could not execute: {step.raw}",
            "method": "applescript_failed",
            "success": False,
            "error": str(e),
        }
