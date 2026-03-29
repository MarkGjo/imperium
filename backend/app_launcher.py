"""
Detect installed macOS apps and open them natively, or fall back to Chrome + URL.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from urllib.parse import quote, quote_plus

import httpx

from chrome_helpers import build_chrome_new_tab_script

# Key: shorthand the user might say — Value: .app bundle names to check (order matters)
APP_MAP: dict[str, list[str]] = {
    # Music & Entertainment
    "spotify": ["Spotify.app"],
    "apple music": ["Music.app"],
    "music": ["Music.app"],
    "podcasts": ["Podcasts.app"],
    "garage band": ["GarageBand.app"],
    "garageband": ["GarageBand.app"],
    "vlc": ["VLC.app"],
    "iina": ["IINA.app"],
    "plex": ["Plex.app"],
    "netflix": ["Netflix.app"],
    "disney plus": ["Disney+.app"],
    "disney": ["Disney+.app"],
    "twitch": ["Twitch.app"],
    "soundcloud": ["SoundCloud.app"],
    "tidal": ["TIDAL.app"],
    "deezer": ["Deezer.app"],
    "youtube music": ["YouTube Music.app"],
    # Productivity
    "notion": ["Notion.app"],
    "slack": ["Slack.app"],
    "zoom": ["Zoom.app"],
    "teams": ["Microsoft Teams.app", "Microsoft Teams classic.app"],
    "microsoft teams": ["Microsoft Teams.app"],
    "discord": ["Discord.app"],
    "whatsapp": ["WhatsApp.app"],
    "telegram": ["Telegram.app"],
    "signal": ["Signal.app"],
    "skype": ["Skype.app"],
    # Email & Calendar
    "gmail": ["Gmail.app"],
    "outlook": ["Microsoft Outlook.app"],
    "mail": ["Mail.app"],
    "apple mail": ["Mail.app"],
    "calendar": ["Calendar.app"],
    "apple calendar": ["Calendar.app"],
    "fantastical": ["Fantastical.app"],
    "spark": ["Spark.app"],
    # Dev Tools
    "vs code": ["Visual Studio Code.app"],
    "vscode": ["Visual Studio Code.app"],
    "visual studio code": ["Visual Studio Code.app"],
    "xcode": ["Xcode.app"],
    "terminal": ["Terminal.app", "iTerm.app"],
    "iterm": ["iTerm.app"],
    "github desktop": ["GitHub Desktop.app"],
    "github": ["GitHub Desktop.app"],
    "postman": ["Postman.app"],
    "docker": ["Docker.app"],
    "cursor": ["Cursor.app"],
    "sublime": ["Sublime Text.app"],
    "sublime text": ["Sublime Text.app"],
    "atom": ["Atom.app"],
    "webstorm": ["WebStorm.app"],
    "pycharm": ["PyCharm.app"],
    # Browsers
    "chrome": ["Google Chrome.app"],
    "google chrome": ["Google Chrome.app"],
    "firefox": ["Firefox.app"],
    "safari": ["Safari.app"],
    "brave": ["Brave Browser.app"],
    "arc": ["Arc.app"],
    "opera": ["Opera.app"],
    "edge": ["Microsoft Edge.app"],
    # Design & Creative
    "figma": ["Figma.app"],
    "sketch": ["Sketch.app"],
    "canva": ["Canva.app"],
    "photoshop": [
        "Adobe Photoshop 2025.app",
        "Adobe Photoshop 2024.app",
        "Adobe Photoshop 2023.app",
        "Adobe Photoshop.app",
    ],
    "illustrator": ["Adobe Illustrator 2024.app", "Adobe Illustrator.app"],
    "premiere": ["Adobe Premiere Pro 2024.app", "Adobe Premiere Pro.app"],
    "after effects": ["Adobe After Effects 2024.app", "Adobe After Effects.app"],
    "lightroom": ["Adobe Lightroom Classic.app", "Adobe Lightroom.app"],
    "final cut": ["Final Cut Pro.app"],
    "final cut pro": ["Final Cut Pro.app"],
    "davinci resolve": ["DaVinci Resolve.app"],
    "davinci": ["DaVinci Resolve.app"],
    "logic pro": ["Logic Pro.app"],
    "logic": ["Logic Pro.app"],
    "procreate": ["Procreate.app"],
    # Cloud Storage
    "dropbox": ["Dropbox.app"],
    "google drive": ["Google Drive.app"],
    "onedrive": ["OneDrive.app"],
    "icloud": ["iCloud Drive.app"],
    # Finance
    "robinhood": ["Robinhood.app"],
    "coinbase": ["Coinbase.app"],
    "mint": ["Mint.app"],
    # Social
    "instagram": ["Instagram.app"],
    "twitter": ["Twitter.app"],
    "x": ["X.app"],
    "facebook": ["Facebook.app"],
    "tiktok": ["TikTok.app"],
    "reddit": ["Reddit.app"],
    "linkedin": ["LinkedIn.app"],
    "snapchat": ["Snapchat.app"],
    "bereal": ["BeReal.app"],
    # Utilities
    "notes": ["Notes.app"],
    "apple notes": ["Notes.app"],
    "reminders": ["Reminders.app"],
    "maps": ["Maps.app"],
    "apple maps": ["Maps.app"],
    "photos": ["Photos.app"],
    "facetime": ["FaceTime.app"],
    "messages": ["Messages.app"],
    "imessage": ["Messages.app"],
    "contacts": ["Contacts.app"],
    "find my": ["Find My.app"],
    "weather": ["Weather.app"],
    "calculator": ["Calculator.app"],
    "clock": ["Clock.app"],
    "voice memos": ["Voice Memos.app"],
    "shortcuts": ["Shortcuts.app"],
    "activity monitor": ["Activity Monitor.app"],
    "system preferences": ["System Preferences.app", "System Settings.app"],
    "system settings": ["System Settings.app", "System Preferences.app"],
    "app store": ["App Store.app"],
    "preview": ["Preview.app"],
    "quicktime": ["QuickTime Player.app"],
    "keynote": ["Keynote.app"],
    "pages": ["Pages.app"],
    "numbers": ["Numbers.app"],
    "word": ["Microsoft Word.app"],
    "excel": ["Microsoft Excel.app"],
    "powerpoint": ["Microsoft PowerPoint.app"],
    "1password": ["1Password 7.app", "1Password.app"],
    "alfred": ["Alfred.app"],
    "raycast": ["Raycast.app"],
    "bartender": ["Bartender 4.app"],
    "cleanmymac": ["CleanMyMac X.app"],
    "loom": ["Loom.app"],
    "grammarly": ["Grammarly Desktop.app"],
    "bear": ["Bear.app"],
    "obsidian": ["Obsidian.app"],
    "todoist": ["Todoist.app"],
    "things": ["Things 3.app"],
    "magnet": ["Magnet.app"],
    "screenflow": ["ScreenFlow.app"],
    "capcut": ["CapCut.app"],
    "finder": ["Finder.app"],
}

APP_SEARCH_PATHS = [
    "/Applications",
    "/Applications/Adobe",
    os.path.expanduser("~/Applications"),
    "/System/Applications",
    "/System/Applications/Utilities",
    "/System/Library/CoreServices",
]


def find_installed_app(app_name: str) -> str | None:
    """
    Return full path to an installed .app, or None.
    """
    app_name_lower = app_name.lower().strip()
    possible_apps = APP_MAP.get(app_name_lower, [])

    if not possible_apps:
        compact = app_name.title().replace(" ", "") + ".app"
        titled = app_name.title() + ".app"
        possible_apps = [compact, titled]

    for search_path in APP_SEARCH_PATHS:
        for app_filename in possible_apps:
            full_path = os.path.join(search_path, app_filename)
            if os.path.exists(full_path):
                return full_path

    return None


def open_native_app(app_path: str) -> tuple[bool, str]:
    """Open a .app by path. Returns (success, message)."""
    try:
        if not app_path or not os.path.exists(app_path):
            return False, "App path missing or not found"
        subprocess.run(["open", app_path], check=True, timeout=60)
        name = os.path.basename(app_path).replace(".app", "")
        return True, f"Opened {name} app on your Mac"
    except subprocess.CalledProcessError as e:
        return False, f"Failed to open app: {e!s}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Failed to open app: {e!s}"


def resolve_app_or_web(
    spoken_name: str,
    fallback_url: str | None = None,
) -> tuple[str, str, str]:
    """
    Try native app first, then Chrome script for fallback_url.

    Returns (method, action, path_or_script).
    method: native_app | chrome_tab | not_found
    """
    names: list[str] = []
    s = spoken_name.strip()
    if " and " in s.lower():
        names.append(s.split(" and ", 1)[0].strip())
    names.append(s)

    for name in names:
        app_path = find_installed_app(name)
        if app_path:
            base = os.path.basename(app_path).replace(".app", "")
            action = f"Opening {base} app installed on your Mac"
            return "native_app", action, app_path

    if fallback_url:
        script = build_chrome_new_tab_script(fallback_url)
        action = (
            f"{spoken_name.strip().title()} app not installed — opening {fallback_url} in Chrome"
        )
        return "chrome_tab", action, script

    action = f"Could not find {spoken_name.strip()} app or website"
    return "not_found", action, ""


def lookup_fallback_url(
    spoken_name: str,
    found_shortcuts: dict[str, str] | None,
) -> str | None:
    """Resolve a URL from shortcut dict or url_shortcuts.SHORTCUTS."""
    from url_shortcuts import SHORTCUTS

    found_shortcuts = found_shortcuts or {}
    low = spoken_name.lower().strip()
    parts = low.split()

    if low in found_shortcuts:
        return found_shortcuts[low]
    if low in SHORTCUTS:
        return SHORTCUTS[low]
    if parts and parts[0] in found_shortcuts:
        return found_shortcuts[parts[0]]
    if parts and parts[0] in SHORTCUTS:
        return SHORTCUTS[parts[0]]

    for key in sorted(SHORTCUTS.keys(), key=len, reverse=True):
        if key in low:
            return SHORTCUTS[key]
    return None


_COMPOUND_SPLIT_PATTERNS = [
    r"\band\s+play\b",
    r"\band\s+search\b",
    r"\band\s+find\b",
    r"\band\s+open\b",
    r"\band\s+go\b",
    r"\band\s+navigate\b",
    r"\band\s+type\b",
    r"\band\s+compose\b",
    r"\band\s+send\b",
    r"\bthen\s+play\b",
    r"\bthen\s+search\b",
    r"\bthen\s+open\b",
    r"\bafter\s+that\b",
]

_COMPOUND_OPEN_TRIGGERS = (
    "pull up",
    "bring up",
    "switch to",
    "open",
    "launch",
    "start",
    "run",
    "load",
)


def parse_compound_command(transcript: str) -> dict:
    """
    Detect open/launch + secondary action (play, search, etc.).
    """
    transcript_lower = transcript.lower().strip()

    split_index = -1
    for pattern in _COMPOUND_SPLIT_PATTERNS:
        match = re.search(pattern, transcript_lower)
        if match:
            split_index = match.start()
            break

    if split_index == -1:
        return {"is_compound": False}

    first_part = transcript_lower[:split_index].strip()
    second_part = transcript_lower[split_index:].strip()
    second_part = re.sub(
        r"^(and|then|after that)\s+",
        "",
        second_part,
    ).strip()

    app_name = ""
    for trigger in sorted(_COMPOUND_OPEN_TRIGGERS, key=len, reverse=True):
        pat = rf"\b{re.escape(trigger)}\s+(.+?)(?:\s+app)?$"
        m = re.search(pat, first_part)
        if m:
            app_name = m.group(1).strip()
            break

    if not app_name:
        return {"is_compound": False}

    secondary_type = "search"
    music_apps = (
        "spotify",
        "apple music",
        "music",
        "soundcloud",
        "tidal",
        "deezer",
        "youtube music",
    )

    if any(a in app_name for a in music_apps):
        if any(
            w in second_part
            for w in ("play", "song", "track", "artist", "album", "songs")
        ):
            secondary_type = "play_music"
        else:
            secondary_type = "search_music"
    elif any(w in second_part for w in ("search", "find", "look up", "look for")):
        secondary_type = "search"
    elif any(w in second_part for w in ("navigate", "go to", "open")):
        secondary_type = "navigate"
    elif any(w in second_part for w in ("compose", "write", "send", "email")):
        secondary_type = "compose"
    elif any(w in second_part for w in ("type", "write", "enter")):
        secondary_type = "type"

    return {
        "is_compound": True,
        "app_name": app_name,
        "secondary_action": second_part,
        "secondary_type": secondary_type,
    }


def _spotify_query_user_spec(action_lower: str) -> str | None:
    """Clean search query for Spotify (native + web) — user-specified extraction."""
    by_match = re.search(
        r"(?:play|song|track)\s+(?:the\s+)?(?:song\s+)?(.+?)\s+by\s+(.+?)(?:\.|,|\s+the\s+artist)?$",
        action_lower,
    )
    if by_match:
        song = by_match.group(1).strip()
        artist = by_match.group(2).strip()
        query = f"{song} {artist}"
    else:
        just_play = re.search(
            r"(?:play|song|track)\s+(?:the\s+)?(?:song\s+)?(.+?)(?:\.|,)?$",
            action_lower,
        )
        query = just_play.group(1).strip() if just_play else action_lower

    filler = (
        "instrumental music playing",
        "music playing",
        "the artist",
        "the rapper",
        "the singer",
        "please",
        "for me",
        "now",
    )
    for f in filler:
        query = query.replace(f, "").strip()

    query = re.sub(r"\s+", " ", query).strip()
    return query if query else None


def _spotify_escaped_search_uri(query: str) -> str:
    encoded = query.replace(" ", "%20")
    search_uri = f"spotify:search:{encoded}"
    return search_uri.replace("\\", "\\\\").replace('"', '\\"')


def _spotify_escape_applescript_literal(value: str) -> str:
    """Escape a string for use inside AppleScript double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _spotify_parse_song_artist(action_lower: str) -> tuple[str, str] | None:
    """Extract (song_name, artist_name) for Spotify API; artist may be empty."""
    t = action_lower.strip()
    by_match = re.search(
        r"(?:play|song|track)\s+(?:the\s+)?(?:song\s+)?(.+?)\s+by\s+(.+?)(?:\.|,|\s+the\s+(?:artist|rapper|singer|band|group))?$",
        t,
        flags=re.I,
    )
    if by_match:
        song_name = by_match.group(1).strip()
        artist_name = by_match.group(2).strip()
        artist_name = re.sub(
            r"\s*(,\s*)?(the\s+)?(artist|rapper|singer|band|group).*$",
            "",
            artist_name,
            flags=re.I,
        ).strip()
        if song_name:
            return song_name, artist_name
    just_play = re.search(
        r"(?:play|song|track)\s+(?:the\s+)?(?:song\s+)?(.+?)(?:\.|,)?$",
        t,
    )
    if just_play:
        s = just_play.group(1).strip()
        if s:
            return s, ""
    return None


