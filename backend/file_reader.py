# file_reader.py

import os
import re
from pathlib import Path

# Safety constraints
ALLOWED_DIRECTORY = Path.home() / "Downloads"
MAX_FILE_SIZE_BYTES = 50_000  # 50KB max
ALLOWED_EXTENSIONS = [
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".html",
    ".log",
]


def extract_filename(transcript: str) -> str | None:
    """
    Extracts a filename from a voice transcript.

    Handles patterns like:
    - "read notes.txt"
    - "read my file called notes.txt"
    - "open notes.txt from downloads"
    - "what does todo.txt say"
    - "read the file report.txt in downloads"
    - "read the file black and pray it's good" → "black" (stops before filler)
    - "read notes" (no extension — tries common extensions)
    """
    transcript_lower = transcript.lower().strip()

    # Stop filename capture before these words (word boundaries — not inside filenames w/ ext in pattern 1)
    _STOP = r"(?=\s+(?:and|then|print|show|display|please|from|in|now)\b|$)"

    # Pattern 1 — explicit filename with extension
    ext_pattern = r"\b([\w\-]+\.(?:txt|md|csv|json|py|js|html|log))\b"
    match = re.search(ext_pattern, transcript_lower)
    if match:
        return match.group(1)

    # Pattern 2a — "the file NAME" — non-greedy until stop words (never past "and print", etc.)
    file_phrase = re.search(
        rf"\bthe\s+file\s+(.+?){_STOP}",
        transcript_lower,
    )
    if file_phrase:
        name = file_phrase.group(1).strip()
        name = re.sub(r"\s+", " ", name)
        if name and _looks_like_filename_token(name):
            return name

    # Pattern 2b — "called X" / "named X" / "titled X" (single \w token)
    called_match = re.search(
        r"(?:called|named|titled)\s+([\w\-]+)",
        transcript_lower,
    )
    if called_match:
        return called_match.group(1)

    # Pattern 2c — "file WORD" not after "the" (single token)
    file_word = re.search(r"\b(?<!the\s)file\s+([\w\-]+)\b", transcript_lower)
    if file_word:
        w = file_word.group(1)
        if w not in (
            "file",
            "files",
            "downloads",
            "called",
            "named",
            "from",
            "in",
            "the",
        ):
            return w

    # Pattern 3 — "read X" … single-token filename; stops at \w boundary
    read_match = re.search(
        r"(?:read|open|show|load)\s+(?:my\s+)?(?:file\s+)?(?:the\s+)?(?:called\s+)?([\w\-]+)(?:\s+from|\s+in|\s+file)?",
        transcript_lower,
    )
    if read_match:
        name = read_match.group(1).strip()
        excluded = [
            "file",
            "files",
            "downloads",
            "folder",
            "document",
            "documents",
            "the",
            "my",
            "a",
            "an",
            "it",
            "this",
            "that",
        ]
        if name not in excluded and len(name) > 1:
            return name

    return None


def _looks_like_filename_token(name: str) -> bool:
    if len(name) < 1:
        return False
    if len(name) > 120:
        return False
    return bool(re.match(r"^[\w\-\s]+$", name))


def _is_under_downloads(path: Path, downloads: Path) -> bool:
    try:
        path.resolve().relative_to(downloads)
        return True
    except ValueError:
        return False


def _try_path(
    candidate: Path, downloads: Path
) -> tuple[Path | None, str | None]:
    """Return (path, None) if readable, (None, err) if too large, (None, None) if skip."""
    try:
        resolved = candidate.resolve()
    except OSError:
        return None, None

    if not _is_under_downloads(resolved, downloads):
        return None, None

    if not resolved.exists() or not resolved.is_file():
        return None, None

    ext = resolved.suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        return None, None

    try:
        size = resolved.stat().st_size
    except OSError:
        return None, None

    if size > MAX_FILE_SIZE_BYTES:
        return (
            None,
            f"File is too large (max {MAX_FILE_SIZE_BYTES // 1000}KB): {resolved.name}",
        )

    return resolved, None


def _fuzzy_match_downloads(stem_query: str, downloads: Path) -> Path | None:
    """
    Case-insensitive stem / name match for STT variants (e.g. black vs Black.txt).
    Also handles extensionless files whose basename equals the query.
    """
    q = stem_query.strip().lower()
    if not q or len(q) > 200:
        return None

    matches: list[Path] = []
    try:
        for p in downloads.iterdir():
            if not p.is_file():
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz > MAX_FILE_SIZE_BYTES:
                continue
            if not _is_under_downloads(p, downloads):
                continue

            name_lower = p.name.lower()
            ext = p.suffix.lower()
            root_lower = p.stem.lower() if ext else name_lower

            if ext and ext not in ALLOWED_EXTENSIONS:
                continue

            # extensionless: file literally named "black"
            if not ext:
                if name_lower == q:
                    matches.append(p)
                continue

            if root_lower == q or name_lower == q:
                matches.append(p)
    except OSError:
        return None

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        for m in matches:
            if m.suffix.lower() == ".txt":
                return m
        return matches[0]
    return None


