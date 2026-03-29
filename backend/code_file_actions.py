"""
Create/edit Python files via the backend (reliable). AppleScript + embedded shell/Python often hits -2741 syntax errors.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _parse_json_from_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text.strip())


def transcript_requests_web_stack(transcript: str) -> bool:
    """HTML/CSS/JS or generic web page — not Python-only."""
    t = transcript.lower()
    return bool(
        re.search(
            r"\b(html|css|javascript|web\s*page|webpage|website|\.html\b|front\s*end|frontend|\bjs\b)\b",
            t,
            re.I,
        )
    )


def wants_vscode_html_create(transcript: str) -> bool:
    t = transcript.lower()
    if not re.search(r"visual studio code|vscode|\bvs code\b", t):
        return False
    if not transcript_requests_web_stack(transcript):
        return False
    return bool(
        re.search(r"\b(create|new file|write|make|add|build|program)\b", t, re.I)
    )


def wants_vscode_python_create(transcript: str) -> bool:
    t = transcript.lower()
    if transcript_requests_web_stack(t):
        return False
    if not re.search(r"visual studio code|vscode|\bvs code\b", t):
        return False
    if not re.search(r"\.py\b|python\s+file|calculator", t):
        return False
    return bool(re.search(r"\b(create|new file|write|make|add)\b", t))


def extract_py_filename(transcript: str) -> str:
    m = re.search(
        r"called\s+(?:a\s+)?(?:new\s+)?(?:file\s+)?[\"']?([\w\-]+\.py)[\"']?",
        transcript,
        re.I,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"(?:named|file)\s+[\"']?([\w\-]+\.py)[\"']?", transcript, re.I
    )
    if m:
        return m.group(1)
    m = re.search(r"\b([\w\-]+\.py)\b", transcript, re.I)
    if m:
        return m.group(1)
    return "script.py"


def extract_html_filename(transcript: str) -> str:
    m = re.search(
        r"called\s+(?:a\s+)?(?:new\s+)?(?:file\s+)?[\"']?([\w\-]+\.html)[\"']?",
        transcript,
        re.I,
    )
    if m:
        return m.group(1)
    m = re.search(
        r"(?:named|file)\s+[\"']?([\w\-]+\.html)[\"']?", transcript, re.I
    )
    if m:
        return m.group(1)
    m = re.search(r"\b([\w\-]+\.html)\b", transcript, re.I)
    if m:
        return m.group(1)
    if re.search(r"\bcalculator\b", transcript, re.I):
        return "calculator.html"
    return "index.html"


def create_vscode_html_file(transcript: str, shortcut_context: str = "") -> dict:
    """Single-file HTML+CSS+JS on Desktop, VS Code + default browser."""
    script_model = os.getenv("ANTHROPIC_SCRIPT_MODEL", "claude-haiku-4-5").strip()
    fname = extract_html_filename(transcript)
    out = Path.home() / "Desktop" / fname

    sc = shortcut_context.strip()
    prefix = f"{sc}\n" if sc else ""

    message = _claude.messages.create(
        model=script_model,
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": f"""{prefix}The user spoke this command (may be cut off). Respond with ONLY valid JSON (no markdown fences):
{{
  "html_document": "<full HTML5 document as a single string>",
  "action": "<one honest sentence: what you built and where it was saved>"
}}

Rules:
- One self-contained file: embedded <style> and <script>, no external CDN required (works offline).
- Valid HTML5, charset UTF-8, viewport meta for mobile.
- If they asked for a calculator: working + − × ÷, clear/equals, sensible layout, keyboard-friendly if possible.
- Polished, readable UI (spacing, contrast, button states). No placeholder-only stubs — it must work in a browser.
- The app will save to ~/Desktop/{fname} and open it; do not reference file:// paths that assume other files.
- Valid JSON only; escape double quotes and newlines inside html_document as JSON requires.

User command:
{transcript}
""",
            }
        ],
    )
    raw = message.content[0].text
    data = _parse_json_from_text(raw)
    html = data.get("html_document") or data.get("html")
    if not html:
        raise ValueError("Claude JSON missing html_document")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    subprocess.run(
        ["open", "-a", "Visual Studio Code", str(out)],
        check=False,
    )
    subprocess.run(["open", str(out)], check=False)

    action = (
        data.get("action")
        or f"Saved {out.name} on Desktop, opened in VS Code and your default browser"
    )
    return {
        "action": action,
        "result": str(out),
        "osascript_ok": True,
        "method": "html_file_write",
        "opened_in_browser": True,
    }


_WORD_NUM = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def wants_run_program(transcript: str) -> bool:
    return bool(re.search(r"\brun\b", transcript, re.I))


def extract_times_expression(transcript: str) -> str | None:
    """e.g. 'five times three' / '5 times 3' -> '5*3' for --eval."""
    t = transcript.lower()
    m = re.search(r"(\d+)\s*times\s*(\d+)", t)
    if m:
        return f"{m.group(1)}*{m.group(2)}"
    m = re.search(
        r"(zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+times\s+(zero|one|two|three|four|five|six|seven|eight|nine|ten)",
        t,
    )
    if m and m.group(1) in _WORD_NUM and m.group(2) in _WORD_NUM:
        return f"{_WORD_NUM[m.group(1)]}*{_WORD_NUM[m.group(2)]}"
    return None


# Prepended when user asked to run with "N*M" but generated code lacks --eval (Claude miss).
_ICONTROL_EVAL_PREFIX = """# icontrol: voice --eval (auto)
import sys as _icontrol_sys
import re as _icontrol_re