async def get_spotify_token() -> str | None:
    """Client-credentials token for search (no user login)."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if (
        not client_id
        or not client_secret
        or client_id == "your_client_id"
        or client_secret == "your_client_secret"
    ):
        return None
    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )
            if response.status_code != 200:
                return None
            data = response.json()
            return data.get("access_token")
    except (httpx.HTTPError, ValueError, TypeError):
        return None


async def search_spotify_track(
    song_name: str,
    artist_name: str = "",
) -> dict | None:
    """
    Search Spotify Web API for a track and return the best match with URI.

    Matching priority: exact title+artist, exact title, starts-with title,
    then first API result.
    """
    token = await get_spotify_token()
    if not token:
        return None
    song_name = (song_name or "").strip()
    artist_name = (artist_name or "").strip()
    if not song_name:
        return None
    if artist_name:
        q = f"track:{song_name} artist:{artist_name}"
    else:
        q = song_name
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": q, "type": "track", "limit": 5},
            )
            if response.status_code != 200:
                return None
            data = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return None

    tracks = data.get("tracks", {}).get("items") or []
    if not tracks:
        return None

    def _primary_artist(track: dict) -> str:
        artists = track.get("artists") or []
        return artists[0]["name"] if artists else ""

    song_lower = song_name.lower().strip()
    artist_lower = artist_name.lower().strip() if artist_name else ""

    if artist_lower:
        for track in tracks:
            track_name = track["name"].lower().strip()
            track_artists = [a["name"].lower() for a in track.get("artists") or []]
            if song_lower == track_name and any(
                artist_lower in a for a in track_artists
            ):
                return {
                    "uri": track["uri"],
                    "name": track["name"],
                    "artist": _primary_artist(track),
                    "matched": "exact",
                }

    for track in tracks:
        track_name = track["name"].lower().strip()
        if song_lower == track_name:
            return {
                "uri": track["uri"],
                "name": track["name"],
                "artist": _primary_artist(track),
                "matched": "title_only",
            }

    for track in tracks:
        track_name = track["name"].lower().strip()
        if track_name.startswith(song_lower):
            return {
                "uri": track["uri"],
                "name": track["name"],
                "artist": _primary_artist(track),
                "matched": "starts_with",
            }

    first = tracks[0]
    return {
        "uri": first["uri"],
        "name": first["name"],
        "artist": _primary_artist(first),
        "matched": "first_result",
    }


def build_spotify_keyboard_script(search_uri_escaped: str) -> str:
    """
    Fallback 1: load search in UI, then Tab to first result and Enter.
    search_uri_escaped must be safe for AppleScript string literal (use _spotify_escaped_search_uri).
    """
    return f"""tell application "Spotify"
    activate
    delay 2
    open location "{search_uri_escaped}"
    delay 3