def resolve_safe_path(filename: str) -> tuple[Path | None, str | None]:
    """
    Safely resolves a filename to a path inside ~/Downloads.

    Security checks:
    - Must be inside ~/Downloads only
    - No path traversal (../ etc)
    - Must have allowed extension (or extensionless plain file)
    - Must not exceed max file size

    Returns (path, None) on success, (None, None) if not found,
    (None, error_message) if found but rejected (e.g. too large).
    """
    filename = os.path.basename(filename.strip())
    filename = re.sub(r"\s+", " ", filename).strip()
    downloads = ALLOWED_DIRECTORY.resolve()

    candidates: list[Path] = []
    if any(filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
        candidates.append(ALLOWED_DIRECTORY / filename)
    else:
        for ext in ALLOWED_EXTENSIONS:
            candidates.append(ALLOWED_DIRECTORY / f"{filename}{ext}")
        # Extensionless basename: ~/Downloads/black
        if "." not in filename:
            candidates.append(ALLOWED_DIRECTORY / filename)

    for candidate in candidates:
        ok, err = _try_path(candidate, downloads)
        if err:
            return None, err
        if ok:
            return ok, None

    # Case / STT mismatch: scan Downloads for stem match
    stem = Path(filename).stem if "." in filename else filename
    stem = stem.strip()
    if stem:
        fuzzy = _fuzzy_match_downloads(stem, downloads)
        if fuzzy:
            ok, err = _try_path(fuzzy, downloads)
            if err:
                return None, err
            if ok:
                print(f"[file_read] audit: fuzzy resolved {filename!r} → {ok.name!r}")
                return ok, None

    return None, None


def read_file_contents(path: Path) -> tuple[bool, str]:
    """
    Reads a file and returns its contents.
    Returns (success, content_or_error)
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if not content.strip():
            return False, "The file is empty"

        return True, content

    except PermissionError:
        return False, f"Permission denied reading {path.name}"
    except OSError as e:
        return False, f"Could not read file: {str(e)}"


def list_downloads_files() -> list[str]:
    """
    Lists all readable files in ~/Downloads.
    Used when user says 'what files do I have in downloads'
    """
    try:
        files = []
        for f in ALLOWED_DIRECTORY.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            try:
                if f.stat().st_size > MAX_FILE_SIZE_BYTES:
                    continue
            except OSError:
                continue
            try:
                f.resolve().relative_to(ALLOWED_DIRECTORY.resolve())
            except ValueError:
                continue
            files.append(f.name)
        return sorted(files)
    except OSError:
        return []


async def handle_file_read(transcript: str) -> dict:
    """
    MAIN FUNCTION — called from main.py routing.

    Detects file read intent, extracts filename,
    safely reads the file, returns contents.
    """

    transcript_lower = transcript.lower()
    if any(
        phrase in transcript_lower
        for phrase in [
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
    ):
        files = list_downloads_files()
        if files:
            file_list = ", ".join(files)
            print(f"[file_read] audit: list_downloads count={len(files)}")
            return {
                "action": f"Found {len(files)} files in Downloads: {file_list}",
                "content": file_list,
                "voice_content": file_list[:2000],
                "method": "file_list",
                "success": True,
            }
        else:
            return {
                "action": "No readable files found in Downloads",
                "content": "",
                "method": "file_list",
                "success": False,
            }

    filename = extract_filename(transcript)

    if not filename:
        return {
            "action": "Could not determine which file to read — please say the filename",
            "content": "",
            "method": "file_read_failed",
            "success": False,
            "error": "No filename detected in transcript",
        }

    print(f"[file_read] audit: attempt filename={filename!r} transcript={transcript!r}")

    file_path, reject_reason = resolve_safe_path(filename)

    if reject_reason:
        return {
            "action": reject_reason,
            "content": "",
            "method": "file_read_failed",
            "success": False,
            "error": reject_reason,
        }

    if not file_path:
        available = list_downloads_files()
        similar = [f for f in available if filename.lower() in f.lower()]

        if similar:
            suggestion = (
                f"Could not find '{filename}' — did you mean: {', '.join(similar)}?"
            )
        else:
            suggestion = f"Could not find '{filename}' in Downloads folder"

        return {
            "action": suggestion,
            "content": "",
            "method": "file_read_failed",
            "success": False,
            "error": f"File not found: {filename}",
        }

    success, content = read_file_contents(file_path)

    if not success:
        print(f"[file_read] audit: read failed path={file_path} err={content!r}")
        return {
            "action": content,
            "content": "",
            "method": "file_read_failed",
            "success": False,
            "error": content,
        }

    word_count = len(content.split())
    truncated = False
    voice_content = content

    if word_count > 500:
        words = content.split()[:500]
        voice_content = " ".join(words) + "..."
        truncated = True

    action = f"Read {file_path.name} from Downloads ({word_count} words)"
    if truncated:
        action += " — showing first 500 words"

    print(
        f"[file_read] audit: READ ok path={file_path} words={word_count} "
        f"truncated={truncated}"
    )

    return {
        "action": action,
        "content": content,
        "voice_content": voice_content,
        "filename": file_path.name,
        "word_count": word_count,
        "truncated": truncated,
        "method": "file_read",
        "success": True,
    }
