"""
Keyword → canonical URL hints for voice commands (injected into Claude prompts).
"""

from __future__ import annotations

import re

SHORTCUTS: dict[str, str] = {
    # AI Tools
    "chat": "https://chat.com",
    "chatgpt": "https://chat.openai.com",
    "openai": "https://openai.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "perplexity": "https://perplexity.ai",
    "copilot": "https://copilot.microsoft.com",
    "midjourney": "https://midjourney.com",
    "grok": "https://grok.x.ai",
    # Productivity
    "gmail": "https://mail.google.com",
    "google mail": "https://mail.google.com",
    "calendar": "https://calendar.google.com",
    "google calendar": "https://calendar.google.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "docs": "https://docs.google.com",
    "sheets": "https://sheets.google.com",
    "slides": "https://slides.google.com",
    "notion": "https://notion.so",
    "trello": "https://trello.com",
    "slack": "https://slack.com",
    "zoom": "https://zoom.us",
    "teams": "https://teams.microsoft.com",
    "outlook": "https://outlook.live.com",
    "dropbox": "https://dropbox.com",
    "figma": "https://figma.com",
    "linear": "https://linear.app",
    "asana": "https://asana.com",
    # Social Media
    "youtube": "https://youtube.com",
    "instagram": "https://instagram.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "tiktok": "https://tiktok.com",
    "facebook": "https://facebook.com",
    "linkedin": "https://linkedin.com",
    "reddit": "https://reddit.com",
    "snapchat": "https://snapchat.com",
    "pinterest": "https://pinterest.com",
    "twitch": "https://twitch.tv",
    "discord": "https://discord.com",
    "whatsapp": "https://web.whatsapp.com",
    # Shopping
    "amazon": "https://amazon.com",
    "ebay": "https://ebay.com",
    "etsy": "https://etsy.com",
    "walmart": "https://walmart.com",
    "target": "https://target.com",
    # News & Info
    "google": "https://google.com",
    "bing": "https://bing.com",
    "wikipedia": "https://wikipedia.org",
    "weather": "https://weather.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "news": "https://news.google.com",
    # Entertainment
    "netflix": "https://netflix.com",
    "spotify": "https://open.spotify.com",
    "hulu": "https://hulu.com",
    "disney": "https://disneyplus.com",
    "disney plus": "https://disneyplus.com",
    "apple music": "https://music.apple.com",
    "soundcloud": "https://soundcloud.com",
    # Dev Tools
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "vercel": "https://vercel.com",
    "heroku": "https://heroku.com",
    "replit": "https://replit.com",
    "codepen": "https://codepen.io",
    # Finance
    "paypal": "https://paypal.com",
    "venmo": "https://venmo.com",
    "cashapp": "https://cash.app",
    "robinhood": "https://robinhood.com",
    "coinbase": "https://coinbase.com",
}


def url_from_shortcut_substrings(text: str) -> str | None:
    """
    First URL from SHORTCUTS whose keyword appears in text.
    Uses the same safe rules as resolve_shortcuts (e.g. 'x' is not matched inside '.txt').
    """
    command_lower = text.lower()
    for keyword in sorted(SHORTCUTS.keys(), key=len, reverse=True):
        if keyword == "x":
            if not re.search(r"(?<![a-z0-9])x(?![a-z0-9])", command_lower):
                continue
        elif keyword not in command_lower:
            continue
        return SHORTCUTS[keyword]
    return None


def resolve_shortcuts(command: str) -> tuple[str, dict[str, str]]:
    """
    Scan for shortcut keywords (longest keys first to prefer 'google maps' over 'google').
    Returns the original command and a map keyword → URL for matches.
    """
    command_lower = command.lower()
    found: dict[str, str] = {}
    for keyword in sorted(SHORTCUTS.keys(), key=len, reverse=True):
        if keyword == "x":
            if not re.search(r"(?<![a-z0-9])x(?![a-z0-9])", command_lower):
                continue
        elif keyword not in command_lower:
            continue
        found[keyword] = SHORTCUTS[keyword]
    return command, found


def build_shortcut_context(found_shortcuts: dict[str, str]) -> str:
    if not found_shortcuts:
        return ""
    lines = ["KNOWN URL MAPPINGS FOR THIS COMMAND:"]
    for word, url in found_shortcuts.items():
        lines.append(f'- When user says "{word}" use this exact URL: {url}')
    lines.append("IMPORTANT: Always use these exact URLs as hardcoded strings.")
    lines.append("Never assign URLs to variables. Always inline them directly.")
    return "\n".join(lines) + "\n\n"


def pick_chrome_url_from_shortcuts(transcript: str, found: dict[str, str]) -> str | None:
    """Pick the longest matching shortcut keyword’s URL present in the transcript."""
    if not found:
        return None
    tl = transcript.lower()
    for kw in sorted(found.keys(), key=len, reverse=True):
        if kw in tl:
            return found[kw]
    return None