end tell
tell application "System Events"
    tell process "Spotify"
        delay 1
        key code 48
        delay 0.4
        key code 48
        delay 0.4
        key code 48
        delay 0.4
        key code 48
        delay 0.4
        key code 36
        delay 0.3
    end tell
end tell"""


def _build_spotify_fallback2_script(search_uri_escaped: str) -> str:
    """Last resort: open search, then single Return in the Spotify process."""
    return f"""tell application "Spotify"
    activate
    delay 2
    open location "{search_uri_escaped}"
    delay 4
end tell
tell application "System Events"
    tell process "Spotify"
        key code 36
    end tell
end tell"""


async def build_spotify_in_app_bundle(
    secondary_action: str,
) -> tuple[str, str, str, str] | None:
    """
    Native Spotify automation: resolve exact track via Web API when possible,
    then play that URI (does not resume a paused unrelated track).

    Returns:
        (primary_play_track_script, fallback1_keyboard_script,
         fallback2_enter_script, human_action_description)
    """
    action_lower = secondary_action.lower().strip()
    query = _spotify_query_user_spec(action_lower)
    if not query:
        return None

    esc_search = _spotify_escaped_search_uri(query)
    fb1 = build_spotify_keyboard_script(esc_search)
    fb2 = _build_spotify_fallback2_script(esc_search)

    track: dict | None = None
    parsed = _spotify_parse_song_artist(action_lower)
    if parsed:
        song_name, artist_name = parsed
        track = await search_spotify_track(song_name, artist_name)
    if not track:
        track = await search_spotify_track(query, "")

    if track:
        uri_esc = _spotify_escape_applescript_literal(track["uri"])
        primary = f"""tell application "Spotify"
    activate
    delay 1.5
    play track "{uri_esc}"