if __name__ == "__main__":
    if len(_icontrol_sys.argv) >= 3 and _icontrol_sys.argv[1] == "--eval":
        _e = _icontrol_sys.argv[2].replace(" ", "")
        _m = _icontrol_re.match(r"^(\\d+)\\*(\\d+)$", _e)
        if _m:
            print(int(_m.group(1)) * int(_m.group(2)))
            raise SystemExit(0)
        raise SystemExit(2)
"""


def open_terminal_run_script(path: Path) -> None:
    inner = f"cd {shlex.quote(str(path.parent))} && python3 {shlex.quote(path.name)}"
    as_inner = inner.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'tell application "Terminal" to do script "{as_inner}"'],
        check=False,
        capture_output=True,
    )


def maybe_run_python_after_write(path: Path, transcript: str) -> dict:
    """If user asked to run the program, execute --eval or open Terminal."""
    extra: dict = {}
    if not wants_run_program(transcript):
        return extra

    expr = extract_times_expression(transcript)
    if expr:
        r = subprocess.run(
            ["/usr/bin/python3", str(path), "--eval", expr],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(path.parent),
        )
        extra["program_exit_code"] = r.returncode
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode == 0 and out:
            extra["program_output"] = out
            extra["detail"] = f"Ran: python3 {path.name} --eval '{expr}' → {out}"
        else:
            extra["program_stderr"] = err or out
            extra["detail"] = (
                "Script did not handle --eval (regenerate) or failed. Opening Terminal to run interactively."
            )
            open_terminal_run_script(path)
    else:
        open_terminal_run_script(path)
        extra["detail"] = "Opened Terminal running: python3 " + path.name + " (type input there)."

    return extra


def create_vscode_python_file(transcript: str, shortcut_context: str = "") -> dict:
    """Ask Claude for Python source only, write file on Desktop, open VS Code."""
    script_model = os.getenv("ANTHROPIC_SCRIPT_MODEL", "claude-haiku-4-5").strip()
    fname = extract_py_filename(transcript)
    out = Path.home() / "Desktop" / fname

    sc = shortcut_context.strip()
    prefix = f"{sc}\n" if sc else ""

    message = _claude.messages.create(
        model=script_model,
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": f"""{prefix}The user spoke this command (may be cut off). Respond with ONLY valid JSON (no markdown fences):
{{
  "python_code": "<full Python 3 source, runnable with python3>",
  "action": "<one honest sentence: what you built and where it was saved>"
}}

Rules:
- Put the complete program in python_code (imports, main guard if appropriate).
- If they asked for a calculator, implement add/subtract/multiply/divide with a simple menu or REPL and handle divide-by-zero.
- REQUIRED for voice automation: inside `if __name__ == "__main__":`, handle FIRST:
  `if len(sys.argv) >= 3 and sys.argv[1] == "--eval":` then parse `sys.argv[2]` as a simple arithmetic expression
  (e.g. "5*3" or "5 * 3"), print the numeric result only, then `raise SystemExit(0)`.
  Use a safe evaluation (e.g. only digits and +-*/. and spaces) — no arbitrary eval of user strings.
  After that block, run the normal interactive calculator.
- Save location: file will be written to ~/Desktop/{fname}.
- Valid JSON only; escape double quotes and newlines inside python_code as JSON requires.

User command:
{transcript}
""",
            }
        ],
    )
    raw = message.content[0].text
    data = _parse_json_from_text(raw)
    code = data.get("python_code") or data.get("code")
    if not code:
        raise ValueError("Claude JSON missing python_code")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(code, encoding="utf-8")

    if (
        wants_run_program(transcript)
        and extract_times_expression(transcript)
        and "--eval" not in code
    ):
        code = _ICONTROL_EVAL_PREFIX + "\n" + code
        out.write_text(code, encoding="utf-8")

    subprocess.run(
        ["open", "-a", "Visual Studio Code", str(out)],
        check=False,
    )

    action = data.get("action") or f"Saved {out.name} on Desktop and opened it in VS Code"
    payload: dict = {
        "action": action,
        "result": str(out),
        "osascript_ok": True,
        "method": "python_file_write",
    }
    run_extra = maybe_run_python_after_write(out, transcript)
    payload.update(run_extra)
    if run_extra.get("program_output") and wants_run_program(transcript):
        payload["action"] = (
            f"{action} Output: {run_extra['program_output']}"
        )
    return payload
