"""Google Chrome: always open URLs in a new tab on the existing window (never a new window)."""

from __future__ import annotations

import subprocess


def escape_url_for_chrome_applescript(url: str) -> str:
    """Escape backslashes and double quotes for AppleScript string literals."""
    return url.replace("\\", "\\\\").replace('"', '\\"')


def build_chrome_new_tab_script(url: str) -> str:
    """
    Builds AppleScript that opens a URL in a new tab of the
    existing Chrome window. NEVER opens a new window (unless Chrome has no windows yet).
    """
    u = escape_url_for_chrome_applescript(url)
    return f"""tell application "Google Chrome"
    activate
    delay 0.5
    if (count of windows) > 0 then
        tell front window
            set newTab to make new tab at end of tabs
            set URL of newTab to "{u}"
            set active tab index to (count of tabs)
        end tell
    else
        make new window
        set URL of active tab of front window to "{u}"
    end if
end tell"""


def build_chrome_new_empty_tab_script() -> str:
    """New blank tab in the front window, or one window if Chrome has none."""
    return """tell application "Google Chrome"
    activate
    delay 0.5
    if (count of windows) > 0 then
        tell front window
            set newTab to make new tab at end of tabs
            set active tab index to (count of tabs)
        end tell
    else
        make new window
    end if
end tell"""


def _run_osascript(script: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["osascript", "-"],
        input=script.strip() + "\n",
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, (result.stderr or "").strip() or "osascript failed"
    return True, ""


def open_url_in_chrome(url: str) -> tuple[bool, str]:
    """
    Executes the new tab script immediately.
    Returns (success, error_message)
    """
    script = build_chrome_new_tab_script(url)
    return _run_osascript(script)


def open_chrome_new_empty_tab() -> tuple[bool, str]:
    """New empty tab via the same AppleScript path (no URL)."""
    return _run_osascript(build_chrome_new_empty_tab_script())