end tell"""
        human = (
            f"Playing '{track['name']}' by {track['artist']} on Spotify"
        )
        return primary, fb1, fb2, human

    primary = f"""tell application "Spotify"
    activate
    delay 1.5
    play track "{esc_search}"
end tell"""
    human = f"Searching and playing: {query.strip()} on Spotify"
    return primary, fb1, fb2, human


async def build_in_app_action_script(
    app_name: str,
    secondary_action: str,
    secondary_type: str,
    *,
    spotify_use_native: bool = True,
) -> str | None:
    """
    AppleScript or shell-backed automation after the app / browser is available.
    If Spotify is opened via web (Chrome) because the app is missing, pass spotify_use_native=False.
    """
    app_lower = app_name.lower().strip()
    action_lower = secondary_action.lower().strip()

    if "spotify" in app_lower:
        if not spotify_use_native:
            qtext = _spotify_query_user_spec(action_lower)
            if not qtext:
                return None
            return build_chrome_new_tab_script(
                f"https://open.spotify.com/search/{quote(qtext, safe='')}"
            )
        bundle = await build_spotify_in_app_bundle(secondary_action)
        return bundle[0] if bundle else None

    if app_lower in ("apple music", "music"):
        search_term = re.sub(
            r"^(play|search|find|look up)(?:\s+for)?\s+(?:the\s+song\s+)?",
            "",
            action_lower,
        ).strip()
        search_term = re.sub(
            r"\s*(,\s*)?(the\s+)?(artist|rapper|singer|band).*$",
            "",
            search_term,
            flags=re.I,
        ).strip()
        if not search_term:
            return None
        enc = quote_plus(search_term)
        return f'''tell application "Music"
    activate
    delay 1
    open location "https://music.apple.com/search?term={enc}"
end tell'''

    if "youtube" in app_lower:
        search_term = re.sub(
            r"^(play|search|find|look up)(?:\s+for)?\s+",
            "",
            action_lower,
        ).strip()
        search_term = re.sub(
            r"\s*(videos?|tutorials?|channels?).*$",
            "",
            search_term,
            flags=re.I,
        ).strip()
        if not search_term:
            return None
        q = quote_plus(search_term)
        url = f"https://www.youtube.com/results?search_query={q}"
        return build_chrome_new_tab_script(url)

    if any(a in app_lower for a in ("gmail", "mail", "outlook")):
        return None

    return None
