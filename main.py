"""AstrBot NameSearcher plugin.

The plugin deliberately keeps AstrBot imports small and probes optional APIs at
runtime.  Message component shapes vary between adapters, so attachment
extraction is recursive and accepts the common url/path/file fields.
"""

from __future__ import annotations

import asyncio
import base64
import bz2
import csv
import difflib
import hashlib
import hmac
import html
import inspect
import io
import json
import gzip
import lzma
import mimetypes
import re
import threading
import time
import tempfile
import textwrap
import tarfile
import urllib.parse
import urllib.request
import uuid
import unicodedata
import binascii
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional


# AstrBot moved a few imports across minor releases.  Keeping the fallback
# shims makes the module importable for local tests and old installations.
try:  # pragma: no cover - exercised inside AstrBot
    # Current AstrBot releases export ``filter`` from ``astrbot.api.event``.
    from astrbot.api.event import filter as _astr_filter
except Exception:  # pragma: no cover - older releases export it at api level
    try:
        from astrbot.api import filter as _astr_filter
    except Exception:
        _astr_filter = None

if _astr_filter is None:  # pragma: no cover - local development fallback
    class _FallbackEventMessageType:
        ALL = "all"

    class _FilterShim:
        EventMessageType = _FallbackEventMessageType

        @staticmethod
        def command(*_args: Any, **_kwargs: Any):
            return lambda func: func

        @staticmethod
        def event_message_type(*_args: Any, **_kwargs: Any):
            return lambda func: func

    _astr_filter = _FilterShim()

try:  # pragma: no cover - exercised inside AstrBot
    from astrbot.api.event import AstrMessageEvent as _AstrMessageEvent
except Exception:  # pragma: no cover
    class _AstrMessageEvent:  # type: ignore[no-redef]
        pass

try:  # pragma: no cover - current AstrBot exposes the enum on filter
    _EventMessageType = _astr_filter.EventMessageType
except AttributeError:  # pragma: no cover - compatibility with older releases
    try:
        from astrbot.api.event import EventMessageType as _EventMessageType
    except Exception:
        class _EventMessageType:
            ALL = "all"

if not hasattr(_astr_filter, "EventMessageType"):  # pragma: no cover - old AstrBot
    try:
        setattr(_astr_filter, "EventMessageType", _EventMessageType)
    except Exception:
        pass

try:  # pragma: no cover - exercised inside AstrBot
    from astrbot.api.star import Context as _Context
    from astrbot.api.star import Star as _Star
    from astrbot.api.star import register as _register
except Exception:  # pragma: no cover
    class _Context:  # type: ignore[no-redef]
        pass

    class _Star:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    def _register(*_args: Any, **_kwargs: Any):
        return lambda cls: cls

try:  # pragma: no cover - logger is supplied by AstrBot
    from astrbot.api import logger as _astr_logger
except Exception:  # pragma: no cover
    import logging

    _astr_logger = logging.getLogger("name_searcher")

try:  # pragma: no cover - available in AstrBot versions with Plugin Pages
    from astrbot.api.web import (
        error_response as _astr_error_response,
        file_response as _astr_file_response,
        json_response as _astr_json_response,
        request as _astr_web_request,
    )
except Exception:  # pragma: no cover - local tests and older AstrBot versions
    _astr_error_response = None
    _astr_file_response = None
    _astr_json_response = None
    _astr_web_request = None


filter = _astr_filter
AstrMessageEvent = _AstrMessageEvent
EventMessageType = _EventMessageType
Context = _Context
Star = _Star
register = _register


PLUGIN_ID = "astrbot_plugin_name_searcher"
PLUGIN_VERSION = "1.11.5"
PLUGIN_ROOT = Path(__file__).resolve().parent
LEGACY_STORAGE_DIRS = {"data/name_searcher/files", "data/name_searcher/files/"}


def _plugin_data_root() -> Path:
    """Persistent data folder that survives AstrBot plugin updates.

    AstrBot deletes ``data/plugins/<plugin>`` before unpacking an update, so
    runtime files live in ``data/plugin_data/<plugin>`` instead.  Outside an
    AstrBot tree (tests, the local tool) the plugin folder's ``data`` is used.
    """

    if PLUGIN_ROOT.parent.name == "plugins":
        return PLUGIN_ROOT.parent.parent / "plugin_data" / PLUGIN_ID
    return PLUGIN_ROOT / "data"


def _resolve_data_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return _plugin_data_root() / path
PLUGIN_PAGE_ROOT = PLUGIN_ROOT / "pages" / "name-searcher"

DEFAULT_CONFIG: dict[str, Any] = {
    "storage_dir": "files",
    "people_file": "",
    "ocr_lang": "chi_sim+eng",
    "max_file_size_mb": 25,
    "review_threshold": 0.55,
    "request_review_on_low_confidence": True,
    "cleanup_on_start": False,
    "clear_downloaded_files": False,
    "allow_panel_delete": True,
    "download_timeout_seconds": 20,
    "restrict_chat_scope": True,
    "allowed_group_ids": "",
    "allowed_private_ids": "",
    "admin_only_capture": False,
    "admin_ids": "",
    "merged_forward_results": True,
    "ocr_preprocess": True,
    "ocr_psm_modes": "6,11",
    "ocr_upscale_min_side": 1600,
    "pdf_ocr_max_pages": 8,
    "vision_enabled": True,
    "vision_provider": "",
    "vision_model": "",
    "vision_model_targets": "",
    "vision_review_max_pages": 10,
    "vision_consensus_enabled": True,
    "vision_consensus_prompt": "请综合本地解析和多个视觉模型结果，逐行核对姓名、编号、班级、专业及表格列关系。不得概括、删行或合并不同人员；保留完整可检索原文和表格结构，只输出最终校正文本。",
    "vision_prompt": "请完整识别这份文件页面中的正文、表格和图片文字，逐行保留姓名、编号、班级、专业及所有单元格关系。不得概括、删行或合并不同人员，只输出可检索的完整原文。",
    "vision_timeout_seconds": 60,
    "vision_file_scope": "image_pdf",
    "capture_window_enabled": True,
    "capture_interval_seconds": 30,
    "capture_progress_notify": True,
    "forward_file_timeout_seconds": 15,
    "forward_images_only": True,
    "paused": False,
    "notify_encoding_issues": True,
    "notify_archive_limits": True,
    "archive_extraction_enabled": True,
    "archive_max_depth": 3,
    "archive_max_members": 300,
    "archive_max_total_size_mb": 200,
    "llm_review_enabled": False,
    "llm_review_provider": "",
    "llm_review_model": "",
    "llm_review_targets": "",
    "llm_review_consensus_prompt": "下面是多个模型对同一份文件识别结果的审核与修正。请逐行核对姓名、编号、班级、专业，取多数一致或最可信的写法，不得删行或合并不同人员，只输出最终校正文本。",
    "llm_review_prompt": "请审核下面的文件识别结果。判断是否包含可检索的人名及班级、专业等信息；如有明显错误，请给出修正后的简洁文本。只输出审核结论和修正文本。\n\n文件名：{name}\n识别结果：{text}",
    "llm_review_timeout_seconds": 60,
    "panel_mount_path": "/api/name-searcher",
    "panel_public_url": "",
    "panel_access_token": "",
    "name_match_threshold": 0.85,
    "min_free_memory_mb": 200,
    "import_max_bundle_mb": 2048,
    "import_model_review": True,
    "portal_enabled": False,
    "portal_host": "127.0.0.1",
    "portal_port": 6197,
    "portal_public_url": "",
    "portal_allowed_origins": "https://20610728.xyz",
    "portal_allowed_groups": "",
    "portal_allowed_users": "",
    "portal_allow_private": True,
    "portal_session_minutes": 30,
    "portal_session_max_finds": 2,
}

# Keys that only the AstrBot plugin config (admin) may change.  The plugin
# panel can never rewrite them, so a panel session cannot point the people
# library at an arbitrary server file, move storage, or change panel auth.
PANEL_LOCKED_KEYS = frozenset({
    "storage_dir",
    "people_file",
    "panel_mount_path",
    "panel_public_url",
    "panel_access_token",
    "portal_host",
    "portal_port",
})
# Never echoed back to any panel client.
PANEL_SECRET_KEYS = frozenset({"panel_access_token"})

_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")


IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".ico", ".heic", ".heif", ".avif",
}
TEXT_EXTENSIONS = {
    ".txt", ".csv", ".tsv", ".log", ".json", ".jsonl", ".md", ".markdown",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".rtf",
}
SPREADSHEET_EXTENSIONS = {
    ".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".xlsb", ".ods", ".fods",
    ".csv", ".tsv",
}
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".docm", ".dotx", ".odt", ".ott", ".pages", ".epub",
}
PRESENTATION_EXTENSIONS = {".ppt", ".pptx", ".pptm", ".ppsx", ".odp", ".key"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".tgz", ".tbz", ".tbz2", ".txz", ".gz", ".bz2", ".xz"}
CAPTURABLE_EXTENSIONS = (
    IMAGE_EXTENSIONS
    | TEXT_EXTENSIONS
    | SPREADSHEET_EXTENSIONS
    | DOCUMENT_EXTENSIONS
    | PRESENTATION_EXTENSIONS
    | ARCHIVE_EXTENSIONS
)

MIME_OVERRIDES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
}

_RAPID_OCR_ENGINE: Any = None
_RAPID_OCR_LOCK = threading.Lock()


def _log(level: str, message: str, *args: Any) -> None:
    """Log through whichever logger an AstrBot release exposes."""

    method = getattr(_astr_logger, level, None)
    if callable(method):
        try:
            method(message, *args)
        except TypeError:
            method(message % args if args else message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(value: str, fallback: str = "attachment") -> str:
    value = str(value or "").strip().replace("\\", "/").split("/")[-1]
    value = re.sub(r"[^\w.\-\u3400-\u9fff ]+", "_", value).strip(" .")
    return value[:160] or fallback


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes:{len(value)}>"
    return str(value)


def _normalise_name(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).casefold()


def _visible_model_text(value: Any) -> str:
    """Normalise model display escapes without altering persisted evidence."""

    text = html.unescape(unicodedata.normalize("NFKC", str(value or "")))
    text = re.sub(
        r"\\[uU]([0-9a-fA-F]{4,8})",
        lambda match: chr(int(match.group(1), 16))
        if int(match.group(1), 16) <= 0x10FFFF
        else match.group(0),
        text,
    )
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)


def _search_key(value: Any) -> str:
    """Build a punctuation-insensitive key for noisy OCR/model text."""

    return "".join(char.casefold() for char in _visible_model_text(value) if char.isalnum())


def _artifact_search_sections(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return every retained extraction result that may contain a person."""

    sections: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        sections.append((label, text))

    add("最终识别文本", record.get("ocr_text"))
    add("视觉共识", record.get("vision_consensus_text"))
    add("LLM 审核", record.get("llm_review_text"))
    add("LLM 审核前文本", record.get("ocr_text_before_llm_review"))
    add("本地解析", record.get("ocr_text_local"))
    for result in record.get("vision_results") or []:
        if not isinstance(result, Mapping):
            continue
        label = str(result.get("label") or "").strip()
        if not label:
            provider = str(result.get("provider") or "").strip()
            model = str(result.get("model") or "").strip()
            label = "::".join(part for part in (provider, model) if part)
        add(f"视觉模型 {label or '未命名结果'}", result.get("text"))
    for result in record.get("llm_review_results") or []:
        if isinstance(result, Mapping):
            add(f"LLM 审核 {result.get('label') or '未命名模型'}", result.get("text"))
    # Compatibility with records created by older or third-party revisions.
    for key, label in (
        ("vision_text", "视觉模型结果"),
        ("recognized_text", "识别文本"),
        ("extracted_text", "提取文本"),
    ):
        add(label, record.get(key))
    return sections


def _name_pattern(query: str) -> Optional[re.Pattern[str]]:
    """Regex that finds ``query`` in visible text, tolerating OCR/model noise
    (spaces, middle dots, punctuation) between characters."""

    chars = [char for char in _visible_model_text(query) if char.isalnum()]
    if not chars:
        return None
    gap = r"[^0-9A-Za-z\u3400-\u9fff\uf900-\ufaff]{0,2}"
    return re.compile(gap.join(re.escape(char) for char in chars), re.IGNORECASE)


def _is_name_char(char: str, query: str) -> bool:
    """Whether a neighbouring character would extend the name into another word."""

    if not char:
        return False
    if _CJK_CHAR.match(char):
        return bool(_CJK_CHAR.search(query))
    if char.isalpha():
        # Latin names only extend through letters.
        return not _CJK_CHAR.search(query)
    return False


def _name_match_strength(text: str, query: str) -> str:
    """Return "exact" when ``query`` appears as a whole name in ``text``,
    "partial" when it only appears inside a longer name (张三 in 张三丰),
    and "" when absent."""

    visible = _visible_model_text(text)
    pattern = _name_pattern(query)
    if pattern is None:
        return ""
    found = ""
    for match in pattern.finditer(visible):
        before = visible[match.start() - 1] if match.start() > 0 else ""
        after = visible[match.end()] if match.end() < len(visible) else ""
        prefix = visible[max(0, match.start() - 4):match.start()]
        labelled = bool(re.search(r"(姓名|名字|名称|学生|同学|name)$", prefix, re.IGNORECASE))
        if (labelled or not _is_name_char(before, query)) and not _is_name_char(after, query):
            return "exact"
        found = "partial"
    if found:
        return found
    # Fall back to the punctuation-free key for heavily mangled model output.
    query_key = _search_key(query)
    return "partial" if query_key and query_key in _search_key(text) else ""


_WILDCARD_MAP = str.maketrans({"＊": "*", "？": "?", "﹡": "*", "×": "*"})
_CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff"
_NAME_LABEL_LOOKBEHIND = r"(?<=姓名)|(?<=名字)|(?<=学生)|(?<=同学)"
WILDCARD_MAX_NAMES = 200


def _normalise_wildcard(query: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(query or "").translate(_WILDCARD_MAP))


def _has_wildcard(query: Any) -> bool:
    text = _normalise_wildcard(query)
    return "*" in text or "?" in text


def _wildcard_pattern(query: Any) -> Optional[re.Pattern[str]]:
    """Compile a name wildcard: ``*`` = one or more name characters, ``?`` = exactly one.

    Chinese names only grow through CJK characters (``张*`` → 张三 / 张三丰, at most
    four characters in total unless the literal part is longer); Latin names grow
    through letters.  A match must start and end on a name boundary so 主张 / 张贴
    inside longer words are not reported.  Returns None without a literal part.
    """

    text = _normalise_wildcard(query)
    literal = [char for char in text if char not in "*?"]
    if not literal or not all(char.isalnum() or char in "·." for char in literal):
        return None
    cjk = any(_CJK_CHAR.match(char) for char in literal)
    name_char = f"[{_CJK_CLASS}]" if cjk else "[A-Za-z]"
    parts: list[str] = []
    for char in text:
        if char == "*":
            parts.append(f"{name_char}{{1,3}}" if cjk else f"{name_char}+")
        elif char == "?":
            parts.append(name_char)
        else:
            parts.append(re.escape(char))
    core = "".join(parts)
    if cjk:
        pattern = f"(?:(?<![{_CJK_CLASS}])|{_NAME_LABEL_LOOKBEHIND})({core})(?![{_CJK_CLASS}])"
    else:
        pattern = f"(?<![A-Za-z])({core})(?![A-Za-z])"
    return re.compile(pattern, re.IGNORECASE)


def _wildcard_names(text: Any, pattern: re.Pattern[str], query: Any) -> list[tuple[str, str]]:
    """Return (name, line) pairs for every distinct wildcard hit in ``text``."""

    literal_len = len([char for char in _normalise_wildcard(query) if char not in "*?"])
    limit = max(4, literal_len + 2)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in _visible_model_text(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for match in pattern.finditer(stripped):
            name = match.group(1)
            if _CJK_CHAR.match(name[0]) and len(name) > limit:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append((name, stripped[:300]))
    return found


def _wildcard_text_sections(record: Mapping[str, Any]) -> list[str]:
    """Final (reviewed) texts only: raw OCR before review is too noisy to list names from."""

    texts: list[str] = []
    for key in ("ocr_text", "vision_consensus_text", "llm_review_text"):
        value = str(record.get(key) or "").strip()
        if value and value not in texts:
            texts.append(value)
    if not texts:
        texts = [text for _, text in _artifact_search_sections(record)]
    return texts


def _mime_for(path: Path, hint: str = "") -> str:
    return hint or MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


_MAGIC_TYPES: tuple[tuple[bytes, int, str, str], ...] = (
    (b"\xff\xd8\xff", 0, ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, ".png", "image/png"),
    (b"GIF87a", 0, ".gif", "image/gif"),
    (b"GIF89a", 0, ".gif", "image/gif"),
    (b"BM", 0, ".bmp", "image/bmp"),
    (b"II*\x00", 0, ".tif", "image/tiff"),
    (b"MM\x00*", 0, ".tif", "image/tiff"),
    (b"%PDF-", 0, ".pdf", "application/pdf"),
)
_GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream", "application/unknown"}


def _sniff_content_type(head: bytes) -> Optional[tuple[str, str]]:
    """Return (extension, mime) for common image/PDF payloads by magic bytes."""

    head = bytes(head or b"")[:32]
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if head[4:12] in {b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"}:
        return (".avif", "image/avif") if head[8:12] == b"avif" else (".heic", "image/heic")
    for magic, offset, ext, mime in _MAGIC_TYPES:
        if head[offset:offset + len(magic)] == magic:
            return ext, mime
    return None


def _fix_type_by_content(name: str, kind: str, mime: str, head: bytes) -> tuple[str, str, str]:
    """Give extension-less downloads (QQ forward images are often named
    "download") a real extension, MIME and kind from their magic bytes."""

    sniffed = _sniff_content_type(head)
    if not sniffed:
        return name, kind, mime
    ext, sniffed_mime = sniffed
    suffix = Path(name or "").suffix.lower()
    known = suffix in IMAGE_EXTENSIONS or suffix in MIME_OVERRIDES or bool(mimetypes.guess_type(f"x{suffix}")[0]) if suffix else False
    if not known:
        name = f"{name or 'file'}{ext}"
    if str(mime or "").strip().lower() in _GENERIC_MIMES or (sniffed_mime.startswith("image/") and not str(mime).lower().startswith("image/") and not known):
        mime = sniffed_mime
    if kind in {"", "file"} or (sniffed_mime.startswith("image/") and kind != "image" and not known):
        kind = _kind_for(name, mime)
    return name, kind, mime


def _repair_record_type(record: dict[str, Any], root: Path) -> bool:
    """Fix stored records saved before content sniffing; True if changed."""

    if record.get("format") not in {None, "", "unknown"} or record.get("download_status") != "ok":
        return False
    stored = str(record.get("path") or "")
    if not stored:
        return False
    try:
        with (root / stored).open("rb") as handle:
            head = handle.read(32)
    except OSError:
        return False
    name, kind, mime = _fix_type_by_content(str(record.get("name") or ""), str(record.get("kind") or ""), str(record.get("mime") or ""), head)
    if name == record.get("name"):
        return False
    record.update({"name": name, "kind": kind, "mime": mime, "format": Path(name).suffix.lower().lstrip(".") or "unknown"})
    # Vision skipped these because they did not look like images; let the
    # next capture or 补跑模型识别 review them again.
    if record.get("vision_status") == "no_review_pages":
        record.pop("vision_status", None)
    return True


def _kind_for(name: str, mime: str = "", type_hint: str = "") -> str:
    text = f"{name} {mime} {type_hint}".lower()
    suffix = Path(name).suffix.lower()
    if "image" in text or suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in SPREADSHEET_EXTENSIONS or "spreadsheet" in text or "excel" in text:
        return "spreadsheet"
    if suffix in PRESENTATION_EXTENSIONS or "presentation" in text or "powerpoint" in text:
        return "presentation"
    if suffix in ARCHIVE_EXTENSIONS or "archive" in text or "compressed" in text:
        return "archive"
    if "pdf" in text or suffix in DOCUMENT_EXTENSIONS or suffix in TEXT_EXTENSIONS:
        return "document"
    return "file"


@dataclass
class AttachmentCandidate:
    name: str
    kind: str
    mime: str
    value: Any
    source: dict[str, Any]
    type_hint: str = ""


@dataclass
class ArchiveExpansion:
    members: list[tuple[str, bytes]]
    errors: list[str]
    skipped: int = 0
    depth_limited: int = 0


class PanelUploadEvent:
    """Minimal event facade so panel uploads reuse the normal capture pipeline."""

    def __init__(self) -> None:
        self.sender_id = "plugin-page"
        self.sender_name = "Plugin Page 本地上传"
        self.unified_msg_origin = "plugin-page:local-upload"
        self.sent: list[Any] = []

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return self.sender_name

    def get_message_id(self) -> str:
        return f"upload-{int(time.time())}"

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class ArtifactStore:
    """Thread-safe file store with a JSON index suitable for small deployments."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._records = [item for item in data if isinstance(item, dict)]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._records = []
        repaired = sum(1 for item in self._records if _repair_record_names(item))
        retyped = sum(1 for item in self._records if _repair_record_type(item, self.root))
        if retyped:
            _log("info", "NameSearcher detected file types for %s extension-less files", retyped)
        repaired += retyped
        if repaired:
            try:
                self._save()
                _log("info", "NameSearcher repaired %s garbled archive member names", repaired)
            except OSError:
                pass

    def _save(self) -> None:
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._records, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        temporary.replace(self.index_path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in reversed(self._records)]

    def get(self, artifact_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for item in self._records:
                if str(item.get("id")) == str(artifact_id):
                    return dict(item)
        return None

    def path_for(self, record: Mapping[str, Any]) -> Optional[Path]:
        raw = str(record.get("path") or "")
        if not raw:
            return None
        path = (self.root / raw).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        return path

    def add_bytes(
        self,
        name: str,
        content: bytes,
        *,
        kind: str,
        mime: str,
        source: Mapping[str, Any],
        download_status: str = "ok",
        reason: str = "",
    ) -> dict[str, Any]:
        name, kind, mime = _fix_type_by_content(name, kind, mime, content[:32])
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            for existing in self._records:
                if existing.get("sha256") == digest and existing.get("download_status") == "ok":
                    new_source = dict(source)
                    sources = existing.setdefault("sources", [dict(existing.get("source") or {})])
                    if new_source and new_source not in sources:
                        sources.append(new_source)
                    existing["capture_count"] = int(existing.get("capture_count") or 1) + 1
                    existing["last_captured_at"] = _now()
                    self._save()
                    return dict(existing)
            artifact_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            clean_name = _safe_name(name)
            stored_name = f"{artifact_id}_{clean_name}"
            path = self.root / stored_name
            path.write_bytes(content)
            record = {
                "id": artifact_id,
                "name": clean_name,
                "path": stored_name,
                "kind": kind,
                "mime": mime,
                "format": Path(clean_name).suffix.lower().lstrip(".") or "unknown",
                "size": len(content),
                "sha256": digest,
                "source": dict(source),
                "sources": [dict(source)],
                "capture_count": 1,
                "captured_at": _now(),
                "download_status": download_status,
                "reason": reason,
                "ocr_text": "",
                "ocr_confidence": 0.0,
                "extraction_status": "pending",
                "review_status": "pending" if download_status != "ok" else "unreviewed",
            }
            self._records.append(record)
            self._save()
            return dict(record)

    def add_unavailable(
        self,
        name: str,
        *,
        kind: str,
        mime: str,
        source: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        with self._lock:
            artifact_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            record = {
                "id": artifact_id,
                "name": _safe_name(name),
                "path": "",
                "kind": kind,
                "mime": mime,
                "format": Path(str(name)).suffix.lower().lstrip(".") or "unknown",
                "size": 0,
                "sha256": "",
                "source": dict(source),
                "captured_at": _now(),
                "download_status": "unavailable",
                "reason": reason,
                "ocr_text": "",
                "ocr_confidence": 0.0,
                "extraction_status": "unavailable",
                "review_status": "pending",
            }
            self._records.append(record)
            self._save()
            return dict(record)

    def update(self, artifact_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            for item in self._records:
                if str(item.get("id")) == str(artifact_id):
                    item.update(fields)
                    self._save()
                    return dict(item)
        return None

    def delete(self, artifact_ids: Iterable[str]) -> list[str]:
        ids = {str(item) for item in artifact_ids}
        removed: list[str] = []
        with self._lock:
            retained: list[dict[str, Any]] = []
            for item in self._records:
                if str(item.get("id")) not in ids:
                    retained.append(item)
                    continue
                path = self.path_for(item)
                if path and path.exists():
                    try:
                        path.unlink()
                    except OSError as exc:
                        _log("warning", "Unable to remove %s: %s", path, exc)
                preview = self.root / PREVIEW_DIR_NAME / f"{item.get('id')}.jpg"
                try:
                    preview.unlink(missing_ok=True)
                except OSError:
                    pass
                removed.append(str(item.get("id")))
            self._records = retained
            if removed:
                self._save()
        return removed

    def clear(self) -> int:
        """Remove every tracked artifact and orphaned file in the store.

        The index can be edited or lost independently of the downloaded files.
        A cleanup action therefore also removes files left behind in the store
        directory, while retaining the index itself (rewritten as an empty
        list).  Only direct children are considered; nested user directories
        are never traversed.
        """

        with self._lock:
            ids = [str(item.get("id")) for item in self._records]
        deleted = len(self.delete(ids))
        with self._lock:
            for child in self.root.iterdir():
                if child == self.index_path or not child.is_file():
                    continue
                try:
                    child.unlink()
                    deleted += 1
                except OSError as exc:
                    _log("warning", "Unable to remove orphaned artifact %s: %s", child, exc)
            # ``delete`` only writes when it removed a record.  Persist an
            # empty index even when the directory contained only orphans.
            self._save()
        return deleted

    def save(self) -> None:
        with self._lock:
            self._save()

    def import_record(self, fields: Mapping[str, Any], content: bytes) -> tuple[dict[str, Any], str]:
        """Adopt one already-recognised file from a bundle without saving the index.

        Returns ``(record, outcome)`` where outcome is ``created``, ``updated``
        (an unfinished local record took the bundle's result) or ``exists``.
        Callers batch :meth:`save` so a large import rewrites index.json once.
        """

        digest = hashlib.sha256(content).hexdigest()
        clean = {
            key: value for key, value in fields.items()
            if key not in _IMPORT_DROPPED_FIELDS and isinstance(key, str)
        }
        clean["ocr_text"] = str(clean.get("ocr_text") or "")[:200000]
        clean["processing_status"] = "completed"
        clean["processing_updated_at"] = _now()
        clean["processing_error"] = ""
        with self._lock:
            for existing in self._records:
                if existing.get("sha256") != digest or existing.get("download_status") != "ok":
                    continue
                unfinished = (
                    existing.get("extraction_status") in {None, "", "pending", "failed"}
                    or existing.get("processing_status") in {"queued", "running", "failed"}
                )
                if unfinished:
                    keep = {key: existing.get(key) for key in ("id", "path", "source", "sources", "captured_at", "capture_count")}
                    existing.update(clean)
                    existing.update({key: value for key, value in keep.items() if value is not None})
                    return dict(existing), "updated"
                return dict(existing), "exists"
            artifact_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            clean_name = _safe_name(str(clean.get("name") or "imported"))
            stored_name = f"{artifact_id}_{clean_name}"
            (self.root / stored_name).write_bytes(content)
            source = dict(clean.get("source") or {}) if isinstance(clean.get("source"), Mapping) else {}
            record = dict(clean)
            record.update({
                "id": artifact_id,
                "name": clean_name,
                "path": stored_name,
                "kind": str(clean.get("kind") or _kind_for(clean_name, str(clean.get("mime") or ""))),
                "mime": str(clean.get("mime") or _mime_for(Path(clean_name))),
                "format": Path(clean_name).suffix.lower().lstrip(".") or "unknown",
                "size": len(content),
                "sha256": digest,
                "source": source,
                "sources": [source],
                "capture_count": 1,
                "captured_at": str(clean.get("captured_at") or _now()),
                "download_status": "ok",
            })
            self._records.append(record)
            return dict(record), "created"

    def remap_archive_ids(self, mapping: Mapping[str, str], record_ids: Iterable[str]) -> None:
        wanted = {str(item) for item in record_ids}
        with self._lock:
            for item in self._records:
                if str(item.get("id")) not in wanted:
                    continue
                source = item.get("source")
                if isinstance(source, dict) and str(source.get("archive_id") or "") in mapping:
                    source["archive_id"] = mapping[str(source["archive_id"])]
                    item["sources"] = [dict(source)]

    def clear_failed(self) -> list[str]:
        """Remove records whose attachment could not be downloaded."""

        with self._lock:
            ids = [
                str(item.get("id"))
                for item in self._records
                if item.get("download_status") in {"unavailable", "failed"}
            ]
        return self.delete(ids)


BUNDLE_FORMAT = "name-searcher-bundle"
BUNDLE_VERSION = 1
_IMPORT_DROPPED_FIELDS = frozenset({
    "id", "path", "bundle_file", "upload_progress_id", "processing_status",
    "processing_updated_at", "processing_error", "processing_attempts", "sources",
    "capture_count", "sha256", "size", "download_status",
})
_IMPORT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_sha256(value: Any) -> str:
    """Accept a bare hex digest or a sha256sum line ("<hex>  name")."""

    text = str(value or "").strip().lower()
    match = re.search(r"\b([0-9a-f]{64})\b", text)
    return match.group(1) if match else ""


def _sha256_sidecar(bundle: Path) -> Optional[Path]:
    for candidate in (bundle.with_name(bundle.name + ".sha256"), bundle.with_suffix(".sha256")):
        if candidate.is_file():
            return candidate
    return None


def _verify_bundle_sha256(bundle: Path, expected: Any) -> str:
    """Check the whole bundle before anything is read from it. Returns the digest."""

    wanted = _parse_sha256(expected)
    if not wanted:
        raise ValueError("缺少 SHA256 识别码：请同时选择本地工具生成的 .sha256 文件，或粘贴识别码")
    actual = _file_sha256(bundle)
    if actual != wanted:
        raise ValueError(
            f"SHA256 校验失败，识别包可能在传输中损坏或不是同一个文件（应为 {wanted[:12]}…，实际 {actual[:12]}…），已放弃导入"
        )
    return actual


def _import_bundle(store: "ArtifactStore", bundle_path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Import a bundle produced by name_searcher_local.py.

    The archive is read member by member from disk, every file is checked
    against the sha256 recorded by the local tool, and index.json is written
    once at the end.  No OCR, rendering or model call happens on the server.
    """

    max_bytes = int(float(config.get("max_file_size_mb", 25) or 25) * 1024 * 1024)
    summary: dict[str, Any] = {"created": 0, "updated": 0, "exists": 0, "skipped": 0, "errors": []}
    try:
        archive = zipfile.ZipFile(bundle_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"不是有效的识别包：{exc}") from exc
    with archive:
        try:
            info = archive.getinfo("manifest.json")
        except KeyError as exc:
            raise ValueError("识别包缺少 manifest.json") from exc
        if info.file_size > 256 * 1024 * 1024:
            raise ValueError("manifest.json 过大")
        try:
            manifest = json.loads(archive.read(info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"manifest.json 无法解析：{exc}") from exc
        if not isinstance(manifest, Mapping) or manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError("不是 NameSearcher 识别包")
        if int(manifest.get("version") or 0) > BUNDLE_VERSION:
            raise ValueError("识别包版本比插件新，请先升级插件")
        records = manifest.get("records")
        if not isinstance(records, list):
            raise ValueError("识别包没有文件记录")
        names = set(archive.namelist())
        id_map: dict[str, str] = {}
        touched: list[str] = []
        bundle_label = str(config.get("_label") or manifest.get("label") or bundle_path.name)[:200]
        for index, fields in enumerate(records):
            if not isinstance(fields, Mapping):
                summary["skipped"] += 1
                continue
            label = str(fields.get("name") or f"#{index + 1}")
            member = str(fields.get("bundle_file") or "")
            if fields.get("download_status") != "ok" or not member:
                summary["skipped"] += 1
                continue
            if not member.startswith("files/") or ".." in member.split("/") or member not in names:
                summary["errors"].append(f"{label}：识别包内文件缺失")
                continue
            member_info = archive.getinfo(member)
            if member_info.file_size > max_bytes:
                summary["errors"].append(f"{label}：超过单文件大小上限")
                continue
            content = archive.read(member_info)
            expected = str(fields.get("sha256") or "")
            if expected and hashlib.sha256(content).hexdigest() != expected:
                summary["errors"].append(f"{label}：校验失败，文件可能已损坏")
                continue
            source = dict(fields.get("source") or {}) if isinstance(fields.get("source"), Mapping) else {}
            source.setdefault("upload_source", "local-tool")
            source["import_bundle"] = bundle_label
            adopted = dict(fields)
            adopted["source"] = source
            _repair_record_names(adopted)
            record, outcome = store.import_record(adopted, content)
            summary[outcome] += 1
            if isinstance(fields.get("archive_errors"), list) and fields.get("archive_errors"):
                summary.setdefault("archive_limits", []).append({
                    "name": str(record.get("name") or label),
                    "count": _archive_limited_count(fields.get("archive_errors")),
                })
            old_id = str(fields.get("id") or "")
            if old_id:
                id_map[old_id] = str(record.get("id"))
            if outcome != "exists":
                touched.append(str(record.get("id")))
            if len(touched) and len(touched) % 200 == 0:
                store.save()
        store.remap_archive_ids(id_map, touched)
        store.save()
    summary["record_ids"] = touched
    summary["total"] = len(records)
    summary["ok"] = True
    return summary


def _available_memory_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _record_source(record: Mapping[str, Any]) -> str:
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    sender = source.get("sender_name") or source.get("sender_id") or "未知发起人"
    message_id = source.get("message_id") or source.get("unified_msg_origin") or "未知消息"
    captured = record.get("captured_at") or "未知时间"
    return f"{record.get('name') or '未命名文件'}（文件编号 {record.get('id')}，发起人 {sender}，消息 {message_id}，捕获时间 {captured}）"


_MOJIBAKE_MARKERS = (
    "�", "锟斤拷", "烫烫烫", "屯屯屯", "Ã", "Â", "â€", "ðŸ",
    "ä¸", "æ–", "å­", "çš", "æœ", "ï¼", "銆", "鈥",
)


def _text_quality(text: str) -> float:
    if not text:
        return -1000.0
    sample = text[:200000]
    controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in sample)
    private_use = sum(0xE000 <= ord(char) <= 0xF8FF for char in sample)
    replacements = sample.count("�")
    markers = sum(sample.count(marker) for marker in _MOJIBAKE_MARKERS)
    chinese = sum("\u3400" <= char <= "\u9fff" for char in sample)
    readable = sum(char.isprintable() or char in "\n\r\t" for char in sample)
    return readable / max(1, len(sample)) * 20 + min(chinese, 200) * 0.08 - controls * 8 - private_use * 12 - replacements * 25 - markers * 7


def _repair_mojibake(text: str) -> tuple[str, bool, str]:
    """Reverse common UTF-8/GBK mojibake without changing healthy text."""

    if not text:
        return text, False, ""
    best = text
    best_score = _text_quality(text)
    best_route = ""
    for source_encoding in ("latin-1", "cp1252", "gb18030", "big5"):
        try:
            raw = text.encode(source_encoding)
        except (UnicodeEncodeError, LookupError):
            continue
        for target_encoding in ("utf-8", "gb18030", "big5"):
            if source_encoding == target_encoding:
                continue
            try:
                candidate = raw.decode(target_encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            score = _text_quality(candidate)
            if score > best_score + 3:
                best, best_score = candidate, score
                best_route = f"{source_encoding}->{target_encoding}"
    return best, best != text, best_route


def _decode_text_with_info(raw: bytes) -> tuple[str, str, bool, str]:
    """Decode Chinese-oriented text and report whether transcoding was needed."""

    if not raw:
        return "", "utf-8", False, ""
    bom_encodings = (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    )
    candidates: list[tuple[float, int, str, str]] = []
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            try:
                text = raw.decode(encoding)
                repaired, changed, route = _repair_mojibake(text)
                return repaired, encoding, encoding != "utf-8-sig" or changed, route
            except UnicodeDecodeError:
                break
    try:
        decoded_utf8 = raw.decode("utf-8")
        repaired_utf8, changed_utf8, route_utf8 = _repair_mojibake(decoded_utf8)
        return repaired_utf8, "utf-8", changed_utf8, route_utf8
    except UnicodeDecodeError:
        pass
    for priority, encoding in enumerate(("utf-8", "gb18030", "big5", "utf-16-le", "utf-16-be")):
        try:
            decoded = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        repaired, changed, route = _repair_mojibake(decoded)
        score = _text_quality(repaired)
        if encoding.startswith("utf-16") and "\x00" in decoded:
            score -= 20
        candidates.append((score, -priority, repaired, f"{encoding}|{route}" if changed else encoding))
    if not candidates:
        decoded = raw.decode("gb18030", errors="replace")
        repaired, changed, route = _repair_mojibake(decoded)
        return repaired, "gb18030-replace", True, route
    _score, _priority, text, encoding_info = max(candidates, key=lambda item: (item[0], item[1]))
    source_encoding, _, repair_route = encoding_info.partition("|")
    converted = source_encoding not in {"utf-8", "utf-8-sig"} or bool(repair_route)
    return text, source_encoding, converted, repair_route


def _decode_text(raw: bytes) -> str:
    return _decode_text_with_info(raw)[0]


class _TextOnlyHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _word_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "t" and node.text:
            parts.append(node.text)
        elif local == "tab":
            parts.append("\t")
        elif local in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _word_table_lines(table: ET.Element, table_number: int, prefix: str = "") -> list[str]:
    lines = [f"{prefix}[表格 {table_number}]"]
    row_number = 0
    for row in table.findall(f"{_WORD_NS}tr"):
        row_number += 1
        values: list[str] = []
        nested_lines: list[str] = []
        for cell_number, cell in enumerate(row.findall(f"{_WORD_NS}tc"), 1):
            cell_parts: list[str] = []
            properties = cell.find(f"{_WORD_NS}tcPr")
            span = 1
            vertical_merge = ""
            if properties is not None:
                grid_span = properties.find(f"{_WORD_NS}gridSpan")
                if grid_span is not None:
                    try:
                        span = max(1, int(grid_span.get(f"{_WORD_NS}val") or "1"))
                    except ValueError:
                        span = 1
                merge = properties.find(f"{_WORD_NS}vMerge")
                if merge is not None:
                    vertical_merge = merge.get(f"{_WORD_NS}val") or "continue"
            for child in cell:
                local = child.tag.rsplit("}", 1)[-1]
                if local == "p":
                    text = _word_text(child)
                    if text:
                        cell_parts.append(text)
                elif local == "tbl":
                    nested = _word_table_lines(child, cell_number, prefix=f"{prefix}  ")
                    nested_lines.extend(nested)
                    nested_text = " / ".join(line for line in nested[1:] if " | " in line)
                    if nested_text:
                        cell_parts.append(nested_text)
            value = " / ".join(cell_parts).strip()
            if span > 1:
                value = f"{value} [跨 {span} 列]".strip()
            if vertical_merge:
                merge_label = "纵向合并起始" if vertical_merge == "restart" else "纵向合并延续"
                value = f"{value} [{merge_label}]".strip()
            values.append(value)
        if values:
            lines.append(f"{prefix}第 {row_number} 行 | " + " | ".join(values))
        lines.extend(nested_lines)
    return lines


def _extract_docx_text(path: Path) -> str:
    sections = (
        ("word/document.xml", "正文"),
        ("word/footnotes.xml", "脚注"),
        ("word/endnotes.xml", "尾注"),
        ("word/comments.xml", "批注"),
    )
    lines: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        dynamic_sections = [
            (name, "页眉" if "/header" in name else "页脚")
            for name in sorted(names)
            if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
        ]
        for member, label in (*sections, *dynamic_sections):
            if member not in names:
                continue
            try:
                root = ET.fromstring(archive.read(member))
            except ET.ParseError:
                continue
            container = root.find(f"{_WORD_NS}body")
            if container is None:
                container = root
            section_lines: list[str] = []
            table_number = 0
            for child in container:
                local = child.tag.rsplit("}", 1)[-1]
                if local == "p":
                    text = _word_text(child)
                    if text:
                        section_lines.append(text)
                elif local == "tbl":
                    table_number += 1
                    section_lines.extend(_word_table_lines(child, table_number))
                elif local == "sdt":
                    text = _word_text(child)
                    if text:
                        section_lines.append(text)
            if section_lines:
                lines.append(f"[{label}]")
                lines.extend(section_lines)
    return "\n".join(lines)


def _extract_xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook  # type: ignore

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    formula_workbook = load_workbook(str(path), read_only=True, data_only=False)
    lines: list[str] = []
    for sheet_index, sheet in enumerate(workbook.worksheets):
        formula_sheet = formula_workbook.worksheets[sheet_index]
        lines.append(f"[工作表 {sheet_index + 1}/{len(workbook.worksheets)}：{sheet.title}]")
        for value_row, formula_row in zip(sheet.iter_rows(values_only=True), formula_sheet.iter_rows(values_only=True)):
            values = []
            for value, formula in zip(value_row, formula_row):
                selected = formula if value is None and formula is not None else value
                values.append("" if selected is None else str(selected).strip())
            while values and not values[-1]:
                values.pop()
            if values:
                lines.append(" | ".join(values))
    workbook.close()
    formula_workbook.close()
    return "\n".join(lines)


def _extract_xls_text(path: Path) -> str:
    import xlrd  # type: ignore

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    lines: list[str] = []
    sheets = workbook.sheets()
    for sheet_index, sheet in enumerate(sheets, 1):
        lines.append(f"[工作表 {sheet_index}/{len(sheets)}：{sheet.name}]")
        for row_index in range(sheet.nrows):
            values = [str(value).strip() for value in sheet.row_values(row_index)]
            while values and not values[-1]:
                values.pop()
            if values:
                lines.append(" | ".join(values))
    workbook.release_resources()
    return "\n".join(lines)


def _extract_xlsb_text(path: Path) -> str:
    from pyxlsb import open_workbook  # type: ignore

    lines: list[str] = []
    with open_workbook(str(path)) as workbook:
        for sheet_index, sheet_name in enumerate(workbook.sheets, 1):
            lines.append(f"[工作表 {sheet_index}/{len(workbook.sheets)}：{sheet_name}]")
            with workbook.get_sheet(sheet_name) as sheet:
                for row in sheet.rows():
                    values = ["" if cell.v is None else str(cell.v).strip() for cell in row]
                    while values and not values[-1]:
                        values.pop()
                    if values:
                        lines.append(" | ".join(values))
    return "\n".join(lines)


def _extract_pptx_text(path: Path) -> str:
    from pptx import Presentation  # type: ignore

    presentation = Presentation(str(path))
    lines: list[str] = []
    for slide_number, slide in enumerate(presentation.slides, 1):
        lines.append(f"幻灯片：{slide_number}")
        for shape in slide.shapes:
            text = str(getattr(shape, "text", "") or "").strip()
            if text:
                lines.append(text)
            table = getattr(shape, "table", None) if getattr(shape, "has_table", False) else None
            if table is not None:
                for row in table.rows:
                    values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if values:
                        lines.append(" | ".join(values))
    return "\n".join(lines)


def _extract_odf_text(path: Path) -> str:
    from odf import teletype  # type: ignore
    from odf.opendocument import load  # type: ignore
    from odf.table import Table, TableCell, TableRow  # type: ignore
    from odf.text import H, P  # type: ignore

    document = load(str(path))
    lines: list[str] = []
    sheets = document.getElementsByType(Table)
    for sheet_index, sheet in enumerate(sheets, 1):
        sheet_name = sheet.getAttribute("name") or f"Sheet{sheet_index}"
        lines.append(f"[工作表 {sheet_index}/{len(sheets)}：{sheet_name}]")
        for row in sheet.getElementsByType(TableRow):
            values: list[str] = []
            for cell in row.childNodes:
                if getattr(cell, "tagName", "") not in {"table:table-cell", "table:covered-table-cell"}:
                    continue
                value = teletype.extractText(cell).strip()
                repeated = min(1000, int(cell.getAttribute("numbercolumnsrepeated") or 1))
                values.extend([value] * repeated)
            while values and not values[-1]:
                values.pop()
            if values:
                lines.append(" | ".join(values))
    if sheets:
        return "\n".join(lines)
    for element_type in (H, P):
        for element in document.getElementsByType(element_type):
            text = teletype.extractText(element).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


def _extract_epub_text(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            suffix = Path(member.filename).suffix.lower()
            if suffix not in {".html", ".htm", ".xhtml"} or member.file_size > 5 * 1024 * 1024:
                continue
            parser = _TextOnlyHTMLParser()
            parser.feed(_decode_text(archive.read(member)))
            parts.extend(parser.parts)
    return "\n".join(parts)


def _ocr_text_from_data(data: Mapping[str, Any]) -> tuple[str, float, int]:
    lines: dict[tuple[Any, ...], list[tuple[int, str]]] = {}
    confidences: list[float] = []
    texts = data.get("text", [])
    for index, raw_word in enumerate(texts):
        word = str(raw_word or "").strip()
        if not word:
            continue
        key = tuple(
            (data.get(field, [0] * len(texts))[index] if index < len(data.get(field, [])) else 0)
            for field in ("page_num", "block_num", "par_num", "line_num")
        )
        left_values = data.get("left", [])
        left = int(left_values[index]) if index < len(left_values) else index
        lines.setdefault(key, []).append((left, word))
        confidence_values = data.get("conf", [])
        try:
            confidence = float(confidence_values[index])
            if confidence >= 0:
                confidences.append(confidence / 100)
        except (IndexError, TypeError, ValueError):
            pass
    text = "\n".join(" ".join(word for _left, word in sorted(words)) for words in lines.values())
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return text, confidence, len(confidences)


def _image_clarity(image: Any) -> float:
    from PIL import ImageFilter, ImageOps, ImageStat  # type: ignore

    sample = ImageOps.grayscale(image.copy())
    sample.thumbnail((1400, 1400))
    contrast = min(1.0, (ImageStat.Stat(sample).var[0] ** 0.5) / 64.0)
    edges = sample.filter(ImageFilter.FIND_EDGES)
    edge_strength = min(1.0, (ImageStat.Stat(edges).var[0] ** 0.5) / 36.0)
    return round(0.25 * contrast + 0.75 * edge_strength, 4)


def _rapid_result_to_data(result: Any) -> dict[str, list[Any]]:
    detections: list[tuple[int, int, str, float, int, int]] = []
    items: list[Any] = []
    if result is not None:
        if isinstance(result, tuple) and len(result) >= 1 and (hasattr(result[0], "boxes") or isinstance(result[0], list)):
            result = result[0]
        if hasattr(result, "boxes") and hasattr(result, "txts") and hasattr(result, "scores"):
            boxes = result.boxes if result.boxes is not None else []
            txts = result.txts if result.txts is not None else []
            scores = result.scores if result.scores is not None else []
            items = list(zip(boxes, txts, scores))
        elif isinstance(result, (list, tuple)):
            items = list(result)

    for item in items:
        try:
            box, text, score = item[0], str(item[1]).strip(), float(item[2])
            if not text:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            left, top = int(min(xs)), int(min(ys))
            width, height = max(1, int(max(xs) - min(xs))), max(1, int(max(ys) - min(ys)))
            detections.append((top, left, text, score, width, height))
        except (IndexError, TypeError, ValueError):
            continue
    detections.sort(key=lambda item: (item[0] + item[5] / 2, item[1]))
    lines: list[dict[str, Any]] = []
    for detection in detections:
        top, _left, _text, _score, _width, height = detection
        center = top + height / 2
        closest: Optional[dict[str, Any]] = None
        closest_distance = float("inf")
        for line in lines:
            distance = abs(center - float(line["center"]))
            tolerance = max(8.0, 0.55 * max(height, float(line["height"])))
            if distance <= tolerance and distance < closest_distance:
                closest = line
                closest_distance = distance
        if closest is None:
            lines.append({"center": center, "height": float(height), "items": [detection]})
            continue
        closest["items"].append(detection)
        count = len(closest["items"])
        closest["center"] = (float(closest["center"]) * (count - 1) + center) / count
        closest["height"] = (float(closest["height"]) * (count - 1) + height) / count
    lines.sort(key=lambda line: float(line["center"]))
    data: dict[str, list[Any]] = {
        key: [] for key in (
            "text", "conf", "left", "top", "width", "height",
            "page_num", "block_num", "par_num", "line_num",
        )
    }
    for line_number, line in enumerate(lines, 1):
        for top, left, recognized_text, score, width, height in sorted(line["items"], key=lambda item: item[1]):
            data["text"].append(recognized_text)
            data["conf"].append(score * 100)
            data["left"].append(left)
            data["top"].append(top)
            data["width"].append(width)
            data["height"].append(height)
            data["page_num"].append(1)
            data["block_num"].append(1)
            data["par_num"].append(1)
            data["line_num"].append(line_number)
    return data


def _run_rapid_ocr(image: Any) -> dict[str, list[Any]]:
    global _RAPID_OCR_ENGINE

    if _RAPID_OCR_ENGINE is False:
        raise RuntimeError("RapidOCR is unavailable")
    with _RAPID_OCR_LOCK:
        if _RAPID_OCR_ENGINE is None:
            try:
                try:
                    from rapidocr import RapidOCR  # type: ignore
                except ImportError:
                    from rapidocr_onnxruntime import RapidOCR  # type: ignore

                try:
                    # Keep ONNX to one worker thread so a 1-core server stays responsive.
                    _RAPID_OCR_ENGINE = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
                except TypeError:
                    _RAPID_OCR_ENGINE = RapidOCR()
            except Exception:
                _RAPID_OCR_ENGINE = False
                raise
        import numpy as np  # type: ignore

        output = _RAPID_OCR_ENGINE(np.asarray(image.convert("RGB")))
    result = output[0] if isinstance(output, tuple) else output
    return _rapid_result_to_data(result)


def _recognize_image(source: Any, ocr_lang: str, options: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    from PIL import Image, ImageFilter, ImageOps, ImageStat  # type: ignore

    options = options or {}
    try:
        from pillow_heif import register_heif_opener  # type: ignore

        register_heif_opener()
    except Exception:
        pass

    opened = Image.open(source) if isinstance(source, (str, Path)) else source.copy()
    image = ImageOps.exif_transpose(opened)
    if image.mode in {"RGBA", "LA"}:
        canvas = Image.new("RGB", image.size, "white")
        alpha = image.getchannel("A")
        canvas.paste(image.convert("RGB"), mask=alpha)
        image = canvas
    else:
        image = image.convert("RGB")

    width, height = image.size
    clarity = _image_clarity(image)
    try:
        import pytesseract  # type: ignore

        orientation = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
        rotation = int(orientation.get("rotate") or 0)
        if rotation:
            image = image.rotate(rotation, expand=True, fillcolor="white")
    except Exception:
        pass

    preprocess = _as_bool(options.get("ocr_preprocess", True))
    gray = ImageOps.autocontrast(ImageOps.grayscale(image))
    if preprocess:
        min_side = max(400, int(options.get("ocr_upscale_min_side", 1600) or 1600))
        shortest = max(1, min(gray.size))
        scale = min(3.0, max(1.0, min_side / shortest))
        max_scale = (24_000_000 / max(1, gray.width * gray.height)) ** 0.5
        scale = min(scale, max(1.0, max_scale))
        if scale > 1.05:
            gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.Resampling.LANCZOS)
        enhanced = gray.filter(ImageFilter.UnsharpMask(radius=1.4, percent=180, threshold=3))
        threshold = int(sum(ImageStat.Stat(gray).mean) / len(ImageStat.Stat(gray).mean))
        binary = gray.point(lambda pixel: 255 if pixel > threshold else 0)
    else:
        enhanced = gray
        binary = gray

    modes = []
    for token in re.split(r"[,，;；\s]+", str(options.get("ocr_psm_modes", "6,11"))):
        try:
            mode = int(token)
        except ValueError:
            continue
        if mode in {3, 4, 6, 7, 11, 12, 13} and mode not in modes:
            modes.append(mode)
    modes = modes[:2] or [6, 11]
    jobs = [(enhanced, modes[0]), (binary, modes[0])]
    if len(modes) > 1:
        jobs.append((enhanced, modes[1]))

    best: dict[str, Any] = {
        "text": "",
        "confidence": 0.0,
        "raw_confidence": 0.0,
        "word_count": 0,
        "data": {},
        "psm": None,
        "engine": "unavailable",
    }

    def consider(data: Mapping[str, Any], engine: str, mode: Optional[int] = None) -> None:
        nonlocal best
        text, raw_confidence, word_count = _ocr_text_from_data(data)
        score = raw_confidence + min(word_count, 50) / 1000
        current_score = float(best.get("raw_confidence", 0)) + min(int(best.get("word_count", 0)), 50) / 1000
        if text and score > current_score:
            adjusted = raw_confidence * (0.7 + 0.3 * clarity)
            best = {
                "text": text,
                "confidence": round(adjusted, 4),
                "raw_confidence": round(raw_confidence, 4),
                "word_count": word_count,
                "data": data,
                "psm": mode,
                "engine": engine,
            }

    try:
        consider(_run_rapid_ocr(enhanced), "rapidocr-onnx")
    except Exception as exc:
        _log("debug", "RapidOCR unavailable: %s", exc)

    try:
        import pytesseract  # type: ignore

        for variant, mode in jobs:
            data = pytesseract.image_to_data(
                variant,
                lang=ocr_lang,
                config=f"--oem 3 --psm {mode}",
                output_type=pytesseract.Output.DICT,
            )
            consider(data, "tesseract-multipass", mode)
    except Exception as exc:
        _log("debug", "Tesseract OCR unavailable: %s", exc)

    best.update({"clarity": clarity, "width": width, "height": height})
    return best


def _read_text_from_file(
    path: Path,
    mime: str,
    ocr_lang: str,
    options: Optional[Mapping[str, Any]] = None,
    details: Optional[dict[str, Any]] = None,
) -> tuple[str, float]:
    """Extract searchable text from common document, sheet and image formats."""

    suffix = path.suffix.lower()
    try:
        if suffix in TEXT_EXTENSIONS or mime.startswith("text/"):
            raw = path.read_bytes()
            decoded, source_encoding, converted, repair_route = _decode_text_with_info(raw)
            if details is not None:
                details.update({
                    "source_encoding": source_encoding,
                    "target_encoding": "utf-8",
                    "encoding_converted": converted,
                    "encoding_repair": repair_route,
                })
            if suffix == ".rtf":
                try:
                    from striprtf.striprtf import rtf_to_text  # type: ignore

                    return rtf_to_text(decoded), 0.9
                except Exception:
                    pass
            return decoded, 0.95 if decoded.strip() else 0.0
        if suffix == ".pdf":
            from pypdf import PdfReader  # type: ignore

            text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
            if text.strip():
                return text, 0.9
            try:
                try:
                    import pymupdf as fitz  # type: ignore
                except ImportError:
                    import fitz  # type: ignore
                from PIL import Image  # type: ignore

                page_text: list[str] = []
                confidences: list[float] = []
                document = fitz.open(str(path))
                limit = min(len(document), max(1, int((options or {}).get("pdf_ocr_max_pages", 8))))
                for page_number in range(limit):
                    pixmap = document[page_number].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                    result = _recognize_image(image, ocr_lang, options)
                    if result.get("text"):
                        page_text.append(f"页码：{page_number + 1}\n{result['text']}")
                        confidences.append(float(result.get("confidence") or 0))
                document.close()
                if page_text:
                    return "\n".join(page_text), sum(confidences) / len(confidences)
            except Exception as exc:
                _log("debug", "Scanned PDF OCR unavailable for %s: %s", path, exc)
            return "", 0.0
        if suffix in {".docx", ".docm", ".dotx"}:
            text = _extract_docx_text(path)
            return text, 0.95 if text.strip() else 0.0
        if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
            text = _extract_xlsx_text(path)
            return text, 0.98 if text.strip() else 0.0
        if suffix == ".xls":
            text = _extract_xls_text(path)
            return text, 0.95 if text.strip() else 0.0
        if suffix == ".xlsb":
            text = _extract_xlsb_text(path)
            return text, 0.95 if text.strip() else 0.0
        if suffix in {".pptx", ".pptm", ".ppsx"}:
            text = _extract_pptx_text(path)
            return text, 0.93 if text.strip() else 0.0
        if suffix in {".odt", ".ods", ".odp", ".ott", ".fods"}:
            text = _extract_odf_text(path)
            return text, 0.92 if text.strip() else 0.0
        if suffix == ".epub":
            text = _extract_epub_text(path)
            return text, 0.9 if text.strip() else 0.0
        if suffix in IMAGE_EXTENSIONS or mime.startswith("image/"):
            result = _recognize_image(path, ocr_lang, options)
            return str(result.get("text") or ""), float(result.get("confidence") or 0)
    except Exception as exc:
        _log("warning", "Text extraction failed for %s: %s", path, exc)
    return "", 0.0


def _event_value(event: Any, *names: str) -> Any:
    """Return the first non-empty event value across adapter API variants."""

    for name in names:
        value = getattr(event, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value not in (None, "", 0, "0"):
            return value
    return ""


def _object_value(value: Any, *names: str) -> Any:
    for name in names:
        candidate = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if candidate not in (None, "", 0, "0"):
            return candidate
    return ""


def _event_chat_identity(event: Any) -> tuple[str, str]:
    """Return (group/private, id) without depending on one adapter class."""

    message_obj = getattr(event, "message_obj", None)
    raw_message = _object_value(message_obj, "raw_message") if message_obj is not None else None
    group_id = _event_value(event, "get_group_id", "group_id")
    if not group_id and message_obj is not None:
        group_id = _object_value(message_obj, "group_id", "room_id", "channel_id", "guild_id")
    if not group_id and raw_message is not None:
        group_id = _object_value(raw_message, "group_id", "room_id", "channel_id", "guild_id")

    message_type = _event_value(event, "get_message_type", "message_type", "type")
    if not message_type and message_obj is not None:
        message_type = _object_value(message_obj, "message_type", "type")
    unified = str(getattr(event, "unified_msg_origin", "") or "")
    type_text = f"{message_type or ''} {unified}".casefold()
    is_group = bool(group_id) or any(word in type_text for word in ("group", "channel", "guild"))

    if is_group and not group_id and unified:
        group_id = unified.rsplit(":", 1)[-1]
    if is_group:
        return "group", str(group_id or "")

    sender_id = _event_value(event, "get_sender_id", "sender_id", "user_id")
    if not sender_id and message_obj is not None:
        sender_id = _object_value(message_obj, "sender_id", "user_id")
    return "private", str(sender_id or "")


def _parse_allowed_ids(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        values = [key for key, enabled in value.items() if enabled]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = re.split(r"[,，;；\s]+", str(value or ""))
    return {str(item).strip() for item in values if str(item).strip()}


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "是", "开启"}
    return bool(value)


def _event_is_allowed(event: Any, config: Mapping[str, Any]) -> bool:
    if not _as_bool(config.get("restrict_chat_scope", False)):
        return True
    chat_type, chat_id = _event_chat_identity(event)
    key = "allowed_group_ids" if chat_type == "group" else "allowed_private_ids"
    allowed = _parse_allowed_ids(config.get(key, ""))
    return bool(chat_id) and ("*" in allowed or chat_id in allowed)


def _event_sender_id(event: Any) -> str:
    """Return the actual sender of the incoming event across adapter shapes."""

    sender_id = _event_value(event, "get_sender_id", "sender_id", "user_id")
    message_obj = getattr(event, "message_obj", None)
    raw_message = _object_value(message_obj, "raw_message") if message_obj is not None else None
    for value in (message_obj, raw_message):
        if sender_id or value is None:
            break
        sender_id = _object_value(value, "sender_id", "user_id", "uid", "uin")
        if not sender_id:
            sender = _object_value(value, "sender", "author")
            sender_id = _object_value(sender, "sender_id", "user_id", "id", "uid", "uin") if sender else ""
    return str(sender_id or "").strip()


def _event_can_capture(event: Any, config: Mapping[str, Any]) -> bool:
    if not _event_is_allowed(event, config):
        return False
    if not _as_bool(config.get("admin_only_capture", False)):
        return True
    admin_ids = _parse_allowed_ids(config.get("admin_ids", ""))
    sender_id = _event_sender_id(event)
    return bool(sender_id) and sender_id in admin_ids


def _event_source(event: Any) -> dict[str, Any]:
    sender_id = _event_value(event, "get_sender_id", "sender_id", "user_id")
    sender_name = _event_value(event, "get_sender_name", "sender_name")
    message_id = _event_value(event, "get_message_id", "message_id")
    unified = getattr(event, "unified_msg_origin", "")
    chat_type, chat_id = _event_chat_identity(event)
    return {
        "sender_id": str(sender_id or ""),
        "sender_name": str(sender_name or ""),
        "message_id": str(message_id or ""),
        "unified_msg_origin": str(unified or ""),
        "chat_type": chat_type,
        "chat_id": chat_id,
    }


def _object_attrs(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return {key: getattr(value, key) for key in dir(value) if not key.startswith("_") and key in {
            "url", "src", "uri", "file_url", "download_url", "downloadUrl", "href", "path", "file", "file_path", "local_path", "name", "filename", "file_name", "fileName",
            "mime", "mime_type", "type", "data", "base64", "content", "id", "file_id", "resource",
            "messages", "message", "nodes", "node", "content_type", "sender", "sender_id", "sender_name",
            "user_id", "uid", "uin", "author", "nickname", "display_name", "message_id", "msg_id", "time", "timestamp", "source",
            "image", "images", "attachments", "attachment", "files", "forward", "forwards", "forward_messages",
            "selected_messages", "elements", "segments", "raw", "sub_type", "fileId", "token", "summary",
            "raw_message", "forward_id", "res_id", "message_seq",
        }}
    except Exception:
        return {}


# Each merged-forward layer that NapCat inlines (messages → node → message →
# segment → data → content) costs about six levels, so 64 covers ~10 layers of
# 聊天记录套聊天记录. ``seen`` still guards against cycles.
NESTED_SCAN_MAX_DEPTH = 64
FORWARD_MAX_FETCHES = 50


def _iter_nested(value: Any, depth: int = 0, seen: Optional[set[int]] = None) -> Iterator[Any]:
    if depth > NESTED_SCAN_MAX_DEPTH or value is None:
        return
    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_nested(child, depth + 1, seen)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_nested(child, depth + 1, seen)
    elif not isinstance(value, (str, bytes, bytearray, int, float, bool)):
        for child in _object_attrs(value).values():
            if child is not value:
                yield from _iter_nested(child, depth + 1, seen)


def _source_for_nested(value: Any, inherited: Mapping[str, Any]) -> dict[str, Any]:
    """Merge sender/message metadata carried by a forward node.

    Forwarded messages commonly put the original sender on the node object,
    while the nested file component only contains a URL.  Carrying this small
    context down the traversal keeps citations useful without depending on one
    adapter's concrete ``Node`` class.
    """

    attrs = _object_attrs(value)
    result = dict(inherited)
    nested_sender = attrs.get("sender") or attrs.get("author")
    sender_attrs = _object_attrs(nested_sender) if nested_sender is not None else {}
    if isinstance(nested_sender, str) and nested_sender.strip():
        result["sender_name"] = nested_sender.strip()
    for target, keys in (
        ("sender_id", ("sender_id", "user_id", "uid", "uin")),
        ("sender_name", ("sender_name", "nickname", "display_name")),
        ("message_id", ("message_id", "msg_id")),
        ("unified_msg_origin", ("unified_msg_origin",)),
    ):
        for key in keys:
            candidate = attrs.get(key)
            if candidate in (None, ""):
                candidate = sender_attrs.get(key)
            if candidate not in (None, ""):
                result[target] = str(candidate)
                break
    nested_source = attrs.get("source")
    if isinstance(nested_source, Mapping):
        for key in ("sender_id", "sender_name", "message_id", "unified_msg_origin"):
            if nested_source.get(key) not in (None, ""):
                result[key] = str(nested_source[key])
    return result


def _iter_nested_with_source(
    value: Any,
    source: Mapping[str, Any],
    depth: int = 0,
    seen: Optional[set[int]] = None,
) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Yield nested values together with the nearest forward-node context."""

    if depth > NESTED_SCAN_MAX_DEPTH or value is None:
        return
    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    current_source = _source_for_nested(value, source)
    yield value, current_source
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_nested_with_source(child, current_source, depth + 1, seen)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_nested_with_source(child, current_source, depth + 1, seen)
    elif not isinstance(value, (str, bytes, bytearray, int, float, bool)):
        for child in _object_attrs(value).values():
            if child is not value:
                yield from _iter_nested_with_source(child, current_source, depth + 1, seen)


def _candidate_for(value: Any, source: Mapping[str, Any]) -> Optional[AttachmentCandidate]:
    attrs = _object_attrs(value)
    if not attrs:
        return None
    nested_data = attrs.get("data")
    if isinstance(nested_data, Mapping):
        merged = dict(nested_data)
        merged.update({key: val for key, val in attrs.items() if key != "data"})
        attrs = merged
    type_hint = str(attrs.get("type") or attrs.get("content_type") or value.__class__.__name__)
    lowered = f"{type_hint} {attrs.get('mime') or attrs.get('mime_type') or ''}".lower()
    attachment_words = ("image", "picture", "photo", "file", "document", "attachment", "fileitem", "upload", "图片", "文件")
    likely_attachment = any(word in lowered for word in attachment_words)
    declared_name = str(attrs.get("name") or attrs.get("filename") or attrs.get("file_name") or attrs.get("fileName") or "")
    # A few adapters expose bare dictionaries such as {"file": "/tmp/a.pdf"}
    # without a type field.  A known attachment suffix or an explicit file
    # field is sufficient to classify those dictionaries.
    likely_attachment = likely_attachment or bool(
        declared_name
        and Path(urllib.parse.urlparse(declared_name).path).suffix.lower() in CAPTURABLE_EXTENSIONS
    )
    raw_value: Any = None
    # QQ image segments often expose both a cache token in ``file`` and a
    # downloadable URL in ``url``. Prefer the URL so the normal HTTP reader
    # can retrieve the actual image instead of recording an unavailable token.
    for key in ("path", "file_path", "local_path", "url", "src", "uri", "file_url", "download_url", "downloadUrl", "href", "image", "file", "resource", "data", "base64", "content"):
        candidate = attrs.get(key)
        if isinstance(candidate, (str, bytes, bytearray)) and candidate:
            if key in {"data", "base64"} and isinstance(candidate, str) and not (candidate.startswith("data:") or len(candidate) > 80):
                continue
            raw_value = candidate
            if key in {"path", "file_path", "local_path", "url", "src", "uri", "file_url", "download_url", "downloadUrl", "href", "file", "resource"}:
                likely_attachment = True
            break
    if raw_value is None or not likely_attachment:
        return None
    name = declared_name
    if not name and isinstance(raw_value, str):
        parsed = urllib.parse.urlparse(raw_value)
        name = Path(parsed.path).name
    name = _safe_name(name or ("image" if "image" in lowered or "photo" in lowered else "attachment"))
    mime = str(attrs.get("mime") or attrs.get("mime_type") or _mime_for(Path(name)))
    kind = _kind_for(name, mime, type_hint)
    return AttachmentCandidate(name=name, kind=kind, mime=mime, value=raw_value, source=dict(source), type_hint=type_hint)


def _message_roots(event: Any) -> list[Any]:
    roots: list[Any] = []
    for method_name in (
        "get_messages", "get_message_chain", "get_forward_messages",
        "get_forward_message", "get_selected_messages", "get_merged_messages",
    ):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                roots.append(method())
            except Exception:
                pass
    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None:
        roots.append(message_obj)
        roots.append(getattr(message_obj, "message", None))
        roots.append(getattr(message_obj, "raw_message", None))
    roots.append(getattr(event, "message", None))
    # Multi-select forwarding adapters expose the selected nodes directly on
    # the event rather than through ``message_obj``.
    for name in (
        "forward", "forwards", "forward_messages", "selected_messages",
        "merged_messages", "messages", "attachments", "segments",
    ):
        value = getattr(event, name, None)
        if value is not None:
            roots.append(value)
    return [item for item in roots if item is not None]


def _extract_candidates(event: Any, extra_roots: Optional[Iterable[Any]] = None) -> list[AttachmentCandidate]:
    source = _event_source(event)
    candidates: list[AttachmentCandidate] = []
    identities: set[tuple[str, str]] = set()
    roots = _message_roots(event)
    if extra_roots:
        roots.extend(item for item in extra_roots if item is not None)
    for root in roots:
        for value, nested_source in _iter_nested_with_source(root, source):
            candidate = _candidate_for(value, nested_source)
            if candidate is None:
                continue
            identity = (candidate.name, str(candidate.value)[:240])
            if identity not in identities:
                identities.add(identity)
                candidates.append(candidate)
    return candidates


def _forward_ids(event: Any, extra_roots: Optional[Iterable[Any]] = None) -> list[str]:
    identifiers: list[str] = []
    roots = _message_roots(event)
    if extra_roots:
        roots.extend(item for item in extra_roots if item is not None)
    for root in roots:
        for value in _iter_nested(root):
            attrs = _object_attrs(value)
            nested_data = attrs.get("data")
            if isinstance(nested_data, Mapping):
                merged = dict(nested_data)
                merged.update({key: val for key, val in attrs.items() if key != "data"})
                attrs = merged
            type_hint = str(attrs.get("type") or attrs.get("content_type") or value.__class__.__name__).casefold()
            if not any(word in type_hint for word in ("forward", "nodes", "合并转发")):
                continue
            if any(attrs.get(key) for key in ("nodes", "messages", "content", "message")):
                continue
            for key in ("id", "forward_id", "res_id", "message_id"):
                candidate = attrs.get(key)
                if candidate not in (None, "") and str(candidate) not in identifiers:
                    identifiers.append(str(candidate))
                    break
    return identifiers


async def _fetch_forward_payloads(event: Any) -> list[Any]:
    """Resolve QQ merged-forward IDs when the adapter did not inline nodes."""

    identifiers = _forward_ids(event)
    if not identifiers:
        return []
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None) if message_obj is not None else None
    self_id = _object_value(raw_message, "self_id")
    routing = {"self_id": self_id} if self_id not in (None, "") else {}
    targets: list[Any] = [event, getattr(event, "bot", None), getattr(event, "adapter", None), getattr(event, "platform", None)]
    getter = getattr(event, "get_platform_instance", None)
    if callable(getter):
        try:
            platform = getter()
            if inspect.isawaitable(platform):
                platform = await platform
            targets.append(platform)
        except Exception:
            pass
    payloads: list[Any] = []
    attempted: set[str] = set()
    for identifier in identifiers:
        if identifier in attempted:
            continue
        if len(attempted) >= FORWARD_MAX_FETCHES:
            _log("warning", "NameSearcher stopped expanding nested forwards after %s fetches", FORWARD_MAX_FETCHES)
            break
        attempted.add(identifier)
        resolved = False
        for target in targets:
            if target is None:
                continue
            for method_name in ("get_forward_msg", "get_forward_message"):
                method = getattr(target, method_name, None)
                if not callable(method):
                    continue
                for kwargs in ({"message_id": identifier}, {"id": identifier}, {"forward_id": identifier}):
                    try:
                        result = method(**kwargs)
                        if inspect.isawaitable(result):
                            result = await result
                        if result is not None:
                            payloads.append(result)
                            for nested_id in _forward_ids(event, [result]):
                                if nested_id not in identifiers:
                                    identifiers.append(nested_id)
                            resolved = True
                            break
                    except TypeError:
                        continue
                    except Exception as exc:
                        _log("debug", "Unable to fetch QQ forward %s: %s", identifier, exc)
                        break
                if resolved:
                    break
            if resolved:
                break
            for method_name in ("call_action", "call_api"):
                method = getattr(target, method_name, None)
                if not callable(method):
                    continue
                for kwargs in ({"message_id": identifier}, {"id": identifier}):
                    try:
                        try:
                            result = method(action="get_forward_msg", **kwargs, **routing)
                        except TypeError:
                            result = method("get_forward_msg", **kwargs, **routing)
                        if inspect.isawaitable(result):
                            result = await result
                        if result is not None:
                            payloads.append(result)
                            for nested_id in _forward_ids(event, [result]):
                                if nested_id not in identifiers:
                                    identifiers.append(nested_id)
                            resolved = True
                            break
                    except TypeError:
                        continue
                    except Exception as exc:
                        _log("debug", "Unable to call QQ get_forward_msg for %s: %s", identifier, exc)
                        break
                if resolved:
                    break
            if resolved:
                break
    return payloads


FORWARD_URL_ACTION_TIMEOUT = 8
FORWARD_UNRESOLVED_REASON = "forward_file_unavailable"
FORWARD_SKIP_URL = "name-searcher-skip://forward-file"


def _forward_file_is_image(data: Mapping[str, Any]) -> bool:
    for key in ("name", "file", "file_name", "filename"):
        value = str(data.get(key) or "").strip()
        if value and Path(urllib.parse.urlparse(value).path).suffix.lower() in IMAGE_EXTENSIONS:
            return True
    mime = str(data.get("mime") or data.get("mime_type") or "").lower()
    return mime.startswith("image/")


async def _resolve_qq_file_urls(
    event: Any,
    roots: Iterable[Any],
    *,
    get_file_timeout: float = 15,
    on_start: Optional[Callable[[int], Any]] = None,
    images_only: bool = False,
    unavailable: Optional[list[str]] = None,
) -> dict[str, int]:
    """Add downloadable URLs to OneBot file segments inside forward nodes.

    NapCat frequently returns only ``file_id`` in ``get_forward_msg``. The
    objects are mutable JSON dictionaries, so enriching them here lets the
    regular attachment extractor handle the result without QQ-specific logic.

    Strategy: every file is resolved concurrently. The chat-scoped URL action
    gets a short timeout; NapCat ``get_file`` (downloads next to QQ and
    returns a local path) is bounded by ``get_file_timeout`` because QQ can
    hang for minutes on files nested in a forward from another chat. A file
    that still has no source is marked so the notice can say why.

    With ``images_only`` non-image files are not requested at all: they are
    marked unavailable at once (QQ cannot hand them out from a forward) and
    their names are appended to ``unavailable`` for the quoted notice.
    """

    stats = {"pending": 0, "resolved": 0, "failed": 0}
    roots = list(roots)
    if images_only:
        skipped_ids: set[str] = set()
        for root in roots:
            for value in _iter_nested(root):
                if not isinstance(value, MutableMapping):
                    continue
                attrs = _object_attrs(value)
                data = value.get("data") if isinstance(value.get("data"), MutableMapping) else value
                type_hint = str(attrs.get("type") or data.get("type") or "").casefold()
                if type_hint != "file" and not any(data.get(key) for key in ("file_id", "fileId")):
                    continue
                if _forward_file_is_image(data) or data.get("name_searcher_unresolved"):
                    continue
                file_id = str(data.get("file_id") or data.get("fileId") or data.get("name") or data.get("file") or "")
                data["url"] = FORWARD_SKIP_URL
                data["name_searcher_unresolved"] = FORWARD_UNRESOLVED_REASON
                data.setdefault("name", data.get("file") or file_id or "未命名文件")
                if file_id in skipped_ids:
                    continue
                skipped_ids.add(file_id)
                stats["skipped"] = stats.get("skipped", 0) + 1
                if unavailable is not None:
                    unavailable.append(str(data.get("name")))
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        return stats
    chat_type, chat_id = _event_chat_identity(event)
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None) if message_obj is not None else None
    self_id = _object_value(raw_message, "self_id")
    routing = {"self_id": self_id} if self_id not in (None, "") else {}
    seen: set[str] = set()
    jobs: list[tuple[MutableMapping[str, Any], str]] = []
    for root in roots:
        for value in _iter_nested(root):
            if not isinstance(value, MutableMapping):
                continue
            attrs = _object_attrs(value)
            data = value.get("data") if isinstance(value.get("data"), MutableMapping) else value
            type_hint = str(attrs.get("type") or data.get("type") or "").casefold()
            if type_hint != "file" and not any(data.get(key) for key in ("file_id", "fileId")):
                continue
            if any(data.get(key) for key in ("url", "file_url", "download_url")):
                continue
            file_id = str(data.get("file_id") or data.get("fileId") or "").strip()
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            jobs.append((data, file_id))
    stats["pending"] = len(jobs)
    if not jobs:
        return stats
    if on_start is not None:
        try:
            started = on_start(len(jobs))
            if inspect.isawaitable(started):
                await started
        except Exception as exc:
            _log("debug", "Forward receipt failed: %s", exc)

    async def call(action: str, kwargs: Mapping[str, Any], timeout: float) -> Any:
        result = call_action(action=action, **kwargs, **routing)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=max(1.0, float(timeout)))
        return result

    async def resolve(data: MutableMapping[str, Any], file_id: str) -> bool:
        label = data.get("name") or data.get("file") or file_id
        actions: list[tuple[str, dict[str, Any], float]] = []
        if chat_type == "group" and chat_id:
            actions.append(("get_group_file_url", {"file_id": file_id, "group_id": chat_id}, FORWARD_URL_ACTION_TIMEOUT))
        else:
            actions.append(("get_private_file_url", {"file_id": file_id}, FORWARD_URL_ACTION_TIMEOUT))
        if get_file_timeout > 0:
            actions.append(("get_file", {"file_id": file_id}, get_file_timeout))
        for action, kwargs, timeout in actions:
            began = time.monotonic()
            try:
                result = await call(action, kwargs, timeout)
            except asyncio.TimeoutError:
                _log("info", "NameSearcher %s timed out after %ss for forwarded file %s", action, int(timeout), label)
                continue
            except Exception as exc:
                _log("info", "NameSearcher %s failed for forwarded file %s: %s", action, label, exc)
                continue
            result_data = result.get("data") if isinstance(result, Mapping) and isinstance(result.get("data"), Mapping) else result
            if not isinstance(result_data, Mapping):
                continue
            url = result_data.get("url") or result_data.get("file_url") or result_data.get("download_url")
            if not url and action == "get_file":
                if result_data.get("base64"):
                    url = f"data:application/octet-stream;base64,{result_data.get('base64')}"
                elif result_data.get("file"):
                    url = result_data.get("file")
            if url:
                data["url"] = url
                data.setdefault("name", result_data.get("file_name") or result_data.get("name") or data.get("file") or file_id)
                _log("info", "NameSearcher resolved forwarded file %s via %s in %.1fs", data.get("name") or file_id, action, time.monotonic() - began)
                return True
        data["name_searcher_unresolved"] = FORWARD_UNRESOLVED_REASON
        data["url"] = FORWARD_SKIP_URL
        data.setdefault("name", data.get("file") or file_id)
        if unavailable is not None:
            unavailable.append(str(data.get("name")))
        _log("warning", "NameSearcher could not resolve forwarded file %s (%s)", label, file_id)
        return False

    results = await asyncio.gather(*(resolve(data, file_id) for data, file_id in jobs), return_exceptions=True)
    stats["resolved"] = sum(1 for item in results if item is True)
    stats["failed"] = len(results) - stats["resolved"]
    return stats


def _read_candidate(candidate: AttachmentCandidate, timeout: int, max_bytes: int) -> tuple[Optional[bytes], str]:
    value = candidate.value
    if isinstance(value, str) and value.startswith(FORWARD_SKIP_URL):
        return None, FORWARD_UNRESOLVED_REASON
    if isinstance(value, (bytes, bytearray)):
        content = bytes(value)
        if len(content) > max_bytes:
            return None, "too_large"
        return content, "ok"
    text = str(value)
    if text.startswith("data:") and "," in text:
        header, payload = text.split(",", 1)
        try:
            data = base64.b64decode(payload, validate=True)
            if len(data) > max_bytes:
                return None, "too_large"
            return data, "ok"
        except (ValueError, binascii.Error):
            return None, "invalid_base64"
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme == "file":
        text = urllib.request.url2pathname(parsed.path)
        parsed = urllib.parse.urlparse("")
    # urlparse treats a Windows drive letter as a URI scheme ("C:").
    # Drive-qualified and UNC paths are always local attachment paths.
    windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"))
    local: Optional[Path] = None
    # URLs and base64 payloads are not valid local paths; constructing or
    # stat-ing them can also raise on Windows when they exceed MAX_PATH.
    if (parsed.scheme in {"", "file"} or windows_path) and len(text) < 4096:
        try:
            local = Path(text).expanduser()
        except (OSError, ValueError):
            local = None
    try:
        local_exists = bool(local and local.exists() and local.is_file())
    except OSError:
        local_exists = False
    if local_exists and local is not None:
        try:
            if local.stat().st_size > max_bytes:
                return None, "too_large"
            return local.read_bytes(), "ok"
        except OSError as exc:
            return None, f"read_failed:{exc}"
    if parsed.scheme in {"http", "https"}:
        urls = [text]
        decoded = urllib.parse.unquote(text)
        if decoded != text:
            urls.append(decoded)
        last_error: Exception | None = None
        for url in urls:
            for attempt in range(3):
                try:
                    request = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": f"AstrBot-NameSearcher/{PLUGIN_VERSION}",
                            "Accept": "image/*,application/octet-stream,*/*;q=0.8",
                            "Cache-Control": "no-cache",
                        },
                    )
                    with urllib.request.urlopen(request, timeout=max(3, timeout)) as response:
                        chunks: list[bytes] = []
                        size = 0
                        while True:
                            chunk = response.read(min(1024 * 256, max_bytes + 1 - size))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > max_bytes:
                                return None, "too_large"
                    data = b"".join(chunks)
                    if data:
                        return data, "ok"
                    last_error = OSError("empty response")
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(0.25 * (attempt + 1))
        return None, f"download_failed:{last_error or 'empty response'}"
    return None, "no_download_url"


def _archive_limits(config: Mapping[str, Any]) -> tuple[int, int, int]:
    max_depth = max(0, min(8, int(float(config.get("archive_max_depth", 3) or 3))))
    max_members = max(1, min(5000, int(float(config.get("archive_max_members", 300) or 300))))
    max_total = max(1, int(float(config.get("archive_max_total_size_mb", 200) or 200))) * 1024 * 1024
    return max_depth, max_members, max_total


def _safe_archive_member(name: Any) -> Optional[str]:
    """Return a normalized relative member path, rejecting traversal and links."""

    raw = str(name or "").replace("\\", "/").strip()
    if not raw or raw.endswith("/") or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _fix_zip_name(name: str, utf8_flag: bool = False) -> str:
    """Recover CJK member names that zipfile decoded as CP437.

    Windows (WinRAR/360/Bandizip legacy mode) writes GBK names without the
    UTF-8 flag, so Python shows them as ``2024_º_9_Ñ.xlsx``.  Re-encode with
    CP437 and try UTF-8 first (tools that forget the flag), then GB18030.
    """

    if utf8_flag or not name or name.isascii():
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "gb18030", "big5", "shift_jis"):
        try:
            fixed = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if fixed != name and _CJK_CHAR.search(fixed):
            return fixed
    return name


def _repair_mojibake_name(name: Any) -> str:
    """Repair an already-stored path whose segments are CP437-decoded GBK."""

    text = str(name or "")
    if not text or text.isascii():
        return text
    return "/".join(_fix_zip_name(part) for part in text.split("/"))


def _repair_record_names(record: dict[str, Any]) -> bool:
    """Fix garbled names on archive-member records in place; True if changed."""

    source = record.get("source")
    if not isinstance(source, dict) or not source.get("archive_member"):
        return False
    member = _repair_mojibake_name(source.get("archive_member"))
    if member == source.get("archive_member"):
        return False
    # The stored display name went through _safe_name, which already replaced
    # the box-drawing characters, so rebuild it from the intact member path.
    source["archive_member"] = member
    record["name"] = _safe_name(member)
    return True


def _zip_member_name(info: "zipfile.ZipInfo") -> str:
    return _fix_zip_name(info.filename, bool(info.flag_bits & 0x800))


def _archive_reader_members(
    name: str,
    content: bytes,
    max_members: int,
    max_total: int,
) -> tuple[list[tuple[str, bytes]], list[str], int]:
    """Read common archive formats through optional libraries without writing members to disk."""

    suffix = Path(name).suffix.lower()
    members: list[tuple[str, bytes]] = []
    errors: list[str] = []
    skipped = 0
    declared_total = 0
    if suffix == ".zip" or content[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    member_name = _zip_member_name(info)
                    if len(members) >= max_members:
                        skipped += 1
                        errors.append(f"超出成员个数上限（{max_members} 个），未识别：{member_name}")
                        continue
                    safe = _safe_archive_member(member_name)
                    if safe is None:
                        errors.append(f"跳过不安全路径：{member_name}")
                        continue
                    # Unix mode 0o120000 marks symlinks in ZIP metadata.
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        errors.append(f"跳过符号链接：{safe}")
                        continue
                    if info.file_size < 0 or info.file_size > max_total - declared_total:
                        skipped += 1
                        errors.append(f"超出解压总大小上限，未识别：{safe}")
                        continue
                    try:
                        payload = archive.read(info)
                        declared_total += len(payload)
                        members.append((safe, payload))
                    except Exception as exc:
                        errors.append(f"{safe}：{exc}")
        except Exception as exc:
            errors.append(f"ZIP：{exc}")
        return members, errors, skipped
    if suffix == ".7z":
        try:
            import py7zr  # type: ignore

            with tempfile.TemporaryDirectory(prefix="name-searcher-7z-") as temp:
                source = Path(temp) / "archive.7z"
                output = Path(temp) / "output"
                output.mkdir()
                source.write_bytes(content)
                with py7zr.SevenZipFile(source, mode="r") as archive:
                    allowed: list[str] = []
                    for info in archive.list():
                        member_name = str(getattr(info, "filename", "") or "")
                        safe = _safe_archive_member(member_name)
                        is_directory = bool(getattr(info, "is_directory", False))
                        size = int(getattr(info, "uncompressed", 0) or 0)
                        if is_directory:
                            continue
                        if safe is None:
                            errors.append(f"跳过不安全路径：{member_name}")
                            continue
                        if len(allowed) >= max_members:
                            skipped += 1
                            errors.append(f"超出成员个数上限（{max_members} 个），未识别：{member_name}")
                            continue
                        if size < 0 or size > max_total - declared_total:
                            skipped += 1
                            errors.append(f"超出解压总大小上限，未识别：{member_name}")
                            continue
                        declared_total += size
                        allowed.append(member_name)
                    archive.extract(path=output, targets=allowed)
                for member_name in allowed:
                    safe = _safe_archive_member(member_name)
                    if safe is None:
                        errors.append(f"跳过不安全路径：{member_name}")
                        continue
                    target = (output / Path(*safe.split("/"))).resolve()
                    try:
                        target.relative_to(output.resolve())
                    except ValueError:
                        skipped += 1
                        continue
                    if target.is_file():
                        members.append((safe, target.read_bytes()))
        except ImportError:
            errors.append("7Z：未安装 py7zr")
        except Exception as exc:
            errors.append(f"7Z：{exc}")
        return members, errors, skipped
    if suffix == ".rar":
        try:
            import rarfile  # type: ignore

            with tempfile.TemporaryDirectory(prefix="name-searcher-rar-") as temp:
                source = Path(temp) / "archive.rar"
                source.write_bytes(content)
                with rarfile.RarFile(source) as archive:
                    for info in archive.infolist():
                        if info.isdir():
                            continue
                        if len(members) >= max_members:
                            skipped += 1
                            errors.append(f"超出成员个数上限（{max_members} 个），未识别：{info.filename}")
                            continue
                        safe = _safe_archive_member(info.filename)
                        if safe is None or info.isdir():
                            if safe is None:
                                errors.append(f"跳过不安全路径：{info.filename}")
                            continue
                        size = int(getattr(info, "file_size", 0) or 0)
                        if size < 0 or size > max_total - declared_total:
                            skipped += 1
                            errors.append(f"超出解压总大小上限，未识别：{safe}")
                            continue
                        try:
                            payload = archive.read(info)
                            declared_total += len(payload)
                            members.append((safe, payload))
                        except Exception as exc:
                            errors.append(f"{safe}：{exc}")
        except ImportError:
            errors.append("RAR：未安装 rarfile")
        except Exception as exc:
            errors.append(f"RAR：{exc}")
        return members, errors, skipped
    lower_name = name.lower()
    if suffix in {".tar", ".tgz", ".tbz", ".tbz2", ".txz"} or lower_name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
                for info in archive.getmembers():
                    if not info.isfile():
                        continue
                    if len(members) >= max_members:
                        skipped += 1
                        errors.append(f"超出成员个数上限（{max_members} 个），未识别：{info.name}")
                        continue
                    safe = _safe_archive_member(info.name)
                    if safe is None:
                        errors.append(f"跳过不安全路径：{info.name}")
                        continue
                    if not info.isfile():
                        continue
                    if info.size < 0 or info.size > max_total - declared_total:
                        skipped += 1
                        errors.append(f"超出解压总大小上限，未识别：{safe}")
                        continue
                    try:
                        handle = archive.extractfile(info)
                        if handle is not None:
                            payload = handle.read(max_total - declared_total + 1)
                            if len(payload) > max_total - declared_total:
                                skipped += 1
                                errors.append(f"超出解压总大小上限，未识别：{safe}")
                                continue
                            declared_total += len(payload)
                            members.append((safe, payload))
                    except Exception as exc:
                        errors.append(f"{safe}：{exc}")
        except Exception as exc:
            errors.append(f"TAR：{exc}")
        return members, errors, skipped
    if suffix in {".gz", ".bz2", ".xz"}:
        try:
            opener = gzip.GzipFile if suffix == ".gz" else bz2.BZ2File if suffix == ".bz2" else lzma.LZMAFile
            with opener(io.BytesIO(content), mode="rb") as stream:
                payload = stream.read(max_total + 1)
            if len(payload) > max_total:
                skipped += 1
                errors.append(f"超出解压总大小上限，未识别：{name}")
            else:
                members.append((Path(name).stem or "解压文件", payload))
        except Exception as exc:
            errors.append(f"{suffix}：{exc}")
    return members, errors, skipped


def _expand_archive_bytes(
    name: str,
    content: bytes,
    config: Mapping[str, Any],
    *,
    depth: int = 0,
) -> ArchiveExpansion:
    """Safely enumerate supported archive members with bounded recursion."""

    if not _as_bool(config.get("archive_extraction_enabled", True)) or not _kind_for(name, "") == "archive":
        return ArchiveExpansion([], [])
    max_depth, max_members, max_total = _archive_limits(config)
    if depth >= max_depth:
        return ArchiveExpansion([], [f"超出嵌套层数上限（{max_depth} 层），未展开：{name}"], 0, 1)
    pending = _archive_reader_members(name, content, max_members, max_total)
    output: list[tuple[str, bytes]] = []
    errors = list(pending[1])
    total = 0
    skipped = pending[2]
    depth_limited = 0
    for member_name, payload in pending[0]:
        if len(output) >= max_members:
            skipped += 1
            errors.append(f"超出成员个数上限（{max_members} 个），未识别：{member_name}")
            continue
        if len(payload) > max_total - total:
            skipped += 1
            errors.append(f"超出解压总大小上限，未识别：{member_name}")
            continue
        total += len(payload)
        output.append((member_name, payload))
        if _kind_for(member_name, "") == "archive":
            if depth + 1 >= max_depth:
                depth_limited += 1
                errors.append(f"超出嵌套层数上限（{max_depth} 层），未展开：{member_name}")
                continue
            nested = _expand_archive_bytes(member_name, payload, config, depth=depth + 1)
            errors.extend(f"{member_name}/{item}" if "：" not in item else item.replace("：", f"：{member_name}/", 1) for item in nested.errors)
            skipped += nested.skipped
            depth_limited += nested.depth_limited
            for nested_name, nested_payload in nested.members:
                if len(output) >= max_members:
                    skipped += 1
                    errors.append(f"超出成员个数上限（{max_members} 个），未识别：{member_name}/{nested_name}")
                    continue
                if len(nested_payload) > max_total - total:
                    skipped += 1
                    errors.append(f"超出解压总大小上限，未识别：{member_name}/{nested_name}")
                    continue
                total += len(nested_payload)
                output.append((f"{member_name}/{nested_name}", nested_payload))
    return ArchiveExpansion(output, errors, skipped, depth_limited)


def _archive_candidates(
    archive_record: Mapping[str, Any],
    content: bytes,
    config: Mapping[str, Any],
) -> tuple[list[AttachmentCandidate], list[str]]:
    expansion = _expand_archive_bytes(str(archive_record.get("name") or "archive.zip"), content, config)
    candidates: list[AttachmentCandidate] = []
    for member_name, payload in expansion.members:
        if not _kind_for(member_name, "") in {"image", "document", "spreadsheet", "presentation", "file", "archive"}:
            continue
        source = dict(archive_record.get("source") or {})
        source.update({
            "archive_id": archive_record.get("id"),
            "archive_name": archive_record.get("name"),
            "archive_member": member_name,
            "archive_depth": member_name.count("/") + 1,
        })
        candidates.append(AttachmentCandidate(
            name=f"{archive_record.get('name')}/{member_name}",
            kind=_kind_for(member_name, _mime_for(Path(member_name))),
            mime=_mime_for(Path(member_name)),
            value=payload,
            source=source,
            type_hint="archive-member",
        ))
    limited = expansion.skipped + expansion.depth_limited
    errors = list(expansion.errors)
    if limited:
        errors.insert(0, f"共 {limited} 个成员超出层数、个数或大小上限，未识别")
    return candidates, errors


def _panel_upload_candidates(
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[list[AttachmentCandidate], list[str]]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raw_files = [payload]
    max_bytes = int(float(config.get("max_file_size_mb", 25) or 25) * 1024 * 1024)
    candidates: list[AttachmentCandidate] = []
    errors: list[str] = []
    for index, item in enumerate(raw_files[:100], 1):
        if not isinstance(item, Mapping):
            errors.append(f"第 {index} 个上传项格式无效")
            continue
        name = _safe_name(str(item.get("name") or item.get("filename") or f"upload-{index}"))
        mime = str(item.get("mime") or item.get("type") or _mime_for(Path(name)))
        encoded = str(item.get("data_base64") or item.get("data") or "")
        if encoded.startswith("data:") and "," in encoded:
            header, encoded = encoded.split(",", 1)
            if not item.get("mime") and ";" in header:
                mime = header[5:].split(";", 1)[0] or mime
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            errors.append(f"{name}：Base64 数据无效")
            continue
        if not content:
            errors.append(f"{name}：文件为空")
            continue
        if len(content) > max_bytes:
            errors.append(f"{name}：超过单文件大小上限")
            continue
        candidates.append(AttachmentCandidate(
            name=name,
            kind=_kind_for(name, mime, "panel-upload"),
            mime=mime,
            value=content,
            source={
                "sender_id": "plugin-page",
                "sender_name": "Plugin Page 本地上传",
                "message_id": f"upload-{int(time.time())}",
                "unified_msg_origin": "plugin-page:local-upload",
                "chat_type": "panel",
                "chat_id": "plugin-page",
                "upload_source": "plugin-page",
            },
            type_hint="panel-upload",
        ))
    if len(raw_files) > 100:
        errors.append("单次最多上传 100 个文件")
    return candidates, errors


class PeopleIndex:
    """Search configured people records and OCR text captured from files."""

    LABELS = ("班级", "专业", "学院", "年级", "学号", "部门", "职位", "电话", "邮箱")

    def __init__(self, config: Mapping[str, Any], store: ArtifactStore) -> None:
        self.config = config
        self.store = store
        self._records: list[dict[str, Any]] = []
        self._loaded_from = ""
        self.last_hidden_partial = 0
        self.reload()

    def reload(self) -> None:
        self._records = []
        raw_path = str(self.config.get("people_file") or "").strip()
        if not raw_path:
            return
        path = _resolve_data_path(raw_path)
        self._loaded_from = str(path)
        try:
            if path.suffix.lower() == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, Mapping):
                    data = data.get("people", [])
                if isinstance(data, list):
                    self._records = [dict(item) for item in data if isinstance(item, Mapping)]
            elif path.suffix.lower() in {".csv", ".tsv"}:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    self._records = [dict(row) for row in csv.DictReader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")]
            elif path.suffix.lower() in {".xlsx", ".xls"}:
                from openpyxl import load_workbook  # type: ignore

                workbook = load_workbook(str(path), read_only=True, data_only=True)
                sheet = workbook.active
                rows = list(sheet.iter_rows(values_only=True))
                if rows:
                    headers = [str(value or "") for value in rows[0]]
                    self._records = [dict(zip(headers, row)) for row in rows[1:]]
        except Exception as exc:
            _log("warning", "Unable to load people file %s: %s", path, exc)

    def _from_artifacts(self, query: str) -> list[dict[str, Any]]:
        normalised = _normalise_name(query)
        query_key = _search_key(query)
        matches: list[dict[str, Any]] = []
        for record in self.store.list():
            reviewed_name = str(record.get("reviewed_name") or "").strip()
            reviewed_match = (
                record.get("review_status") == "accepted"
                and reviewed_name
                and normalised == _normalise_name(reviewed_name)
            )
            graded = [
                (label, text, _name_match_strength(text, query))
                for label, text in _artifact_search_sections(record)
                if query_key
            ]
            exact_sections = [(label, text) for label, text, grade in graded if grade == "exact"]
            partial_sections = [(label, text) for label, text, grade in graded if grade == "partial"]
            matched_sections = exact_sections or partial_sections
            if not reviewed_match and not matched_sections:
                continue
            strength = "exact" if (reviewed_match or exact_sections) else "partial"
            match_label, matched_text = matched_sections[0] if matched_sections else ("人工审核", reviewed_name)
            lines = [line.strip() for line in _visible_model_text(matched_text).splitlines() if line.strip()]
            relevant = (
                next((line for line in lines if _name_match_strength(line, query) == strength), "")
                or next((line for line in lines if query_key in _search_key(line)), "")
            )
            if not relevant and matched_text:
                relevant = _visible_model_text(matched_text).strip()[:500]
            details: dict[str, Any] = {"姓名": reviewed_name if reviewed_match else query}
            if relevant:
                details["识别行"] = relevant
            if matched_sections:
                details["匹配依据"] = match_label
            if strength == "partial":
                details["匹配方式"] = "包含匹配（可能是其他人）"
            if reviewed_match:
                details["审核状态"] = "已确认"
            elif record.get("llm_review_status") == "completed":
                details["审核状态"] = "LLM审核"
            for label in self.LABELS:
                match = re.search(rf"{re.escape(label)}\s*[:：,，\s]+([^|,，;；\s]+)", relevant or matched_text)
                if match:
                    details[label] = match.group(1)
            score = 1.0 if reviewed_match else (0.84 if strength == "exact" else 0.5)
            matches.append({"record": details, "score": score, "source": record, "strength": strength})
        return matches

    def wildcard_search(self, query: str, limit: int = WILDCARD_MAX_NAMES) -> dict[str, Any]:
        """Group every name matching a wildcard query with the files it appears in."""

        pattern = _wildcard_pattern(query)
        if pattern is None:
            return {"ok": False, "query": query, "wildcard": True, "names": [], "message": "通配符前后至少要有一个字，例如 张* 或 王小?"}
        groups: dict[str, dict[str, Any]] = {}

        def group_for(name: str) -> dict[str, Any]:
            key = _normalise_name(name)
            if key not in groups:
                groups[key] = {"name": name, "files": [], "library": []}
            return groups[key]

        for record in self._records:
            name = str(record.get("姓名") or record.get("name") or record.get("姓名/名称") or "").strip()
            if not name:
                continue
            if any(_normalise_name(hit) == _normalise_name(name) for hit, _ in _wildcard_names(name, pattern, query)):
                group_for(name)["library"].append(dict(record))
        for record in self.store.list():
            record_id = str(record.get("id") or "")
            if not record_id or record.get("download_status") != "ok":
                continue
            seen_here: set[str] = set()
            for text in _wildcard_text_sections(record):
                for name, line in _wildcard_names(text, pattern, query):
                    key = _normalise_name(name)
                    if key in seen_here:
                        continue
                    seen_here.add(key)
                    group_for(name)["files"].append({
                        "id": record_id,
                        "name": str(record.get("name") or "未命名文件"),
                        "line": line,
                        "kind": record.get("kind"),
                        "reviewed": record.get("llm_review_status") == "completed",
                    })
        names = sorted(groups.values(), key=lambda item: (-len(item["files"]) - len(item["library"]), item["name"]))
        total_files = len({item["id"] for group in names for item in group["files"]})
        return {
            "ok": True,
            "query": query,
            "wildcard": True,
            "names": names[:limit],
            "name_count": len(names),
            "file_count": total_files,
            "truncated": max(0, len(names) - limit),
        }

    def search(self, query: str, threshold: float = 0.85) -> list[dict[str, Any]]:
        """Exact name hits first.  Near-miss names (王小明 vs 王小红) are no
        longer treated as matches: fuzzy library hits need ``threshold`` and are
        only used when nothing matches exactly, and substring hits inside longer
        names (张三 in 张三丰) are only shown when no whole-name hit exists."""

        query = str(query or "").strip()
        if not query:
            return []
        self.last_hidden_partial = 0
        normalised = _normalise_name(query)
        exact: list[dict[str, Any]] = []
        fuzzy: list[dict[str, Any]] = []
        source = {"name": self._loaded_from or "配置人员库"}
        for record in self._records:
            name = str(record.get("姓名") or record.get("name") or record.get("姓名/名称") or "").strip()
            if not name:
                continue
            candidate = _normalise_name(name)
            if candidate == normalised:
                exact.append({"record": record, "score": 1.0, "source": source, "strength": "exact"})
                continue
            if len(candidate) != len(normalised):
                continue
            score = difflib.SequenceMatcher(None, candidate, normalised).ratio()
            if score >= threshold:
                annotated = dict(record)
                annotated["匹配方式"] = f"近似姓名（{name}，相似度 {score:.2f}）"
                fuzzy.append({"record": annotated, "score": score * 0.9, "source": source, "strength": "fuzzy"})
        artifacts = self._from_artifacts(query)
        exact.extend(item for item in artifacts if item.get("strength") == "exact")
        partial = [item for item in artifacts if item.get("strength") != "exact"]
        if exact:
            results = exact
            self.last_hidden_partial = len(partial) + len(fuzzy)
        else:
            results = fuzzy + partial
        results.sort(key=lambda item: float(item.get("score", 0)), reverse=True)
        return results


def _parse_command_text(event: Any, command: str, supplied: str = "") -> str:
    if supplied and supplied.strip():
        return supplied.strip()
    for method_name in ("get_message_str", "get_plain_text"):
        method = getattr(event, method_name, None)
        if callable(method):
            try:
                text = str(method() or "").strip()
                match = re.match(rf"^/?{re.escape(command)}\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
                if match:
                    return match.group(1).strip()
                return text
            except Exception:
                pass
    return ""


def _plain(event: Any, text: str) -> Any:
    method = getattr(event, "plain_result", None)
    if callable(method):
        try:
            return method(text)
        except Exception:
            pass
    return text


async def _send(event: Any, text: str) -> None:
    sender = getattr(event, "send", None)
    if not callable(sender):
        return
    try:
        result = sender(_plain(event, text))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        _log("debug", "Unable to send proactive notice: %s", exc)


async def _send_quoted(event: Any, text: str) -> None:
    """Reply to the triggering message (quote) and @ its sender in groups."""

    sender = getattr(event, "send", None)
    chain_result = getattr(event, "chain_result", None)
    if callable(sender) and callable(chain_result):
        try:
            import astrbot.api.message_components as Comp  # type: ignore

            message_id = str(_event_value(event, "get_message_id", "message_id") or "")
            sender_id = str(_event_value(event, "get_sender_id", "sender_id", "user_id") or "")
            chat_type, _ = _event_chat_identity(event)
            components: list[Any] = []
            if message_id:
                components.append(Comp.Reply(id=message_id))
            if chat_type == "group" and sender_id:
                components.append(Comp.At(qq=sender_id))
                text = " " + text
            components.append(Comp.Plain(text))
            result = sender(chain_result(components))
            if inspect.isawaitable(result):
                await result
            return
        except Exception as exc:
            _log("debug", "Unable to send quoted notice, falling back to plain text: %s", exc)
    await _send(event, text)


def _forward_unavailable_notice(names: Iterable[str], limit: int = 10) -> str:
    unique = list(dict.fromkeys(str(name) for name in names if str(name).strip()))
    if not unique:
        return ""
    shown = "\n".join(f"· {name}" for name in unique[:limit])
    more = f"\n…等共 {len(unique)} 个" if len(unique) > limit else ""
    return (
        f"这条聊天记录里有 {len(unique)} 个非图片文件下载失败（合并转发里只能获取图片，QQ 不提供其他文件的下载）：\n"
        f"{shown}{more}\n请直接发送原文件。图片已照常识别。"
    )


def _model_text(value: Any, depth: int = 0) -> str:
    """Extract text from common AstrBot/OpenAI provider response shapes."""

    if depth > 5 or value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in (
            "completion_text", "text", "content", "answer", "response",
            "message", "output", "result", "choices",
        ):
            if key in value:
                text = _model_text(value[key], depth + 1)
                if text:
                    return text
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _model_text(item, depth + 1)
            if text:
                return text
        return ""
    for key in (
        "completion_text", "text", "content", "answer", "response",
        "message", "output", "result",
    ):
        candidate = getattr(value, key, None)
        if candidate is not None:
            text = _model_text(candidate, depth + 1)
            if text:
                return text
    return ""


def _provider_options(context: Any) -> list[dict[str, Any]]:
    """Expose configured chat providers as safe Page select options."""

    manager = getattr(context, "provider_manager", None)
    configs = getattr(manager, "providers_config", None) if manager is not None else None
    instances = getattr(manager, "provider_insts", None) if manager is not None else None
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(configs, Mapping):
        configs = [dict(value, id=key) if isinstance(value, Mapping) and not value.get("id") else value for key, value in configs.items()]
    if isinstance(configs, (list, tuple)):
        for config in configs:
            if not isinstance(config, Mapping) or config.get("enable") is False:
                continue
            provider_id = str(config.get("id") or config.get("provider_id") or config.get("name") or "").strip()
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            model = config.get("model") or config.get("model_name") or config.get("default_model")
            raw_models = config.get("models") or config.get("model_list") or []
            if isinstance(raw_models, str):
                raw_models = [raw_models]
            models = [str(item) for item in raw_models if item]
            if model and str(model) not in models:
                models.insert(0, str(model))
            options.append({"id": provider_id, "label": str(config.get("name") or provider_id), "models": models})
    if isinstance(instances, Mapping):
        instances = [dict(value, provider_id=key) if isinstance(value, Mapping) and not value.get("provider_id") else value for key, value in instances.items()]
    if isinstance(instances, (list, tuple)):
        for instance in instances:
            provider_id = ""
            label = ""
            models: list[str] = []
            try:
                meta = instance.meta()
                provider_id = str(getattr(meta, "id", "") or "")
                label = str(getattr(meta, "name", "") or getattr(meta, "type", "") or "")
            except Exception:
                provider_id = str(getattr(instance, "provider_id", "") or "")
            if not provider_id and isinstance(instance, Mapping):
                provider_id = str(instance.get("provider_id") or instance.get("id") or "")
            if not provider_id or provider_id in seen:
                continue
            for attr in ("model", "model_name", "default_model"):
                value = getattr(instance, attr, None)
                if value:
                    models.append(str(value))
            seen.add(provider_id)
            options.append({"id": provider_id, "label": label or provider_id, "models": models})
    return options


def _parse_model_targets(raw: Any) -> list[tuple[str, str]]:
    """Parse a multi-select of ``provider::model`` entries (newline/comma list, list or mapping)."""

    if isinstance(raw, Mapping):
        values = [key for key, enabled in raw.items() if enabled]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = re.split(r"[\r\n,，;；]+", str(raw or ""))
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if isinstance(value, Mapping):
            provider = str(value.get("provider") or value.get("provider_id") or "").strip()
            model = str(value.get("model") or value.get("model_name") or "").strip()
        else:
            token = str(value or "").strip()
            if not token:
                continue
            provider, separator, model = token.partition("::")
            if not separator:
                provider, separator, model = token.partition("|")
            provider = provider.strip()
            model = model.strip() if separator else ""
        target = (provider, model)
        if target not in seen and (provider or model):
            seen.add(target)
            targets.append(target)
    return targets


def _parse_vision_targets(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    targets = _parse_model_targets(config.get("vision_model_targets", ""))
    if not targets:
        targets.append((
            str(config.get("vision_provider") or "").strip(),
            str(config.get("vision_model") or "").strip(),
        ))
    return targets


def _parse_llm_review_targets(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    targets = _parse_model_targets(config.get("llm_review_targets", ""))
    if not targets:
        targets.append((
            str(config.get("llm_review_provider") or "").strip(),
            str(config.get("llm_review_model") or "").strip(),
        ))
    return targets


def _model_scope_allows(record: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    """Vision/LLM review only runs on images and PDFs unless scope is "all"."""

    if str(config.get("vision_file_scope") or "image_pdf").strip().lower() == "all":
        return True
    name = str(record.get("name") or "")
    suffix = Path(name).suffix.lower()
    mime = str(record.get("mime") or "").lower()
    return (
        suffix in IMAGE_EXTENSIONS
        or suffix == ".pdf"
        or mime.startswith("image/")
        or mime == "application/pdf"
        or record.get("kind") == "image"
    )


def _render_text_review_pages(text: str, output_dir: Path, max_pages: int) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception:
        return []
    font = None
    for candidate in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ):
        try:
            if Path(candidate).exists():
                font = ImageFont.truetype(candidate, 28)
                break
        except Exception:
            continue
    font = font or ImageFont.load_default()
    wrapped: list[str] = []
    for line in str(text or "").splitlines():
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(line, width=52, replace_whitespace=False, drop_whitespace=False) or [line])
    if not wrapped:
        return []
    paths: list[Path] = []
    lines_per_page = 46
    for page_index in range(min(max(1, max_pages), (len(wrapped) + lines_per_page - 1) // lines_per_page)):
        page_lines = wrapped[page_index * lines_per_page:(page_index + 1) * lines_per_page]
        image = Image.new("RGB", (1600, 2200), "white")
        draw = ImageDraw.Draw(image)
        draw.text((55, 35), f"文件识别审阅页 {page_index + 1}", fill="#17324d", font=font)
        y = 95
        for line in page_lines:
            try:
                draw.text((55, y), line, fill="#111827", font=font)
            except UnicodeEncodeError:
                draw.text((55, y), line.encode("ascii", "replace").decode("ascii"), fill="#111827", font=font)
            y += 44
        target = output_dir / f"text-review-{page_index + 1}.png"
        image.save(target, format="PNG", optimize=True)
        paths.append(target)
    return paths


VISION_PAGES_HARD_CAP = 100


def _vision_page_limit(value: Any) -> int:
    """Pages sent to each vision model per file; 0 (or less) means all pages, up to the hard cap."""

    try:
        pages = int(float(value))
    except (TypeError, ValueError):
        pages = 10
    if pages <= 0:
        return VISION_PAGES_HARD_CAP
    return min(pages, VISION_PAGES_HARD_CAP)


def _import_fitz() -> Any:
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:
        import fitz  # type: ignore
    return fitz


def _pdf_page_count(path: Path) -> int:
    try:
        with _import_fitz().open(str(path)) as document:
            return len(document)
    except Exception:
        return 0


def _pdf_page_texts(path: Path, start: int) -> list[str]:
    """Text layer of PDF pages from ``start`` (0-based) on; empty strings for scanned pages."""

    try:
        with _import_fitz().open(str(path)) as document:
            return [str(document[index].get_text() or "").strip() for index in range(start, len(document))]
    except Exception:
        return []


def _prepare_vision_review_images(
    path: Path,
    mime: str,
    extracted_text: str,
    output_dir: Path,
    max_pages: int,
) -> list[Path]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS or mime.startswith("image/"):
        return [path]
    try:
        with path.open("rb") as handle:
            sniffed = _sniff_content_type(handle.read(32))
    except OSError:
        sniffed = None
    if sniffed and sniffed[1].startswith("image/"):
        return [path]
    if sniffed and sniffed[0] == ".pdf":
        suffix = ".pdf"
    max_pages = _vision_page_limit(max_pages)
    if suffix == ".pdf":
        try:
            try:
                import pymupdf as fitz  # type: ignore
            except ImportError:
                import fitz  # type: ignore

            images: list[Path] = []
            document = fitz.open(str(path))
            for page_index in range(min(len(document), max_pages)):
                pixmap = document[page_index].get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
                target = output_dir / f"pdf-page-{page_index + 1}.png"
                target.write_bytes(pixmap.tobytes("png"))
                images.append(target)
            document.close()
            if images:
                return images
        except Exception as exc:
            _log("debug", "Unable to render PDF for visual review: %s", exc)
    images = _render_text_review_pages(extracted_text, output_dir, max_pages)
    if suffix in {".docx", ".docm", ".dotx"}:
        try:
            with zipfile.ZipFile(path) as archive:
                media = [
                    name for name in archive.namelist()
                    if name.startswith("word/media/") and Path(name).suffix.lower() in IMAGE_EXTENSIONS
                ]
                remaining = max(0, max_pages - len(images))
                for index, member in enumerate(media[:remaining], 1):
                    target = output_dir / f"docx-media-{index}{Path(member).suffix.lower()}"
                    target.write_bytes(archive.read(member))
                    images.append(target)
        except Exception as exc:
            _log("debug", "Unable to extract DOCX media for visual review: %s", exc)
    return images


async def _resolve_provider_targets(context: Any, provider_name: str = "", session_id: str = "") -> list[Any]:
    """Resolve provider objects across AstrBot provider-manager variants."""

    targets: list[Any] = []
    seen: set[int] = set()

    async def add(value: Any) -> None:
        if value is None:
            return
        if inspect.isawaitable(value):
            try:
                value = await value
            except Exception:
                return
        if value is None:
            return
        marker = id(value)
        if marker not in seen:
            seen.add(marker)
            targets.append(value)

    if context is None:
        return targets
    for method_name in (
        "get_provider_by_name", "get_provider_by_id", "get_provider",
        "get_llm_provider", "get_model_provider", "get_using_provider",
    ):
        method = getattr(context, method_name, None)
        if not callable(method):
            continue
        if method_name == "get_using_provider" and session_id:
            argument_sets = ((),)
            try:
                await add(method(umo=session_id))
                if targets:
                    continue
            except TypeError:
                pass
            except Exception:
                continue
        elif provider_name and method_name != "get_using_provider":
            argument_sets = ((provider_name,), ())
        else:
            argument_sets = ((),)
        for args in argument_sets:
            try:
                await add(method(*args))
                if targets:
                    break
            except TypeError:
                continue
            except Exception:
                break
    for name in ("provider", "llm", "provider_manager", "providers", "llm_provider"):
        await add(getattr(context, name, None))
    await add(context)
    return targets


async def _call_vision_model(
    context: Any,
    image_path: Path,
    mime: str,
    prompt: str,
    *,
    provider_name: str = "",
    model_name: str = "",
    timeout: int = 60,
    session_id: str = "name-searcher",
) -> tuple[str, str]:
    """Ask an AstrBot model provider to inspect an image when configured.

    ``text_chat`` is the native AstrBot provider API; OpenAI-style ``chat`` /
    ``complete`` methods are also accepted so deployments using a custom
    provider can opt in without a hard dependency on one SDK.
    """

    try:
        raw = image_path.read_bytes()
    except OSError as exc:
        _log("debug", "Unable to read image for vision model: %s", exc)
        return "", ""
    if not raw or len(raw) > 16 * 1024 * 1024:
        return "", ""
    data_uri = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(raw).decode('ascii')}"
    image_payload = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_uri}}]
    model_kwargs = {"model": model_name} if model_name else {}

    # AstrBot >= 4.5.7 exposes llm_generate as the stable plugin API. Prefer
    # it so provider selection, retries and response handling stay under the
    # host's control. vision_provider is a provider ID when explicitly set.
    llm_generate = getattr(context, "llm_generate", None)
    if callable(llm_generate):
        provider_id = provider_name
        if not provider_id:
            get_provider_id = getattr(context, "get_current_chat_provider_id", None)
            if callable(get_provider_id):
                try:
                    resolved = get_provider_id(umo=session_id)
                    provider_id = await resolved if inspect.isawaitable(resolved) else resolved
                except TypeError:
                    try:
                        resolved = get_provider_id(session_id)
                        provider_id = await resolved if inspect.isawaitable(resolved) else resolved
                    except Exception:
                        provider_id = ""
                except Exception:
                    provider_id = ""
        if provider_id:
            try:
                try:
                    result = llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=[data_uri],
                        **model_kwargs,
                    )
                except TypeError:
                    result = llm_generate(chat_provider_id=provider_id, prompt=prompt, image_urls=[data_uri])
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=max(1, int(timeout)))
                text = _model_text(result)
                if text:
                    return text, "Context.llm_generate"
            except asyncio.TimeoutError:
                _log("debug", "Vision model timed out via llm_generate")
            except Exception as exc:
                _log("debug", "Vision model call failed via llm_generate: %s", exc)

    attempts = [
        {"prompt": prompt, "session_id": session_id, "image_urls": [data_uri], **model_kwargs},
        {"prompt": prompt, "session_id": session_id, "images": [data_uri], **model_kwargs},
        {"messages": [{"role": "user", "content": image_payload}], **model_kwargs},
        {"prompt": prompt, "image": data_uri, **model_kwargs},
        {"prompt": prompt, "image_path": str(image_path), **model_kwargs},
    ]
    method_names = ("text_chat", "vision", "chat", "chat_completion", "complete", "generate", "ask", "invoke", "call")
    targets = await _resolve_provider_targets(context, provider_name, session_id)
    for target in targets:
        for method_name in method_names:
            method = getattr(target, method_name, None)
            if not callable(method):
                continue
            for kwargs in attempts:
                try:
                    result = method(**kwargs)
                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(result, timeout=max(1, int(timeout)))
                    text = _model_text(result)
                    if text:
                        return text, f"{target.__class__.__name__}.{method_name}"
                except TypeError:
                    continue
                except asyncio.TimeoutError:
                    _log("debug", "Vision model timed out via %s", method_name)
                    break
                except Exception as exc:
                    _log("debug", "Vision model call failed via %s: %s", method_name, exc)
                    break
    return "", ""


async def _call_text_model(
    context: Any,
    prompt: str,
    *,
    provider_name: str = "",
    model_name: str = "",
    timeout: int = 60,
    session_id: str = "name-searcher-review",
) -> tuple[str, str]:
    """Call an AstrBot chat provider for recognition-result review."""

    model_kwargs = {"model": model_name} if model_name else {}
    llm_generate = getattr(context, "llm_generate", None)
    provider_id = provider_name
    if callable(llm_generate):
        if not provider_id:
            get_provider_id = getattr(context, "get_current_chat_provider_id", None)
            if callable(get_provider_id):
                try:
                    resolved = get_provider_id(umo=session_id)
                    provider_id = await resolved if inspect.isawaitable(resolved) else resolved
                except TypeError:
                    try:
                        resolved = get_provider_id(session_id)
                        provider_id = await resolved if inspect.isawaitable(resolved) else resolved
                    except Exception:
                        provider_id = ""
                except Exception:
                    provider_id = ""
        if provider_id:
            try:
                try:
                    result = llm_generate(chat_provider_id=provider_id, prompt=prompt, **model_kwargs)
                except TypeError:
                    result = llm_generate(chat_provider_id=provider_id, prompt=prompt)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=max(1, int(timeout)))
                text = _model_text(result)
                if text:
                    return text, "Context.llm_generate"
            except Exception as exc:
                _log("debug", "LLM review failed via llm_generate: %s", exc)
    targets = await _resolve_provider_targets(context, provider_name, session_id)
    for target in targets:
        method = getattr(target, "text_chat", None)
        if not callable(method):
            continue
        for kwargs in ({"prompt": prompt, **model_kwargs}, {"prompt": prompt, "session_id": session_id, **model_kwargs}):
            try:
                result = method(**kwargs)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=max(1, int(timeout)))
                text = _model_text(result)
                if text:
                    return text, f"{target.__class__.__name__}.text_chat"
            except TypeError:
                continue
            except Exception as exc:
                _log("debug", "LLM review provider failed: %s", exc)
                break
    return "", ""


def _download_failure_notice(record: Mapping[str, Any]) -> str:
    name = record.get("name")
    reason = str(record.get("reason") or "")
    if reason == FORWARD_UNRESOLVED_REASON:
        return f"文件“{name}”下载失败：合并转发的聊天记录里只能获取图片，非图片文件 QQ 不提供下载。请直接发送原文件。"
    if reason == "no_download_url":
        return f"文件“{name}”无法下载：QQ 没有提供它的下载地址（合并转发里的文件常见这种情况）。请直接发送原文件。"
    if reason == "too_large":
        return f"文件“{name}”超过单文件大小上限，未保存。"
    return f"文件“{name}”下载失败，请稍后重新发送原文件。"


def _review_notice(record: Mapping[str, Any]) -> str:
    if record.get("download_status") not in (None, "", "ok"):
        return _download_failure_notice(record)
    if record.get("llm_review_status") == "completed":
        return f"文件“{record.get('name')}”已完成 LLM 审核：{record.get('llm_review_text')}"
    return f"文件“{record.get('name')}”识别置信度较低，LLM 审核暂不可用。请重新发送更清晰或可解析的原文件。"


def _archive_limited_count(errors: Iterable[str]) -> int:
    errors = list(errors or [])
    if errors:
        match = re.match(r"^共 (\d+) 个成员", str(errors[0]))
        if match:
            return int(match.group(1))
    return sum(1 for item in errors if "未识别" in str(item) or "未展开" in str(item))


def _archive_notice(records: Iterable[Mapping[str, Any]], limit: int = 5) -> str:
    """Tell the user which archive members were not recognised and why."""

    lines: list[str] = []
    for record in records:
        errors = record.get("archive_errors") if isinstance(record.get("archive_errors"), list) else []
        if not errors:
            continue
        details = [str(item) for item in errors if not str(item).startswith("共 ")]
        count = max(_archive_limited_count(errors), sum(1 for item in details if "未识别" in item or "未展开" in item))
        title = record.get("name") or "未命名压缩包"
        lines.append(f"压缩包「{title}」有 {count} 个成员未识别：" if count else f"压缩包「{title}」有内容未能解压：")
        lines.extend(f"- {item}" for item in details[:limit])
        if len(details) > limit:
            lines.append(f"- ……另有 {len(details) - limit} 条，详见插件页文件详情")
    if not lines:
        return ""
    lines.append("可在插件配置调高“压缩包嵌套深度 / 成员数 / 解压总大小”上限，或先解压后再发送。")
    return "\n".join(lines)


def _encoding_notice(records: Iterable[Mapping[str, Any]]) -> str:
    affected = [record for record in records if _as_bool(record.get("encoding_issue", False))]
    if not affected:
        return ""
    lines = ["检测到以下文件存在非 UTF-8 中文编码或乱码，已自动转码后用于识别："]
    for record in affected:
        source_encoding = str(record.get("source_encoding") or "未知编码")
        repair = str(record.get("encoding_repair") or "")
        detail = f"{source_encoding} -> UTF-8"
        if repair:
            detail += f"，乱码修复 {repair}"
        lines.append(f"- {record.get('name') or '未命名文件'}（{detail}）")
    return "\n".join(lines)


def _format_wildcard_result(result: Mapping[str, Any], max_names: int = 30) -> str:
    query = str(result.get("query") or "")
    if not result.get("ok"):
        return str(result.get("message") or "通配符格式不正确")
    names = list(result.get("names") or [])
    if not names:
        return f"未找到匹配“{query}”的姓名。* 表示一个或多个字，? 表示一个字。"
    lines = [
        f"查找对象：{query}（通配符）",
        f"匹配姓名：{result.get('name_count', len(names))} 个，涉及 {result.get('file_count', 0)} 个文件",
    ]
    for index, group in enumerate(names[:max_names], 1):
        files = list(group.get("files") or [])
        file_names = list(dict.fromkeys(str(item.get("name") or "未命名文件") for item in files))
        shown = "、".join(file_names[:3]) + (f" 等 {len(file_names)} 个文件" if len(file_names) > 3 else "")
        if group.get("library"):
            shown = (shown + "；" if shown else "") + "人员库"
        lines.append(f"{index}. {group.get('name')}：{shown or '无文件'}")
    hidden = int(result.get("name_count") or len(names)) - min(len(names), max_names)
    if hidden > 0:
        lines.append(f"另有 {hidden} 个姓名未显示，请缩小范围（如 王小*）或在插件面板“查找对象”中查看全部。")
    lines.append("查看某人详情：/查找 完整姓名")
    return "\n".join(lines)


def _format_result(query: str, matches: list[dict[str, Any]], resource_base: str = "", hidden_partial: int = 0) -> str:
    if not matches:
        return f"未找到“{query}”。请确认姓名，或先发送包含名单的文档/图片。"
    top_score = float(matches[0].get("score", 0))
    ambiguous = len(matches) > 1 and top_score - float(matches[1].get("score", 0)) < 0.08
    lines = [f"查找对象：{query}", f"匹配数量：{len(matches)}"]
    for index, item in enumerate(matches[:8], 1):
        record = item.get("record") if isinstance(item.get("record"), Mapping) else {}
        details = []
        for key, value in record.items():
            if key in {"姓名", "name", "姓名/名称"} or value in (None, ""):
                continue
            details.append(f"{key}：{value}")
        source = item.get("source")
        if isinstance(source, Mapping) and source.get("id"):
            citation = _record_source(source)
        elif isinstance(source, Mapping) and source.get("name"):
            citation = f"{source.get('name')}"
        else:
            citation = "未知来源"
        lines.append(f"{index}. " + "；".join(details or [f"姓名：{record.get('姓名') or record.get('name') or query}"]))
        lines.append(f"来源：{citation}")
        if isinstance(source, Mapping) and source.get("id"):
            filename = source.get("name") or "未命名文件"
            base = str(resource_base or "").rstrip("/")
            resource = f"{base}/files/{urllib.parse.quote(str(source.get('id')), safe='')}/content" if base else f"文件编号 {source.get('id')}"
            lines.append(f"文件资源：{filename}；预览地址：{resource}")
    if ambiguous:
        lines.append("提示：存在多个相近结果，请补充班级、专业等信息后再查找。")
    if matches and all(item.get("strength") in {"partial", "fuzzy"} for item in matches):
        lines.append("提示：没有完全一致的姓名，以上为包含或近似匹配，请核对是否为同一人。")
    elif hidden_partial:
        lines.append(f"另有 {hidden_partial} 条包含或近似匹配（如更长的姓名）未显示。")
    return "\n".join(lines)


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose stable fields for the bundled panel without leaking local paths."""

    # Keep list responses small. Model evidence can contain hundreds of
    # kilobytes per file and is retained in the index for lookup, but must not
    # be serialized on every one-second panel refresh.
    public_fields = (
        "id", "name", "kind", "mime", "format", "size", "sha256",
        "source", "capture_count", "captured_at", "last_captured_at",
        "download_status", "reason", "extraction_status", "extraction_error",
        "review_status", "ocr_confidence", "ocr_engine", "image_clarity",
        "image_width", "image_height", "vision_status",
        "vision_models_completed", "vision_consensus_status", "llm_review_status", "model_review_status",
        "encoding_issue", "encoding_status", "source_encoding", "target_encoding",
        "encoding_repair", "sheet_count", "sheet_names", "archive_member_count",
        "archive_extraction_status", "archive_errors", "archive_limited_count", "vision_pages_reviewed", "vision_pages_total", "processing_status",
        "processing_updated_at", "processing_error", "upload_progress_id",
    )
    item = {key: record.get(key) for key in public_fields if key in record}
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    item["mime_type"] = record.get("mime") or "application/octet-stream"
    item["source_name"] = source.get("sender_name") or source.get("sender_id") or source.get("unified_msg_origin") or "未知来源"
    item["ocr_excerpt"] = str(record.get("ocr_text") or "")[:2000]
    return item


DOC_VIEWER_MAX_BYTES = 20 * 1024 * 1024


def _blob_payload(store: ArtifactStore, artifact_id: str) -> tuple[dict[str, Any], int]:
    """Return a captured file as base64 for the online document viewer.

    The AstrBot Plugin Page bridge only carries JSON, so the panel cannot get a
    Blob from the download route. The size cap keeps one bridge message sane.
    """

    record = store.get(artifact_id)
    if not record:
        return {"error": "文件不存在"}, 404
    path = store.path_for(record)
    if not path or not path.exists():
        return {"error": "文件内容不可用"}, 404
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"error": f"读取文件失败：{exc}"}, 500
    if size > DOC_VIEWER_MAX_BYTES:
        return {"error": f"文件超过 {DOC_VIEWER_MAX_BYTES // (1024 * 1024)} MB，无法在线预览，请下载后查看"}, 413
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"error": f"读取文件失败：{exc}"}, 500
    return {
        "name": str(record.get("name") or path.name),
        "mime": str(record.get("mime") or _mime_for(path)),
        "size": len(raw),
        "data_base64": base64.b64encode(raw).decode("ascii"),
    }, 200


PREVIEW_DIR_NAME = ".previews"
PREVIEW_MAX_SIDE = 1280
PREVIEW_JPEG_QUALITY = 78


def _preview_image_bytes(store: ArtifactStore, record: Mapping[str, Any], path: Path) -> Optional[bytes]:
    """Small cached JPEG for the panel preview.

    Phone screenshots are 1-3 MB; through the base64 JSON bridge and a
    Europe-to-China link that took seconds. A 1280px JPEG is ~150-300 KB and
    is cached next to the store, so the second open is just a file read.
    """

    cache = store.root / PREVIEW_DIR_NAME / f"{record.get('id')}.jpg"
    try:
        if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
            return cache.read_bytes()
    except OSError:
        pass
    try:
        from PIL import Image, ImageOps  # type: ignore

        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
            if image.mode not in {"RGB", "L"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                rgba = image.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=PREVIEW_JPEG_QUALITY, optimize=True, progressive=True)
            data = output.getvalue()
    except Exception as exc:
        _log("debug", "Preview resize failed for %s: %s", path, exc)
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(cache)
    except OSError:
        pass
    return data


def _preview_payload(store: ArtifactStore, artifact_id: str) -> tuple[dict[str, Any], int]:
    """Return an iframe-safe image preview without exposing a local path."""

    record = store.get(artifact_id)
    if not record:
        return {"error": "文件不存在"}, 404
    path = store.path_for(record)
    if not path or not path.exists():
        return {"error": "文件内容不可用"}, 404
    mime = str(record.get("mime") or _mime_for(path))
    if record.get("kind") != "image" and not mime.startswith("image/"):
        return {"error": "该格式仅提供信息预览和下载"}, 415
    raw = _preview_image_bytes(store, record, path)
    if raw is not None:
        mime = "image/jpeg"
    else:
        try:
            if path.stat().st_size > 12 * 1024 * 1024:
                return {"error": "图片过大，请下载后查看"}, 413
            raw = path.read_bytes()
        except OSError as exc:
            return {"error": f"读取预览失败：{exc}"}, 500
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return {"id": record.get("id"), "name": record.get("name"), "data_url": data_uri, "size": len(raw)}, 200


def _page_json_response(payload: Any, status_code: int = 200) -> Any:
    if callable(_astr_json_response):
        try:
            return _astr_json_response(payload, status_code=status_code)
        except TypeError:
            return _astr_json_response(payload)
    return payload if status_code < 400 else (payload, status_code)


def _page_error(message: str, status_code: int = 400) -> Any:
    if callable(_astr_error_response):
        return _astr_error_response(message, status_code=status_code)
    return _page_json_response({"status": "error", "message": message}, status_code)


async def _page_request_payload() -> dict[str, Any]:
    if _astr_web_request is None:
        return {}
    try:
        payload = await _astr_web_request.json(default={})
    except TypeError:
        try:
            payload = await _astr_web_request.json()
        except Exception:
            payload = {}
    except Exception:
        payload = {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _progress_public(progress: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(progress)
    records = item.get("records") if isinstance(item.get("records"), list) else []
    item["records"] = [
        {
            "id": record.get("id"),
            "name": record.get("name"),
            "kind": record.get("kind"),
            "download_status": record.get("download_status"),
            "extraction_status": record.get("extraction_status"),
            "ocr_confidence": record.get("ocr_confidence"),
            "vision_status": record.get("vision_status"),
            "vision_models_completed": record.get("vision_models_completed"),
            "encoding_issue": record.get("encoding_issue", False),
            "source_encoding": record.get("source_encoding"),
            "sheet_count": record.get("sheet_count"),
        }
        for record in records
        if isinstance(record, Mapping)
    ]
    return item


def _progress_text(progress: Optional[Mapping[str, Any]]) -> str:
    if not progress:
        return "当前没有文件捕获任务。"
    percent = int(progress.get("percent") or 0)
    completed = int(progress.get("completed") or 0)
    total = int(progress.get("total") or 0)
    status = str(progress.get("status") or "unknown")
    status_name = {
        "queued": "已排队", "collecting": "捕获窗口收集中", "running": "进行中", "paused": "已暂停",
        "completed": "已完成", "failed": "失败", "cancelled": "已取消",
    }.get(status, status)
    stage = str(progress.get("stage") or "")
    stage_name = {
        "collecting": "收集附件", "downloading": "下载文件", "extracting": "本地解析",
        "vision_review": "视觉模型审核", "consensus": "多模型共识", "paused": "已暂停",
        "llm_review": "LLM 审核", "persisting": "保存结果", "completed": "完成",
    }.get(stage, stage)
    current_file = str(progress.get("current_file") or "")
    current_model = str(progress.get("current_model") or "")
    records = progress.get("records") if isinstance(progress.get("records"), list) else []
    names = [str(item.get("name") or "未命名文件") for item in records if isinstance(item, Mapping)]
    content = "、".join(names) if names else "暂无"
    details = [status_name]
    if stage_name:
        details.append(stage_name)
    if current_file:
        details.append(current_file)
    if current_model:
        details.append(current_model)
    return f"文件捕获进度：{percent}%（{completed}/{total}，{' · '.join(details)}）\n已捕获内容：{content}"


def _panel_config_view(plugin: "NameSearcherPlugin") -> dict[str, Any]:
    """Config as shown to panel clients: secrets removed, locked keys flagged."""

    payload = {key: value for key, value in plugin.config.items() if key not in PANEL_SECRET_KEYS}
    payload["panel_access_token_set"] = bool(str(plugin.config.get("panel_access_token") or "").strip())
    payload["panel_locked_keys"] = sorted(PANEL_LOCKED_KEYS)
    payload["plugin_version"] = PLUGIN_VERSION
    payload["vision_provider_options"] = _provider_options(plugin.context)
    payload["llm_review_provider_options"] = payload["vision_provider_options"]
    return payload


def _apply_panel_config(plugin: "NameSearcherPlugin", payload: Mapping[str, Any]) -> list[str]:
    """Apply panel edits; returns the locked keys that were ignored."""

    ignored = [key for key in PANEL_LOCKED_KEYS if key in payload]
    for key in DEFAULT_CONFIG:
        if key in PANEL_LOCKED_KEYS:
            continue
        if key in payload:
            plugin.config[key] = payload[key]
    if "retention_days" in payload:
        plugin.config["retention_days"] = payload["retention_days"]
    if plugin.config.get("clear_downloaded_files"):
        plugin.store.clear()
        plugin.config["clear_downloaded_files"] = False
    plugin.people.reload()
    plugin._persist_config()
    if ignored:
        _log("warning", "NameSearcher panel tried to change locked keys: %s", ", ".join(sorted(ignored)))
    return sorted(ignored)


class _FallbackPanelApp:
    """Dependency-free ASGI panel used when Starlette is unavailable."""

    def __init__(self, plugin: "NameSearcherPlugin") -> None:
        self.plugin = plugin
        self.webui = PLUGIN_PAGE_ROOT if (PLUGIN_PAGE_ROOT / "index.html").exists() else PLUGIN_ROOT / "webui"

    async def _body(self, receive: Any) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    async def _respond(
        self,
        send: Any,
        body: bytes,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        headers: Optional[list[tuple[bytes, bytes]]] = None,
    ) -> None:
        response_headers = [
            (b"content-type", content_type.encode("latin-1")),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        response_headers.extend(headers or [])
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send: Any, payload: Any, status: int = 200) -> None:
        await self._respond(send, json.dumps(payload, ensure_ascii=False).encode("utf-8"), status)

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        path = str(scope.get("path") or "/")
        mount_path = "/" + str(self.plugin.config.get("panel_mount_path") or "/api/name-searcher").strip("/")
        if path == mount_path:
            path = "/"
        elif path.startswith(mount_path + "/"):
            path = path[len(mount_path):]
        elif path.startswith("/api/"):
            path = path[4:]
        method = str(scope.get("method") or "GET").upper()
        try:
            if method == "GET" and path in {"/", "/index.html"}:
                index = self.webui / "index.html"
                if not index.exists():
                    await self._respond(send, "面板文件不存在".encode("utf-8"), 404, "text/plain; charset=utf-8")
                    return
                await self._respond(send, index.read_bytes(), content_type="text/html; charset=utf-8")
                return
            if method == "GET" and path == "/files":
                self.plugin._schedule_pending_uploads()
                files = [_public_record(item) for item in self.plugin.store.list()]
                await self._json(send, {"files": files, "count": len(files)})
                return
            if method == "GET" and path == "/progress":
                self.plugin._schedule_pending_uploads()
                latest = self.plugin.latest_progress()
                await self._json(send, {"progress": latest, "runs": [_progress_public(item) for item in self.plugin._progress_runs.values()]})
                return
            progress_match = re.fullmatch(r"/progress/([^/]+)", path)
            if method == "GET" and progress_match:
                self.plugin._schedule_pending_uploads()
                progress = self.plugin.get_progress(urllib.parse.unquote(progress_match.group(1)))
                await self._json(send, {"progress": progress}, 200 if progress else 404)
                return
            preview_match = re.fullmatch(r"/files/([^/]+)/preview", path)
            if method == "GET" and preview_match:
                payload, status = await asyncio.to_thread(
                    _preview_payload,
                    self.plugin.store,
                    urllib.parse.unquote(preview_match.group(1)),
                )
                await self._json(send, payload, status)
                return
            if method in {"POST", "DELETE"} and path in {"/files/bulk-delete", "/files/delete"}:
                if not self.plugin.config.get("allow_panel_delete", True):
                    await self._json(send, {"error": "panel delete disabled"}, 403)
                    return
                raw = await self._body(receive)
                payload = json.loads(raw or b"{}")
                ids = payload.get("ids", []) if isinstance(payload, Mapping) else []
                ids = [ids] if isinstance(ids, str) else ids
                deleted = self.plugin.store.delete(ids)
                await self._json(send, {"deleted": deleted, "count": len(deleted)})
                return
            if method == "POST" and path == "/files/clear":
                if not self.plugin.config.get("allow_panel_delete", True):
                    await self._json(send, {"error": "panel delete disabled"}, 403)
                    return
                deleted = self.plugin.store.clear()
                await self._json(send, {"deleted": deleted, "count": deleted})
                return
            if method == "POST" and path == "/stop":
                await self._json(send, await self.plugin.stop_all())
                return
            if method == "POST" and path == "/find":
                raw = await self._body(receive)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                query = str(payload.get("query") or payload.get("q") or "") if isinstance(payload, Mapping) else ""
                await self._json(send, await asyncio.to_thread(self.plugin.find_objects, query))
                return
            if method == "POST" and path == "/files/clear-failed":
                if not self.plugin.config.get("allow_panel_delete", True):
                    await self._json(send, {"error": "panel delete disabled"}, 403)
                    return
                deleted = self.plugin.store.clear_failed()
                await self._json(send, {"deleted": deleted, "count": len(deleted)})
                return
            if method == "POST" and path == "/files/upload":
                payload = json.loads(await self._body(receive) or b"{}")
                result = await self.plugin.process_panel_upload(payload if isinstance(payload, Mapping) else {})
                await self._json(send, result, 200 if result.get("ok") else 400)
                return
            if path == "/config" and method == "GET":
                await self._json(send, _panel_config_view(self.plugin))
                return
            if path == "/config" and method in {"PUT", "PATCH", "POST"}:
                payload = json.loads(await self._body(receive) or b"{}")
                ignored = _apply_panel_config(self.plugin, payload) if isinstance(payload, Mapping) else []
                view = _panel_config_view(self.plugin)
                view["ignored_keys"] = ignored
                await self._json(send, view)
                return
            table_match = re.fullmatch(r"/files/([^/]+)/table", path)
            if method == "POST" and table_match:
                await self._json(send, await self.plugin.image_to_table(urllib.parse.unquote(table_match.group(1))))
                return
            file_match = re.fullmatch(r"/files/([^/]+)(?:/content)?", path)
            if method == "GET" and file_match:
                record = self.plugin.store.get(urllib.parse.unquote(file_match.group(1)))
                if not record:
                    await self._respond(send, "文件不存在".encode("utf-8"), 404, "text/plain; charset=utf-8")
                    return
                file_path = self.plugin.store.path_for(record)
                if not file_path or not file_path.exists():
                    await self._json(send, _public_record(record))
                    return
                filename = urllib.parse.quote(str(record.get("name") or "attachment"))
                await self._respond(
                    send,
                    file_path.read_bytes(),
                    content_type=str(record.get("mime") or "application/octet-stream"),
                    headers=[(b"content-disposition", f"inline; filename*=UTF-8''{filename}".encode("ascii"))],
                )
                return
            await self._json(send, {"error": "not found"}, 404)
        except Exception as exc:
            _log("warning", "Fallback panel request failed: %s", exc)
            await self._json(send, {"error": str(exc)}, 500)


class _PanelAuthGuard:
    """Token gate for the legacy ASGI panel.

    The Plugin Page APIs are authenticated by AstrBot itself; this legacy app
    may be mounted straight onto the host server, so it must check access on
    its own.  With no ``panel_access_token`` configured every API call is
    refused; only the static page shell (which holds no data) is served.
    """

    STATIC_PATHS = {"/", "/index.html"}

    def __init__(self, plugin: "NameSearcherPlugin", app: Any) -> None:
        self.plugin = plugin
        self.app = app

    def _relative_path(self, path: str) -> str:
        mount_path = "/" + str(self.plugin.config.get("panel_mount_path") or "/api/name-searcher").strip("/")
        if path == mount_path:
            return "/"
        if path.startswith(mount_path + "/"):
            return path[len(mount_path):]
        return path

    @staticmethod
    def _supplied_token(scope: Mapping[str, Any]) -> str:
        headers = {
            bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if headers.get("x-namesearcher-token"):
            return headers["x-namesearcher-token"].strip()
        query = urllib.parse.parse_qs(bytes(scope.get("query_string") or b"").decode("latin-1"))
        values = query.get("token") or []
        return values[0].strip() if values else ""

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = self._relative_path(str(scope.get("path") or "/"))
        method = str(scope.get("method") or "GET").upper()
        if method == "GET" and path in self.STATIC_PATHS:
            await self.app(scope, receive, send)
            return
        expected = str(self.plugin.config.get("panel_access_token") or "").strip()
        supplied = self._supplied_token(scope)
        if expected and supplied and hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
            await self.app(scope, receive, send)
            return
        message = (
            "旧版面板接口未启用：请在 AstrBot 插件配置中设置 panel_access_token，或使用 AstrBot 插件页。"
            if not expected
            else "访问令牌无效"
        )
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403 if not expected else 401,
            "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body})


def _build_web_app(plugin: "NameSearcherPlugin") -> Any:
    """Build the legacy panel app, always wrapped in the token guard."""

    return _PanelAuthGuard(plugin, _build_unguarded_web_app(plugin))


def _build_unguarded_web_app(plugin: "NameSearcherPlugin") -> Any:
    """Build an optional Starlette application for the AstrBot panel."""

    try:
        from starlette.applications import Starlette  # type: ignore
        from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response  # type: ignore
        from starlette.routing import Route  # type: ignore
    except Exception:
        return _FallbackPanelApp(plugin)

    webui = PLUGIN_ROOT / "webui"

    async def index(_request: Any) -> Response:
        path = PLUGIN_PAGE_ROOT / "index.html"
        if not path.exists():
            path = webui / "index.html"
        if path.exists():
            return FileResponse(path)
        return PlainTextResponse("NameSearcher web UI is not installed", status_code=404)

    async def list_files(_request: Any) -> JSONResponse:
        plugin._schedule_pending_uploads()
        records = [_public_record(item) for item in plugin.store.list()]
        return JSONResponse({"files": records, "count": len(records)})

    async def progress(_request: Any) -> JSONResponse:
        plugin._schedule_pending_uploads()
        latest = plugin.latest_progress()
        with plugin._progress_lock:
            runs = [_progress_public(item) for item in plugin._progress_runs.values()]
        return JSONResponse({"progress": latest, "runs": runs})

    async def progress_item(request: Any) -> JSONResponse:
        plugin._schedule_pending_uploads()
        progress = plugin.get_progress(request.path_params.get("progress_id", ""))
        return JSONResponse({"progress": progress}, status_code=200 if progress else 404)

    async def get_file(request: Any) -> Response:
        record = plugin.store.get(request.path_params.get("artifact_id", ""))
        if not record:
            return PlainTextResponse("文件不存在", status_code=404)
        path = plugin.store.path_for(record)
        if not path or not path.exists():
            return JSONResponse(_public_record(record), status_code=200)
        return FileResponse(path, media_type=record.get("mime") or "application/octet-stream", filename=record.get("name"))

    async def preview_file(request: Any) -> JSONResponse:
        payload, status = await asyncio.to_thread(_preview_payload, plugin.store, request.path_params.get("artifact_id", ""))
        return JSONResponse(payload, status_code=status)

    async def delete_files(request: Any) -> JSONResponse:
        if not plugin.config.get("allow_panel_delete", True):
            return JSONResponse({"error": "panel delete disabled"}, status_code=403)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        ids = payload.get("ids", []) if isinstance(payload, Mapping) else []
        if isinstance(ids, str):
            ids = [ids]
        deleted = plugin.store.delete(ids)
        # Keep both a list (useful to clients) and a count (used by older UIs).
        return JSONResponse({"deleted": deleted, "count": len(deleted)})

    async def cleanup(_request: Any) -> JSONResponse:
        if not plugin.config.get("allow_panel_delete", True):
            return JSONResponse({"error": "panel delete disabled"}, status_code=403)
        deleted = plugin.store.clear()
        return JSONResponse({"deleted": deleted, "count": deleted})

    async def stop_all(_request: Any) -> JSONResponse:
        return JSONResponse(await plugin.stop_all())

    async def find_objects(request: Any) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        query = str(payload.get("query") or payload.get("q") or "") if isinstance(payload, Mapping) else ""
        return JSONResponse(await asyncio.to_thread(plugin.find_objects, query))

    async def cleanup_failed(_request: Any) -> JSONResponse:
        if not plugin.config.get("allow_panel_delete", True):
            return JSONResponse({"error": "panel delete disabled"}, status_code=403)
        deleted = plugin.store.clear_failed()
        return JSONResponse({"deleted": deleted, "count": len(deleted)})

    async def upload_files(request: Any) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        result = await plugin.process_panel_upload(payload if isinstance(payload, Mapping) else {})
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    async def table(request: Any) -> JSONResponse:
        result = await plugin.image_to_table(request.path_params.get("artifact_id", ""))
        return JSONResponse(result)

    async def config(request: Any) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse(_panel_config_view(plugin))
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        ignored = _apply_panel_config(plugin, payload) if isinstance(payload, Mapping) else []
        view = _panel_config_view(plugin)
        view["ignored_keys"] = ignored
        return JSONResponse(view)

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/index.html", index, methods=["GET"]),
        # The short paths are the public contract when this app is mounted
        # under /api/name-searcher.  /api/* aliases preserve compatibility
        # with hosts that mount the app at the site root.
        Route("/files", list_files, methods=["GET"]),
        Route("/progress", progress, methods=["GET"]),
        Route("/progress/{progress_id}", progress_item, methods=["GET"]),
        Route("/files/bulk-delete", delete_files, methods=["POST", "DELETE"]),
        Route("/files/delete", delete_files, methods=["POST", "DELETE"]),
        Route("/files/clear", cleanup, methods=["POST"]),
        Route("/files/clear-failed", cleanup_failed, methods=["POST"]),
        Route("/stop", stop_all, methods=["POST"]),
        Route("/find", find_objects, methods=["POST"]),
        Route("/files/upload", upload_files, methods=["POST"]),
        Route("/files/{artifact_id}/table", table, methods=["POST"]),
        Route("/files/{artifact_id}/preview", preview_file, methods=["GET"]),
        Route("/files/{artifact_id}/content", get_file, methods=["GET"]),
        Route("/files/{artifact_id}", get_file, methods=["GET"]),
        Route("/config", config, methods=["GET", "PUT", "PATCH", "POST"]),
        Route("/api/files", list_files, methods=["GET"]),
        Route("/api/progress", progress, methods=["GET"]),
        Route("/api/progress/{progress_id}", progress_item, methods=["GET"]),
        Route("/api/files/bulk-delete", delete_files, methods=["POST", "DELETE"]),
        Route("/api/files/delete", delete_files, methods=["POST", "DELETE"]),
        Route("/api/files/clear", cleanup, methods=["POST"]),
        Route("/api/files/clear-failed", cleanup_failed, methods=["POST"]),
        Route("/api/stop", stop_all, methods=["POST"]),
        Route("/api/find", find_objects, methods=["POST"]),
        Route("/api/files/upload", upload_files, methods=["POST"]),
        Route("/api/files/{artifact_id}/table", table, methods=["POST"]),
        Route("/api/files/{artifact_id}/preview", preview_file, methods=["GET"]),
        Route("/api/files/{artifact_id}/content", get_file, methods=["GET"]),
        Route("/api/files/{artifact_id}", get_file, methods=["GET"]),
        Route("/api/config", config, methods=["GET", "PUT", "PATCH", "POST"]),
    ]
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Public query portal (v1.11.0): a website asks for a pairing code, the user
# sends it to the bot, the bot replies with a verification code that only the
# browser holding that pairing challenge can redeem for a temporary session.
# ---------------------------------------------------------------------------

PORTAL_PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PORTAL_PAIR_TTL_SECONDS = 600
PORTAL_MAX_VERIFY_ATTEMPTS = 5
PORTAL_CHALLENGES_PER_IP = 12  # per 10 minutes
PORTAL_CLAIMS_PER_USER = 6  # per 10 minutes
PORTAL_RATE_WINDOW = 600
PORTAL_MAX_OPEN_CHALLENGES = 500
PORTAL_FINDS_PER_MINUTE = 20  # per session
PORTAL_DOWNLOADS_PER_10_MIN = 60  # per session
PORTAL_FIND_CONCURRENCY = 2
PORTAL_BAN_BURST = 30  # requests from one IP within PORTAL_BAN_WINDOW seconds -> permanent ban
PORTAL_BAN_WINDOW = 10


def _portal_random(length: int, alphabet: str) -> str:
    import secrets

    return "".join(secrets.choice(alphabet) for _ in range(length))


def _portal_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _portal_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PortalAuth:
    """In-memory pairing / verification / session state for the public portal.

    Nothing secret is persisted: a restart simply invalidates open challenges
    and sessions.  User profiles and the audit trail are written separately.
    """

    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._challenges: dict[str, dict[str, Any]] = {}
        self._pairs: dict[str, str] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window: float = PORTAL_RATE_WINDOW) -> bool:
        with self._lock:
            return self._rate_ok(key, limit, window)

    def _rate_ok(self, key: str, limit: int, window: float = PORTAL_RATE_WINDOW) -> bool:
        now = self._clock()
        hits = [stamp for stamp in self._hits.get(key, []) if now - stamp < window]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def prune(self) -> None:
        now = self._clock()
        with self._lock:
            for challenge_id, item in list(self._challenges.items()):
                if now > item["expires"]:
                    self._challenges.pop(challenge_id, None)
                    self._pairs.pop(item["pair_code"], None)
            for token_hash, session in list(self._sessions.items()):
                if now > session["expires"]:
                    self._sessions.pop(token_hash, None)
            for key, hits in list(self._hits.items()):
                if not any(now - stamp < PORTAL_RATE_WINDOW for stamp in hits):
                    self._hits.pop(key, None)

    def create_challenge(self, client: str) -> dict[str, Any]:
        self.prune()
        with self._lock:
            if not self._rate_ok(f"ip:{client}", PORTAL_CHALLENGES_PER_IP):
                return {"ok": False, "error": "请求太频繁，请稍后再试", "status": 429}
            if len(self._challenges) >= PORTAL_MAX_OPEN_CHALLENGES:
                return {"ok": False, "error": "当前申请人数过多，请稍后再试", "status": 503}
            challenge_id = _portal_token()
            pair_code = _portal_random(6, PORTAL_PAIR_ALPHABET)
            while pair_code in self._pairs:
                pair_code = _portal_random(6, PORTAL_PAIR_ALPHABET)
            now = self._clock()
            self._challenges[_portal_digest(challenge_id)] = {
                "pair_code": pair_code,
                "client": client,
                "created": now,
                "expires": now + PORTAL_PAIR_TTL_SECONDS,
                "user": None,
                "code_hash": "",
                "attempts": 0,
            }
            self._pairs[pair_code] = _portal_digest(challenge_id)
            return {"ok": True, "challenge": challenge_id, "pair_code": pair_code, "expires_in": PORTAL_PAIR_TTL_SECONDS}

    def challenge_state(self, challenge_id: str) -> dict[str, Any]:
        self.prune()
        with self._lock:
            item = self._challenges.get(_portal_digest(str(challenge_id or "")))
            if not item:
                return {"ok": False, "error": "识别码已过期，请刷新获取新的识别码", "status": 410}
            return {
                "ok": True,
                "pair_code": item["pair_code"],
                "claimed": bool(item["user"]),
                "expires_in": max(0, int(item["expires"] - self._clock())),
            }

    def claim(self, pair_code: str, user: Mapping[str, Any]) -> dict[str, Any]:
        """Bind a pairing code to the chat user and mint the verification code."""

        self.prune()
        code_key = re.sub(r"[^A-Za-z0-9]", "", str(pair_code or "")).upper()
        user_id = str(user.get("user_id") or "")
        with self._lock:
            if not self._rate_ok(f"user:{user_id}", PORTAL_CLAIMS_PER_USER):
                return {"ok": False, "error": "申请太频繁，请 10 分钟后再试"}
            challenge_hash = self._pairs.get(code_key)
            item = self._challenges.get(challenge_hash or "")
            if not item:
                return {"ok": False, "error": "识别码无效或已过期，请在查询页刷新后重新获取"}
            if item["user"] and str(item["user"].get("user_id")) != user_id:
                return {"ok": False, "error": "这个识别码已被其他人申请，请在查询页刷新后重新获取"}
            verify_code = _portal_random(6, "0123456789")
            item["user"] = dict(user)
            item["code_hash"] = _portal_digest(verify_code)
            item["attempts"] = 0
            return {"ok": True, "code": verify_code, "pair_code": code_key, "expires_in": max(0, int(item["expires"] - self._clock()))}

    def verify(self, challenge_id: str, code: str, session_minutes: float) -> dict[str, Any]:
        self.prune()
        challenge_hash = _portal_digest(str(challenge_id or ""))
        with self._lock:
            item = self._challenges.get(challenge_hash)
            if not item:
                return {"ok": False, "error": "识别码已过期，请刷新获取新的识别码", "status": 410}
            if not item["user"] or not item["code_hash"]:
                return {"ok": False, "error": "请先把识别码发给机器人获取验证码", "status": 400}
            item["attempts"] += 1
            if not hmac.compare_digest(item["code_hash"], _portal_digest(re.sub(r"\D", "", str(code or "")))):
                if item["attempts"] >= PORTAL_MAX_VERIFY_ATTEMPTS:
                    self._challenges.pop(challenge_hash, None)
                    self._pairs.pop(item["pair_code"], None)
                    return {"ok": False, "error": "验证码错误次数过多，请刷新获取新的识别码", "status": 410}
                left = PORTAL_MAX_VERIFY_ATTEMPTS - item["attempts"]
                return {"ok": False, "error": f"验证码不正确，还可以尝试 {left} 次", "status": 403}
            self._challenges.pop(challenge_hash, None)
            self._pairs.pop(item["pair_code"], None)
            token = _portal_token()
            ttl = max(1.0, float(session_minutes or 30)) * 60
            now = self._clock()
            self._sessions[_portal_digest(token)] = {
                "user": dict(item["user"]),
                "created": now,
                "expires": now + ttl,
                "files": set(),
                "finds": 0,
            }
            return {"ok": True, "token": token, "expires_in": int(ttl), "user": _portal_public_user(item["user"])}

    def session(self, token: str) -> Optional[dict[str, Any]]:
        self.prune()
        with self._lock:
            return self._sessions.get(_portal_digest(str(token or "")))

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_portal_digest(str(token or "")), None)

    def revoke_user(self, user_id: str) -> int:
        with self._lock:
            doomed = [key for key, session in self._sessions.items() if str(session["user"].get("user_id")) == str(user_id)]
            for key in doomed:
                self._sessions.pop(key, None)
            return len(doomed)

    def active_sessions(self) -> list[dict[str, Any]]:
        self.prune()
        now = self._clock()
        with self._lock:
            return [
                {**_portal_public_user(session["user"]), "expires_in": int(session["expires"] - now), "finds": int(session.get("finds") or 0)}
                for session in self._sessions.values()
            ]


def _portal_audit_id(line: str) -> str:
    return hashlib.sha1(str(line).strip().encode("utf-8")).hexdigest()[:16]


def _portal_public_user(user: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(user.get("user_id") or ""),
        "nickname": str(user.get("nickname") or ""),
        "chat_type": str(user.get("chat_type") or ""),
        "group_id": str(user.get("group_id") or ""),
    }


def _portal_strip_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Public view of a find result: names, file ids/names and the matched line only."""

    names = []
    for group in result.get("names") or []:
        files = [
            {"id": str(item.get("id") or ""), "name": str(item.get("name") or "未命名文件"), "line": str(item.get("line") or "")[:200], "kind": item.get("kind")}
            for item in group.get("files") or []
            if item.get("id")
        ]
        names.append({"name": str(group.get("name") or ""), "files": files})
    return {
        "ok": bool(result.get("ok", True)),
        "query": str(result.get("query") or ""),
        "wildcard": bool(result.get("wildcard")),
        "names": names,
        "name_count": int(result.get("name_count") or len(names)),
        "file_count": int(result.get("file_count") or 0),
        "truncated": int(result.get("truncated") or 0),
        "message": str(result.get("message") or ""),
    }


class PortalServer:
    """Tiny aiohttp server exposing only the portal endpoints (behind a reverse proxy)."""

    def __init__(self, plugin: "NameSearcherPlugin") -> None:
        self.plugin = plugin
        self.runner: Any = None
        self.bound: tuple[str, int] = ("", 0)
        self._find_slots: Optional[asyncio.Semaphore] = None

    @property
    def running(self) -> bool:
        return self.runner is not None

    def _origins(self) -> set[str]:
        return {item.rstrip("/") for item in _parse_allowed_ids(self.plugin.config.get("portal_allowed_origins", ""))}

    def _cors(self, request: Any) -> dict[str, str]:
        origin = str(request.headers.get("Origin") or "").rstrip("/")
        allowed = self._origins()
        if origin and ("*" in allowed or origin in allowed):
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            }
        return {"Vary": "Origin"}

    def _client(self, request: Any) -> str:
        forwarded = str(request.headers.get("X-Real-IP") or request.headers.get("CF-Connecting-IP") or "").split(",")[0].strip()
        return forwarded or str(request.remote or "")

    def _json(self, request: Any, payload: Mapping[str, Any], status: int = 200) -> Any:
        from aiohttp import web

        body = {key: value for key, value in payload.items() if key != "status"}
        return web.json_response(body, status=int(payload.get("status") or status), headers={**self._cors(request), "Cache-Control": "no-store"}, dumps=lambda data: json.dumps(data, ensure_ascii=False))

    def _token(self, request: Any) -> str:
        header = str(request.headers.get("Authorization") or "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return str(request.query.get("token") or "")

    async def _payload(self, request: Any) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def start(self) -> None:
        from aiohttp import web

        host = str(self.plugin.config.get("portal_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(self.plugin.config.get("portal_port") or 6197)
        app = web.Application(client_max_size=64 * 1024, middlewares=[self._guard])
        app.router.add_route("OPTIONS", "/{tail:.*}", self.options)
        app.router.add_get("/portal/health", self.health)
        app.router.add_post("/portal/challenge", self.challenge)
        app.router.add_post("/portal/challenge/state", self.challenge_state)
        app.router.add_post("/portal/verify", self.verify)
        app.router.add_get("/portal/session", self.session_info)
        app.router.add_post("/portal/logout", self.logout)
        app.router.add_post("/portal/find", self.find)
        app.router.add_get("/portal/files/{artifact_id}/content", self.content)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        self.bound = (host, port)
        _log("info", "NameSearcher portal listening on %s:%s", host, port)

    @property
    def _guard(self) -> Any:
        from aiohttp import web

        @web.middleware
        async def guard(request: Any, handler: Any) -> Any:
            client = self._client(request)
            if client and self.plugin.portal_is_banned(client):
                return self._json(request, {"ok": False, "error": "该 IP 因访问异常已被封禁，请联系管理员", "status": 403})
            if client and request.method != "OPTIONS" and not self.plugin.portal.allow(f"burst:{client}", PORTAL_BAN_BURST, PORTAL_BAN_WINDOW):
                self.plugin.portal_ban(client, f"{PORTAL_BAN_WINDOW} 秒内请求超过 {PORTAL_BAN_BURST} 次")
                return self._json(request, {"ok": False, "error": "该 IP 因访问异常已被封禁，请联系管理员", "status": 403})
            return await handler(request)

        return guard

    async def stop(self) -> None:
        runner, self.runner = self.runner, None
        if runner is not None:
            await runner.cleanup()

    async def options(self, request: Any) -> Any:
        from aiohttp import web

        return web.Response(status=204, headers=self._cors(request))

    async def health(self, request: Any) -> Any:
        return self._json(request, {"ok": True, "version": PLUGIN_VERSION, "session_minutes": self.plugin._portal_session_minutes()})

    async def challenge(self, request: Any) -> Any:
        result = self.plugin.portal.create_challenge(self._client(request))
        if result.get("ok"):
            result["bot_command"] = f"/验证码 {result['pair_code']}"
        return self._json(request, result)

    async def challenge_state(self, request: Any) -> Any:
        data = await self._payload(request)
        return self._json(request, self.plugin.portal.challenge_state(str(data.get("challenge") or "")))

    async def verify(self, request: Any) -> Any:
        data = await self._payload(request)
        result = self.plugin.portal.verify(str(data.get("challenge") or ""), str(data.get("code") or ""), self.plugin._portal_session_minutes())
        if result.get("ok"):
            self.plugin._portal_audit("login", result.get("user") or {}, client=self._client(request))
        return self._json(request, result)

    def _session_or_error(self, request: Any) -> tuple[Optional[dict[str, Any]], Any]:
        session = self.plugin.portal.session(self._token(request))
        if session is None:
            return None, self._json(request, {"ok": False, "error": "登录已过期，请重新获取验证码", "status": 401})
        return session, None

    async def session_info(self, request: Any) -> Any:
        session, error = self._session_or_error(request)
        if error is not None:
            return error
        info = {"ok": True, "user": _portal_public_user(session["user"]), "expires_in": int(session["expires"] - time.time())}
        max_finds = self.plugin._portal_session_max_finds()
        if max_finds:
            info["finds_left"] = max(0, max_finds - int(session.get("finds") or 0))
        return self._json(request, info)

    async def logout(self, request: Any) -> Any:
        self.plugin.portal.revoke(self._token(request))
        return self._json(request, {"ok": True})

    async def find(self, request: Any) -> Any:
        session, error = self._session_or_error(request)
        if error is not None:
            return error
        user_key = str(session["user"].get("user_id") or "")
        max_finds = self.plugin._portal_session_max_finds()
        if max_finds and int(session.get("finds") or 0) >= max_finds:
            return self._json(request, {"ok": False, "error": f"本次登录最多可查询 {max_finds} 次，已用完，请重新获取验证码登录", "status": 429, "finds_left": 0})
        if not self.plugin.portal.allow(f"find:{user_key}", PORTAL_FINDS_PER_MINUTE, 60):
            return self._json(request, {"ok": False, "error": "查询太频繁，请一分钟后再试", "status": 429})
        data = await self._payload(request)
        query = str(data.get("query") or "").strip()[:64]
        if not query:
            return self._json(request, {"ok": False, "error": "请输入要查找的姓名", "status": 400})
        session["finds"] = int(session.get("finds") or 0) + 1
        if self._find_slots is None:
            self._find_slots = asyncio.Semaphore(PORTAL_FIND_CONCURRENCY)
        async with self._find_slots:
            result = _portal_strip_result(await asyncio.to_thread(self.plugin.find_objects, query))
        for group in result["names"]:
            for item in group["files"]:
                session["files"].add(item["id"])
        self.plugin._portal_audit("find", session["user"], query=query, client=self._client(request), names=result["name_count"])
        if max_finds:
            result["finds_left"] = max(0, max_finds - int(session["finds"]))
        return self._json(request, result)

    async def content(self, request: Any) -> Any:
        from aiohttp import web

        session, error = self._session_or_error(request)
        if error is not None:
            return error
        artifact_id = str(request.match_info.get("artifact_id") or "")
        if not self.plugin.portal.allow(f"dl:{session['user'].get('user_id')}", PORTAL_DOWNLOADS_PER_10_MIN, 600):
            return self._json(request, {"ok": False, "error": "下载太频繁，请稍后再试", "status": 429})
        if artifact_id not in session["files"]:
            return self._json(request, {"ok": False, "error": "只能查看查询结果中的文件", "status": 403})
        record = self.plugin.store.get(artifact_id)
        path = self.plugin.store.path_for(record) if record else None
        if not record or not path or not path.exists():
            return self._json(request, {"ok": False, "error": "文件不存在", "status": 404})
        name = str(record.get("name") or path.name)
        inline = str(request.query.get("inline") or "") in {"1", "true"}
        disposition = "inline" if inline else "attachment"
        headers = {
            **self._cors(request),
            "Content-Type": str(record.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream"),
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{urllib.parse.quote(name)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if not inline:
            self.plugin._portal_audit("download", session["user"], file=name, file_id=artifact_id, client=self._client(request))
        return web.FileResponse(path, headers=headers)


@register(
    PLUGIN_ID,
    "OpenAI",
    "捕获聊天附件并按姓名查找，提供 OCR、图片转表格与文件管理面板",
    PLUGIN_VERSION,
)
class NameSearcherPlugin(Star):
    """AstrBot plugin entry point."""

    def __init__(self, context: Context, config: Optional[Mapping[str, Any]] = None) -> None:
        try:
            super().__init__(context)
        except TypeError:
            super().__init__()
        self.context = context
        self._config_backend = config
        self.config: dict[str, Any] = dict(DEFAULT_CONFIG)
        if isinstance(config, Mapping):
            self.config.update(config)
        raw_storage = str(self.config.get("storage_dir") or DEFAULT_CONFIG["storage_dir"]).strip()
        if raw_storage.replace("\\", "/") in LEGACY_STORAGE_DIRS:
            raw_storage = DEFAULT_CONFIG["storage_dir"]
        storage = _resolve_data_path(raw_storage)
        self.store = ArtifactStore(storage)
        if self.config.get("cleanup_on_start") or self.config.get("clear_downloaded_files"):
            self.store.clear()
            self.config["clear_downloaded_files"] = False
            self._persist_config()
        self.people = PeopleIndex(self.config, self.store)
        self._progress_lock = threading.RLock()
        self._progress_runs: dict[str, dict[str, Any]] = {}
        self._latest_progress_id = ""
        self._capture_window_lock = asyncio.Lock()
        self._capture_window_entries: list[tuple[Any, list[AttachmentCandidate]]] = []
        self._capture_window_task: Optional[asyncio.Task[Any]] = None
        self._capture_window_progress_id = ""
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._scheduled_upload_ids: set[str] = set()
        self._processing_upload_ids: set[str] = set()
        self._resume_scheduled = False
        self._terminating = False
        self._model_review_queue: list[str] = []
        self._model_review_task: Optional[asyncio.Task[Any]] = None
        self._model_review_resumed = False
        self._stop_epoch = 0
        self.portal = PortalAuth()
        self._portal_ban_cache: Optional[dict[str, Any]] = None
        self._portal_server = PortalServer(self)
        self._portal_lock = threading.RLock()
        self._portal_error = ""
        self.web_app = _build_web_app(self)
        self._register_page_apis()
        self._schedule_portal_sync()
        self._try_register_webui()
        self._schedule_pending_uploads()

    def _persist_config(self) -> None:
        """Write panel changes back through AstrBot's config object when possible."""

        backend = self._config_backend
        if isinstance(backend, MutableMapping):
            backend.update(self.config)
        for method_name in ("save_config", "save"):
            method = getattr(backend, method_name, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception as exc:
                _log("debug", "Unable to persist NameSearcher config: %s", exc)
            break

    def is_event_allowed(self, event: AstrMessageEvent) -> bool:
        return _event_is_allowed(event, self.config)

    def panel_base(self) -> str:
        configured = str(self.config.get("panel_public_url") or "").strip()
        if configured:
            return configured.rstrip("/")
        return "/" + str(self.config.get("panel_mount_path") or "/api/name-searcher").strip("/")

    def _track_background_task(self, coroutine: Any) -> Optional[asyncio.Task[Any]]:
        if self._terminating:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            return None
        try:
            task = asyncio.get_running_loop().create_task(coroutine)
        except RuntimeError:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            return None
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _schedule_pending_uploads(self) -> None:
        self._resume_model_reviews()
        if self._resume_scheduled or self._terminating or self._scheduled_upload_ids:
            return
        pending = [
            record for record in reversed(self.store.list())
            if (record.get("source") or {}).get("upload_source") == "plugin-page"
            and record.get("download_status") == "ok"
            and record.get("processing_status") in {"queued", "running"}
            and str(record.get("id") or "") not in self._scheduled_upload_ids
        ]
        if not pending:
            return
        pending_ids = {str(record.get("id") or "") for record in pending}
        self._scheduled_upload_ids.update(pending_ids)
        task = self._track_background_task(self._resume_pending_uploads(pending))
        self._resume_scheduled = task is not None
        if task is None:
            self._scheduled_upload_ids.difference_update(pending_ids)

    async def _resume_pending_uploads(self, pending: list[Mapping[str, Any]]) -> None:
        try:
            for record in pending:
                if self._terminating:
                    return
                await self._process_stored_upload(str(record.get("id") or ""))
        finally:
            self._scheduled_upload_ids.difference_update(
                str(record.get("id") or "") for record in pending
            )
            self._resume_scheduled = False

    def _recognition_completed(self, record: Mapping[str, Any]) -> bool:
        status = str(record.get("processing_status") or "")
        if status == "completed":
            return True
        if status in {"queued", "failed"}:
            return False
        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        if record.get("kind") == "archive" and not source.get("archive_id"):
            return False
        if record.get("download_status") != "ok":
            return False
        if record.get("extraction_status") not in {"extracted", "stored_only", "no_text"}:
            return False
        if _as_bool(self.config.get("vision_enabled", False)):
            if record.get("vision_status") not in {"extracted", "no_review_pages", "unavailable", "skipped"}:
                return False
        if _as_bool(self.config.get("llm_review_enabled", False)):
            if record.get("llm_review_status") not in {"completed", "unavailable", "skipped"}:
                return False
        return True

    def _start_progress(
        self,
        event: Any,
        candidates: list[AttachmentCandidate],
        *,
        status: str = "running",
        window_seconds: int = 0,
    ) -> str:
        progress_id = f"capture-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        source = _event_source(event)
        now = time.time()
        progress = {
            "id": progress_id,
            "status": status,
            "stage": "collecting" if status == "collecting" else "downloading",
            "stage_percent": 0,
            "total": len(candidates),
            "completed": 0,
            "percent": 0,
            "records": [],
            "source": source,
            "started_at": _now(),
            "updated_at": _now(),
            "current_file": "",
            "current_model": "",
            "queued_events": 1 if candidates else 0,
        }
        if window_seconds > 0:
            progress["window_started_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
            progress["window_ends_at"] = datetime.fromtimestamp(now + window_seconds, timezone.utc).isoformat()
        with self._progress_lock:
            self._progress_runs[progress_id] = progress
            self._latest_progress_id = progress_id
            # Keep the in-memory history bounded; records are persisted in the
            # artifact index and remain available from the files endpoint.
            while len(self._progress_runs) > 30:
                oldest = next(iter(self._progress_runs))
                self._progress_runs.pop(oldest, None)
        return progress_id

    def _update_progress(
        self,
        progress_id: str,
        *,
        record: Optional[Mapping[str, Any]] = None,
        advance: bool = True,
        status: str = "",
        stage: str = "",
        stage_percent: Optional[float] = None,
        current_file: Optional[str] = None,
        current_model: Optional[str] = None,
        total_delta: int = 0,
        queued_event_delta: int = 0,
        **extra: Any,
    ) -> Optional[dict[str, Any]]:
        with self._progress_lock:
            progress = self._progress_runs.get(progress_id)
            if progress is None:
                return None
            if record is not None:
                records = progress.setdefault("records", [])
                if not any(str(item.get("id")) == str(record.get("id")) for item in records if isinstance(item, Mapping)):
                    records.append(dict(record))
            if total_delta:
                progress["total"] = max(0, int(progress.get("total") or 0) + int(total_delta))
            if queued_event_delta:
                progress["queued_events"] = max(0, int(progress.get("queued_events") or 0) + int(queued_event_delta))
            progress["completed"] = min(
                int(progress.get("total") or 0),
                int(progress.get("completed") or 0) + (1 if advance else 0),
            )
            total = int(progress.get("total") or 0)
            if stage_percent is not None:
                progress["stage_percent"] = max(0, min(100, int(stage_percent)))
            stage_fraction = float(progress.get("stage_percent") or 0) / 100
            progress["percent"] = 100 if total == 0 and status == "completed" else (
                0 if total == 0 else min(99, int((int(progress["completed"]) + (0 if advance else stage_fraction)) * 100 / total))
            )
            if status:
                progress["status"] = status
            elif progress["completed"] >= total:
                progress["status"] = "completed"
            if stage:
                progress["stage"] = stage
            if current_file is not None:
                progress["current_file"] = current_file
            if current_model is not None:
                progress["current_model"] = current_model
            if extra:
                progress.update(extra)
            if progress.get("status") == "completed":
                progress["percent"] = 100
                progress["stage"] = "completed"
                progress["stage_percent"] = 100
                progress["current_file"] = ""
                progress["current_model"] = ""
            progress["updated_at"] = _now()
            return _progress_public(progress)

    def latest_progress(self) -> Optional[dict[str, Any]]:
        with self._progress_lock:
            if not self._latest_progress_id:
                return None
            progress = self._progress_runs.get(self._latest_progress_id)
            return _progress_public(progress) if progress else None

    def get_progress(self, progress_id: str = "") -> Optional[dict[str, Any]]:
        with self._progress_lock:
            key = progress_id or self._latest_progress_id
            progress = self._progress_runs.get(key)
            return _progress_public(progress) if progress else None

    def _register_page_apis(self) -> None:
        """Register the authenticated APIs used by AstrBot Plugin Pages."""

        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        routes = (
            (f"/{PLUGIN_ID}/files", self._page_files, ["GET"], "List captured files"),
            (f"/{PLUGIN_ID}/progress", self._page_progress, ["GET"], "Latest capture progress"),
            (f"/{PLUGIN_ID}/progress/<progress_id>", self._page_progress_item, ["GET"], "Capture progress"),
            (f"/{PLUGIN_ID}/files/bulk-delete", self._page_delete_files, ["POST"], "Delete captured files"),
            (f"/{PLUGIN_ID}/files/delete", self._page_delete_files, ["POST"], "Delete captured files"),
            (f"/{PLUGIN_ID}/files/clear", self._page_clear_files, ["POST"], "Clear captured files"),
            (f"/{PLUGIN_ID}/files/clear-failed", self._page_clear_failed_files, ["POST"], "Clear failed downloads"),
            (f"/{PLUGIN_ID}/files/upload", self._page_upload_files, ["POST"], "Upload local files"),
            (f"/{PLUGIN_ID}/files/<artifact_id>/preview", self._page_file_preview, ["GET"], "Preview image"),
            (f"/{PLUGIN_ID}/files/<artifact_id>/content", self._page_file_content, ["GET"], "Download captured file"),
            (f"/{PLUGIN_ID}/files/<artifact_id>/blob", self._page_file_blob, ["GET"], "Captured file for the online viewer"),
            (f"/{PLUGIN_ID}/files/<artifact_id>/table", self._page_image_table, ["POST"], "Convert image to table"),
            (f"/{PLUGIN_ID}/import/chunk", self._page_import_chunk, ["POST"], "Upload a recognition bundle chunk"),
            (f"/{PLUGIN_ID}/import/finish", self._page_import_finish, ["POST"], "Import an uploaded recognition bundle"),
            (f"/{PLUGIN_ID}/import/scan", self._page_import_scan, ["POST"], "Import bundles from the inbox folder"),
            (f"/{PLUGIN_ID}/model-review", self._page_model_review, ["POST"], "Queue vision/LLM recognition for stored files"),
            (f"/{PLUGIN_ID}/stop", self._page_stop_all, ["POST"], "Terminate all capture and recognition jobs"),
            (f"/{PLUGIN_ID}/find", self._page_find, ["POST"], "Find which files mention a person (wildcards allowed)"),
            (f"/{PLUGIN_ID}/portal", self._page_portal_status, ["GET"], "Web query portal status, users and audit log"),
            (f"/{PLUGIN_ID}/portal/revoke", self._page_portal_revoke, ["POST"], "Revoke a user's web query sessions"),
            (f"/{PLUGIN_ID}/portal/unban", self._page_portal_unban, ["POST"], "Lift a web query IP ban"),
            (f"/{PLUGIN_ID}/portal/audit/delete", self._page_portal_audit_delete, ["POST"], "Delete web query audit records"),
            (f"/{PLUGIN_ID}/config", self._page_config, ["GET"], "Read plugin config"),
            (f"/{PLUGIN_ID}/config", self._page_save_config, ["POST"], "Save plugin config"),
        )
        for route, handler, methods, description in routes:
            try:
                register_api(route, handler, methods, description)
            except Exception as exc:
                _log("warning", "Unable to register Plugin Page API %s: %s", route, exc)

    async def _page_files(self) -> Any:
        self._schedule_pending_uploads()
        records = [_public_record(item) for item in self.store.list()]
        return _page_json_response({"files": records, "count": len(records)})

    async def _page_progress(self) -> Any:
        self._schedule_pending_uploads()
        with self._progress_lock:
            runs = [_progress_public(item) for item in self._progress_runs.values()]
        return _page_json_response({"progress": self.latest_progress(), "runs": runs})

    async def _page_progress_item(self, progress_id: str) -> Any:
        self._schedule_pending_uploads()
        progress = self.get_progress(progress_id)
        if not progress:
            return _page_error("捕获任务不存在", 404)
        return _page_json_response({"progress": progress})

    async def _page_delete_files(self) -> Any:
        if not self.config.get("allow_panel_delete", True):
            return _page_error("面板删除功能已禁用", 403)
        payload = await _page_request_payload()
        ids = payload.get("ids", [])
        ids = [ids] if isinstance(ids, str) else ids
        if not isinstance(ids, list):
            return _page_error("ids 必须是文件编号数组")
        deleted = self.store.delete(ids)
        return _page_json_response({"deleted": deleted, "count": len(deleted)})

    async def _page_clear_files(self) -> Any:
        if not self.config.get("allow_panel_delete", True):
            return _page_error("面板删除功能已禁用", 403)
        deleted = self.store.clear()
        return _page_json_response({"deleted": deleted, "count": deleted})

    async def _page_clear_failed_files(self) -> Any:
        if not self.config.get("allow_panel_delete", True):
            return _page_error("面板删除功能已禁用", 403)
        deleted = self.store.clear_failed()
        return _page_json_response({"deleted": deleted, "count": len(deleted)})

    async def _page_upload_files(self) -> Any:
        payload = await _page_request_payload()
        result = await self.process_panel_upload(payload)
        return _page_json_response(result, 200 if result.get("ok") else 400)

    def _import_staging(self) -> Path:
        path = self.store.root / ".imports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def import_inbox(self) -> Path:
        path = self.store.root / "import_inbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def import_chunk(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        upload_id = str(payload.get("upload_id") or "")
        if not _IMPORT_ID.match(upload_id):
            return {"ok": False, "message": "upload_id 无效"}, 400
        try:
            offset = int(payload.get("offset") or 0)
            data = base64.b64decode(str(payload.get("data_base64") or ""), validate=True)
        except (TypeError, ValueError, binascii.Error):
            return {"ok": False, "message": "分片数据无效"}, 400
        if len(data) > 8 * 1024 * 1024:
            return {"ok": False, "message": "分片过大"}, 400
        limit = int(float(self.config.get("import_max_bundle_mb", 2048) or 2048) * 1024 * 1024)
        part = self._import_staging() / f"{upload_id}.part"
        current = part.stat().st_size if part.exists() else 0
        if offset != current:
            return {"ok": False, "message": "分片顺序不一致", "received": current}, 409
        if current + len(data) > limit:
            part.unlink(missing_ok=True)
            return {"ok": False, "message": "识别包超过大小上限"}, 413
        with part.open("ab") as handle:
            handle.write(data)
        return {"ok": True, "received": current + len(data)}, 200

    def import_staged(self, upload_id: str, label: str = "", sha256: str = "") -> tuple[dict[str, Any], int]:
        if not _IMPORT_ID.match(upload_id):
            return {"ok": False, "message": "upload_id 无效"}, 400
        part = self._import_staging() / f"{upload_id}.part"
        if not part.exists():
            return {"ok": False, "message": "没有找到已上传的识别包"}, 404
        try:
            try:
                digest = _verify_bundle_sha256(part, sha256)
            except ValueError as exc:
                return {"ok": False, "message": str(exc), "sha256_verified": False}, 422
            summary = _import_bundle(self.store, part, {**self.config, "_label": label})
            summary["sha256"] = digest
            summary["sha256_verified"] = True
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}, 400
        finally:
            part.unlink(missing_ok=True)
        self.people.reload()
        return summary, 200

    def import_from_inbox(self) -> dict[str, Any]:
        inbox = self.import_inbox()
        done = inbox / "done"
        results: list[dict[str, Any]] = []
        for bundle in sorted(inbox.glob("*.zip")):
            try:
                sidecar = _sha256_sidecar(bundle)
                if sidecar is None:
                    raise ValueError(f"缺少 SHA256 识别码文件：请把 {bundle.name}.sha256 一起放进 import_inbox")
                digest = _verify_bundle_sha256(bundle, sidecar.read_text(encoding="utf-8", errors="ignore"))
                summary = _import_bundle(self.store, bundle, self.config)
                done.mkdir(exist_ok=True)
                bundle.replace(done / bundle.name)
                sidecar.replace(done / sidecar.name)
                results.append({"bundle": bundle.name, **summary, "sha256": digest, "sha256_verified": True})
            except ValueError as exc:
                results.append({"bundle": bundle.name, "ok": False, "message": str(exc)})
        if results:
            self.people.reload()
        return {"ok": True, "bundles": results, "count": len(results)}

    async def _page_import_chunk(self) -> Any:
        payload = await _page_request_payload()
        result, status = await asyncio.to_thread(self.import_chunk, payload)
        return _page_json_response(result, status)

    async def _page_import_finish(self) -> Any:
        payload = await _page_request_payload()
        result, status = await asyncio.to_thread(
            self.import_staged,
            str(payload.get("upload_id") or ""),
            str(payload.get("name") or ""),
            str(payload.get("sha256") or ""),
        )
        if status == 200 and _as_bool(self.config.get("import_model_review", True)):
            result["model_review"] = self.queue_model_review(result.get("record_ids") or [])
        return _page_json_response(result, status)

    async def _page_import_scan(self) -> Any:
        result = await asyncio.to_thread(self.import_from_inbox)
        if _as_bool(self.config.get("import_model_review", True)):
            ids = [rid for item in result.get("bundles", []) for rid in (item.get("record_ids") or [])]
            result["model_review"] = self.queue_model_review(ids)
        return _page_json_response(result)

    async def _page_portal_status(self) -> Any:
        if _as_bool(self.config.get("portal_enabled", False)) and not self._portal_server.running:
            await self.sync_portal()
        return _page_json_response(await asyncio.to_thread(self.portal_status))

    async def _page_portal_revoke(self) -> Any:
        payload = await _page_request_payload()
        return _page_json_response({"revoked": self.portal.revoke_user(str(payload.get("user_id") or ""))})

    async def _page_portal_unban(self) -> Any:
        payload = await _page_request_payload()
        return _page_json_response({"unbanned": self.portal_unban(str(payload.get("ip") or ""))})

    async def _page_portal_audit_delete(self) -> Any:
        payload = await _page_request_payload()
        return _page_json_response({"deleted": self.portal_delete_audit(payload.get("ids") or payload.get("id"), _as_bool(payload.get("all", False)))})

    async def _page_find(self) -> Any:
        payload = await _page_request_payload()
        result = await asyncio.to_thread(self.find_objects, str(payload.get("query") or payload.get("q") or ""))
        return _page_json_response(result)

    async def _page_stop_all(self) -> Any:
        return _page_json_response(await self.stop_all())

    async def _page_model_review(self) -> Any:
        payload = await _page_request_payload()
        ids = payload.get("ids")
        ids = [ids] if isinstance(ids, str) else ids
        if ids is not None and not isinstance(ids, list):
            return _page_error("ids 必须是文件编号数组")
        force = _as_bool(payload.get("force", False))
        return _page_json_response(self.queue_model_review(ids, force=force))

    # ---------- 视觉模型 / LLM 补跑队列 ----------
    def _model_review_needed(self, record: Mapping[str, Any], force: bool = False) -> bool:
        if record.get("download_status") != "ok" or record.get("kind") == "archive":
            return False
        if not _model_scope_allows(record, self.config):
            return False
        path = self.store.path_for(record)
        if not path or not path.exists():
            return False
        vision = _as_bool(self.config.get("vision_enabled", False))
        review = _as_bool(self.config.get("llm_review_enabled", False))
        if force:
            return vision or review
        return (
            (vision and record.get("vision_status") not in {"extracted", "no_review_pages", "skipped"})
            or (review and record.get("llm_review_status") not in {"completed", "skipped"})
        )

    def queue_model_review(self, ids: Optional[Iterable[Any]] = None, *, force: bool = False) -> dict[str, Any]:
        """Queue vision-model recognition and LLM review for stored records.

        ``ids`` None means every eligible record (images/PDFs by default).
        Runs one file at a time in the background; returns how many queued.
        """
        vision = _as_bool(self.config.get("vision_enabled", False))
        review = _as_bool(self.config.get("llm_review_enabled", False))
        if not vision and not review:
            return {"ok": False, "queued": 0, "message": "视觉模型识别和 LLM 审核都没有开启"}
        wanted = None if ids is None else {str(item) for item in ids}
        if wanted is not None and not wanted:
            return {"ok": True, "queued": 0, "pending": len(self._model_review_queue)}
        queued = 0
        for record in reversed(self.store.list()):
            record_id = str(record.get("id") or "")
            if wanted is not None and record_id not in wanted:
                continue
            if record_id in self._model_review_queue or not self._model_review_needed(record, force):
                continue
            updates: dict[str, Any] = {"model_review_status": "queued", "model_review_error": ""}
            if force:
                updates.update({"vision_status": "", "llm_review_status": ""})
            self.store.update(record_id, **updates)
            self._model_review_queue.append(record_id)
            queued += 1
        if self._model_review_queue:
            self._start_model_review_worker()
        return {"ok": True, "queued": queued, "pending": len(self._model_review_queue)}

    def _start_model_review_worker(self) -> None:
        if self._model_review_task is not None and not self._model_review_task.done():
            return
        self._model_review_task = self._track_background_task(self._run_model_review_queue())

    def _resume_model_reviews(self) -> None:
        if self._model_review_resumed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._model_review_resumed = True
        pending = [
            str(record.get("id") or "") for record in reversed(self.store.list())
            if record.get("model_review_status") in {"queued", "running"}
        ]
        if pending:
            self.queue_model_review(pending)

    async def _run_model_review_queue(self) -> None:
        event = PanelUploadEvent()
        event.sender_name = "视觉模型补跑"
        progress_id = ""
        try:
            epoch = self._stop_epoch
            while self._model_review_queue and not self._terminating and self._stop_epoch == epoch:
                if not progress_id:
                    progress_id = self._start_progress(event, [], status="running")
                    self._update_progress(progress_id, advance=False, stage="vision_review", kind="model_review")
                record_id = self._model_review_queue[0]
                await self._wait_if_paused(progress_id, str((self.store.get(record_id) or {}).get("name") or record_id))
                if self._terminating:
                    break
                record = self.store.get(record_id)
                with self._progress_lock:
                    progress = self._progress_runs.get(progress_id)
                    if progress is not None:
                        progress["total"] = int(progress.get("completed") or 0) + len(self._model_review_queue)
                if record is None:
                    self._model_review_queue.pop(0)
                    continue
                name = str(record.get("name") or record_id)
                self.store.update(record_id, model_review_status="running")
                self._update_progress(progress_id, advance=False, status="running", stage="vision_review", stage_percent=5, current_file=name, current_model="")
                path = self.store.path_for(record)
                mime = str(record.get("mime") or (path and _mime_for(path)) or "")
                candidate = AttachmentCandidate(
                    name=name, kind=str(record.get("kind") or "file"), mime=mime,
                    value=str(path or ""), source=dict(record.get("source") or {}), type_hint="model-review",
                )
                try:
                    record = await self._apply_vision_model(event, candidate, record, progress_id=progress_id)
                    self._update_progress(progress_id, advance=False, stage="llm_review", stage_percent=90, current_file=name, current_model="")
                    record = await self._apply_llm_review(event, record)
                    self.store.update(record_id, model_review_status="completed", model_review_at=_now())
                except Exception as exc:  # 一个文件失败不影响其余
                    _log("warning", "NameSearcher model review failed for %s: %s", name, exc)
                    self.store.update(record_id, model_review_status="failed", model_review_error=str(exc)[:500])
                self._model_review_queue.pop(0)
                self._update_progress(progress_id, record=self.store.get(record_id) or record, advance=True)
            if progress_id:
                self._update_progress(progress_id, advance=False, status="completed", stage="completed", current_file="", current_model="")
        finally:
            self._model_review_task = None

    async def _page_file_preview(self, artifact_id: str) -> Any:
        payload, status = await asyncio.to_thread(_preview_payload, self.store, artifact_id)
        return _page_json_response(payload, status)

    async def _page_file_blob(self, artifact_id: str) -> Any:
        payload, status = await asyncio.to_thread(_blob_payload, self.store, artifact_id)
        return _page_json_response(payload, status)

    async def _page_file_content(self, artifact_id: str) -> Any:
        record = self.store.get(artifact_id)
        if not record:
            return _page_error("文件不存在", 404)
        path = self.store.path_for(record)
        if not path or not path.exists():
            return _page_error("文件内容不可用", 404)
        if not callable(_astr_file_response):
            return _page_error("当前 AstrBot 版本不支持 Page 文件下载", 501)
        return _astr_file_response(
            str(path),
            filename=str(record.get("name") or path.name),
            content_type=str(record.get("mime") or "application/octet-stream"),
        )

    async def _page_image_table(self, artifact_id: str) -> Any:
        return _page_json_response(await self.image_to_table(artifact_id))

    async def _page_config(self) -> Any:
        return _page_json_response(_panel_config_view(self))

    async def _page_save_config(self) -> Any:
        payload = await _page_request_payload()
        ignored = _apply_panel_config(self, payload)
        await self.sync_portal()
        view = _panel_config_view(self)
        view["ignored_keys"] = ignored
        return _page_json_response(view)

    def _try_register_webui(self) -> None:
        """Register with whichever web hook the installed AstrBot exposes."""

        if self.web_app is None:
            return
        mount_path = "/" + str(self.config.get("panel_mount_path") or "/api/name-searcher").strip("/")
        for owner in (self.context, self):
            for method_name in (
                "register_plugin_page",
                "register_webui",
                "register_plugin_webui",
                "register_web_app",
                "register_web_route",
                "register_route",
                "mount_web_app",
            ):
                method = getattr(owner, method_name, None)
                if not callable(method):
                    continue
                if method_name == "register_plugin_page":
                    argument_sets = (
                        (PLUGIN_ID, str(PLUGIN_PAGE_ROOT)),
                        (PLUGIN_ID, PLUGIN_PAGE_ROOT),
                        (str(PLUGIN_PAGE_ROOT),),
                        (PLUGIN_PAGE_ROOT,),
                    )
                elif method_name in {"register_web_route", "register_route", "mount_web_app"}:
                    argument_sets = (
                        (mount_path, self.web_app),
                        (PLUGIN_ID, mount_path, self.web_app),
                        (self.web_app,),
                    )
                else:
                    argument_sets = (
                        (PLUGIN_ID, self.web_app),
                        (mount_path, self.web_app),
                        (self.web_app,),
                    )
                for args in argument_sets:
                    try:
                        result = method(*args)
                        if inspect.isawaitable(result):
                            try:
                                asyncio.get_running_loop().create_task(result)
                            except RuntimeError:
                                pass
                        _log("info", "NameSearcher web UI registered through %s", method_name)
                        return
                    except TypeError:
                        continue
                    except Exception as exc:
                        _log("debug", "Web UI registration failed: %s", exc)
                        break

        containers = [self.context]
        containers.extend(
            getattr(self.context, name, None)
            for name in ("dashboard", "webui", "server", "web_server")
        )
        for container in containers:
            if container is None:
                continue
            for attribute in ("app", "web_app", "dashboard_app"):
                host_app = getattr(container, attribute, None)
                mount = getattr(host_app, "mount", None)
                if not callable(mount):
                    continue
                try:
                    mount(mount_path, self.web_app, name=PLUGIN_ID)
                    _log("info", "NameSearcher web UI mounted at %s", mount_path)
                    return
                except Exception as exc:
                    _log("debug", "Unable to mount NameSearcher UI: %s", exc)

    def _capture_one(self, candidate: AttachmentCandidate) -> dict[str, Any]:
        max_bytes = int(float(self.config.get("max_file_size_mb", 25)) * 1024 * 1024)
        timeout = int(self.config.get("download_timeout_seconds", 20))
        content, status = _read_candidate(candidate, timeout, max_bytes)
        if content is None or status != "ok":
            return self.store.add_unavailable(candidate.name, kind=candidate.kind, mime=candidate.mime, source=candidate.source, reason=status)
        record = self.store.add_bytes(candidate.name, content, kind=candidate.kind, mime=candidate.mime, source=candidate.source)
        if self._recognition_completed(record):
            return record
        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        if source.get("upload_source") == "plugin-page":
            running = {
                "processing_status": "running",
                "processing_updated_at": _now(),
                "processing_error": "",
            }
            self.store.update(record["id"], **running)
            record.update(running)
        path = self.store.path_for(record)
        if path and path.exists():
            updates: dict[str, Any] = {}
            extraction_details: dict[str, Any] = {}
            try:
                if candidate.kind == "image":
                    result = _recognize_image(path, str(self.config.get("ocr_lang") or "eng"), self.config)
                    text = str(result.get("text") or "")
                    confidence = float(result.get("confidence") or 0)
                    updates.update({
                        "image_clarity": result.get("clarity", 0),
                        "image_width": result.get("width", 0),
                        "image_height": result.get("height", 0),
                        "ocr_engine": result.get("engine", "tesseract"),
                        "ocr_psm": result.get("psm"),
                        "ocr_raw_confidence": result.get("raw_confidence", 0),
                    })
                else:
                    text, confidence = _read_text_from_file(
                        path,
                        candidate.mime,
                        str(self.config.get("ocr_lang") or "eng"),
                        self.config,
                        extraction_details,
                    )
                repaired_text, repaired, repair_route = _repair_mojibake(text)
                if repaired:
                    text = repaired_text
                    extraction_details["encoding_converted"] = True
                    extraction_details["encoding_repair"] = repair_route
                if extraction_details:
                    updates.update(extraction_details)
                    converted = _as_bool(extraction_details.get("encoding_converted", False))
                    updates["encoding_issue"] = converted
                    updates["encoding_status"] = "converted" if converted else "ok"
                if candidate.kind == "spreadsheet":
                    sheet_matches = re.findall(r"^\[工作表\s+\d+/\d+：(.+?)\]$", text, flags=re.MULTILINE)
                    if sheet_matches:
                        updates["sheet_count"] = len(sheet_matches)
                        updates["sheet_names"] = sheet_matches
                updates["extraction_status"] = "extracted" if text.strip() else (
                    "stored_only" if candidate.kind in {"archive", "file"} else "no_text"
                )
            except Exception as exc:
                _log("warning", "Content recognition failed for %s: %s", path, exc)
                text, confidence = "", 0.0
                updates.update({"extraction_status": "failed", "extraction_error": str(exc)[:500]})
            updates.update({"ocr_text": text[:200000], "ocr_confidence": round(confidence, 4)})
            self.store.update(record["id"], **updates)
            record.update(updates)
        return record

    async def _apply_vision_model(
        self,
        event: Any,
        candidate: AttachmentCandidate,
        record: dict[str, Any],
        *,
        progress_id: str = "",
    ) -> dict[str, Any]:
        if not _as_bool(self.config.get("vision_enabled", False)) or record.get("download_status") != "ok":
            return record
        if not _model_scope_allows(record, self.config):
            if record.get("vision_status") != "skipped":
                self.store.update(record["id"], vision_status="skipped")
                record["vision_status"] = "skipped"
            return record
        path = self.store.path_for(record)
        if not path or not path.exists():
            return record
        source = _event_source(event)
        session_id = str(source.get("unified_msg_origin") or source.get("message_id") or "name-searcher")
        existing = str(record.get("ocr_text") or "").strip()
        targets = _parse_vision_targets(self.config)
        max_pages = _vision_page_limit(self.config.get("vision_review_max_pages", 10))
        results: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="name-searcher-vision-") as temporary_directory:
            review_pages = await asyncio.to_thread(
                _prepare_vision_review_images,
                path,
                str(record.get("mime") or candidate.mime or _mime_for(path)),
                existing,
                Path(temporary_directory),
                max_pages,
            )
            if not review_pages:
                updates = {"vision_status": "no_review_pages", "vision_results": []}
                self.store.update(record["id"], **updates)
                record.update(updates)
                return record
            total_calls = max(1, len(review_pages) * len(targets))
            completed_calls = 0
            for provider_name, model_name in targets:
                model_label = "::".join(part for part in (provider_name, model_name) if part) or "当前会话默认视觉模型"
                page_texts: list[str] = []
                engines: list[str] = []
                for page_number, review_page in enumerate(review_pages, 1):
                    if progress_id:
                        self._update_progress(
                            progress_id,
                            advance=False,
                            status="running",
                            stage="vision_review",
                            stage_percent=30 + completed_calls * 55 / total_calls,
                            current_file=str(record.get("name") or candidate.name),
                            current_model=f"{model_label} · 第 {page_number}/{len(review_pages)} 页",
                        )
                    prompt = (
                        str(self.config.get("vision_prompt") or DEFAULT_CONFIG["vision_prompt"])
                        + f"\n\n文件名：{record.get('name') or candidate.name}\n审阅页：{page_number}/{len(review_pages)}"
                    )
                    text, engine = await _call_vision_model(
                        self.context,
                        review_page,
                        _mime_for(review_page),
                        prompt,
                        provider_name=provider_name,
                        model_name=model_name,
                        timeout=int(self.config.get("vision_timeout_seconds", 60) or 60),
                        session_id=session_id,
                    )
                    completed_calls += 1
                    if text:
                        page_texts.append(text)
                    if engine:
                        engines.append(engine)
                combined = "\n".join(page_texts).strip()
                results.append({
                    "provider": provider_name,
                    "model": model_name,
                    "label": model_label,
                    "status": "completed" if combined else "unavailable",
                    "pages_reviewed": len(review_pages),
                    "text": combined[:200000],
                    "engine": ", ".join(dict.fromkeys(engines)),
                })

        successful = [item for item in results if item.get("text")]
        if not successful:
            updates = {
                "vision_status": "unavailable",
                "vision_results": results,
                "vision_models_completed": 0,
            }
            self.store.update(record["id"], **updates)
            record.update(updates)
            return record

        final_text = str(successful[0]["text"])
        consensus_engine = ""
        consensus_status = "single_result"
        if len(successful) > 1:
            final_text = max((str(item["text"]) for item in successful), key=len)
            if _as_bool(self.config.get("vision_consensus_enabled", True)):
                if progress_id:
                    self._update_progress(
                        progress_id,
                        advance=False,
                        status="running",
                        stage="consensus",
                        stage_percent=90,
                        current_file=str(record.get("name") or candidate.name),
                        current_model="多模型共识审核",
                    )
                evidence = [f"[本地解析]\n{existing}" if existing else ""]
                evidence.extend(f"[{item['label']}]\n{item['text']}" for item in successful)
                consensus_prompt = (
                    str(self.config.get("vision_consensus_prompt") or DEFAULT_CONFIG["vision_consensus_prompt"])
                    + f"\n\n文件名：{record.get('name') or candidate.name}\n\n"
                    + "\n\n".join(item for item in evidence if item)[:600000]
                )
                first_target = successful[0]
                consensus, consensus_engine = await _call_text_model(
                    self.context,
                    consensus_prompt,
                    provider_name=str(first_target.get("provider") or ""),
                    model_name=str(first_target.get("model") or ""),
                    timeout=int(self.config.get("vision_timeout_seconds", 60) or 60),
                    session_id=f"{session_id}-consensus",
                )
                if consensus:
                    final_text = consensus
                    consensus_status = "completed"
                else:
                    consensus_status = "unavailable"

        pages_reviewed = max((int(item.get("pages_reviewed") or 0) for item in successful), default=0)
        pages_total = _pdf_page_count(path) if path.suffix.lower() == ".pdf" else 0
        if pages_total > pages_reviewed > 0:
            # 超出送审页数的部分不经视觉模型复核，但要保留本地识别结果，保证全文完整可查。
            rest = _pdf_page_texts(path, pages_reviewed)
            rest_text = "\n".join(
                f"[第 {pages_reviewed + offset + 1} 页]\n{text}" for offset, text in enumerate(rest) if text
            ).strip()
            note = f"[第 {pages_reviewed + 1}–{pages_total} 页未经视觉模型复核，以下为本地识别结果]"
            if rest_text:
                final_text = f"{final_text}\n\n{note}\n{rest_text}"
            elif existing:
                final_text = f"{final_text}\n\n{note.replace('以下为本地识别结果', '以下为本地识别全文')}\n{existing}"
        updates = {
            "ocr_text_local": existing[:200000],
            "ocr_text": final_text[:200000],
            "vision_pages_reviewed": pages_reviewed,
            "vision_pages_total": pages_total or pages_reviewed,
            "ocr_confidence": max(float(record.get("ocr_confidence") or 0), 0.85),
            "ocr_engine": "vision-consensus" if consensus_status == "completed" else "vision-model",
            "vision_engine": consensus_engine or str(successful[0].get("engine") or ""),
            "vision_status": "extracted",
            "vision_results": results,
            "vision_models_completed": len(successful),
            "vision_consensus_status": consensus_status,
            "vision_consensus_text": final_text[:200000] if consensus_status == "completed" else "",
            "extraction_status": "extracted" if final_text.strip() else record.get("extraction_status"),
        }
        self.store.update(record["id"], **updates)
        record.update(updates)
        return record

    async def _apply_llm_review(self, event: Any, record: dict[str, Any]) -> dict[str, Any]:
        """Audit extracted text with one or more AstrBot text models.

        Every selected model reviews the text; with several successful
        reviews a consensus call (first model) merges them.  The original
        local/vision output is retained for diagnostics and the reviewed text
        becomes the searchable value.
        """
        if not _as_bool(self.config.get("llm_review_enabled", False)):
            return record
        if not _model_scope_allows(record, self.config):
            if record.get("llm_review_status") != "skipped":
                self.store.update(record["id"], llm_review_status="skipped")
                record["llm_review_status"] = "skipped"
            return record
        original = str(record.get("ocr_text") or "").strip()
        if not original and record.get("download_status") == "ok":
            return record
        name = str(record.get("name") or "未命名文件")
        template = str(self.config.get("llm_review_prompt") or DEFAULT_CONFIG["llm_review_prompt"])
        try:
            prompt = template.format(name=name, text=original[:200000])
        except Exception:
            prompt = f"请审核文件 {name} 的识别结果并输出可检索文本：\n{original[:200000]}"
        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        session_id = str(source.get("unified_msg_origin") or source.get("message_id") or "name-searcher-review")
        timeout = int(self.config.get("llm_review_timeout_seconds", 60) or 60)
        results: list[dict[str, Any]] = []
        for provider_name, model_name in _parse_llm_review_targets(self.config):
            label = "::".join(part for part in (provider_name, model_name) if part) or "当前会话默认模型"
            try:
                reviewed, engine = await _call_text_model(
                    self.context,
                    prompt,
                    provider_name=provider_name,
                    model_name=model_name,
                    timeout=timeout,
                    session_id=session_id,
                )
            except Exception as exc:
                _log("debug", "LLM review failed (%s): %s", label, exc)
                reviewed, engine = "", ""
            results.append({
                "provider": provider_name,
                "model": model_name,
                "label": label,
                "status": "completed" if reviewed else "unavailable",
                "text": str(reviewed or "")[:200000],
                "engine": engine,
            })
        successful = [item for item in results if item.get("text")]
        if not successful:
            updates = {
                "llm_review_status": "unavailable",
                "llm_review_engine": ", ".join(str(item.get("engine") or "") for item in results if item.get("engine")),
                "llm_review_results": results,
            }
            self.store.update(record["id"], **updates)
            record.update(updates)
            return record
        reviewed = str(successful[0]["text"])
        engine = str(successful[0].get("engine") or "")
        consensus_status = "single_result"
        if len(successful) > 1:
            reviewed = max((str(item["text"]) for item in successful), key=len)
            evidence = "\n\n".join(f"[{item['label']}]\n{item['text']}" for item in successful)
            consensus_prompt = (
                str(self.config.get("llm_review_consensus_prompt") or DEFAULT_CONFIG["llm_review_consensus_prompt"])
                + f"\n\n文件名：{name}\n\n[原识别结果]\n{original[:200000]}\n\n{evidence}"
            )[:600000]
            first = successful[0]
            try:
                merged, merged_engine = await _call_text_model(
                    self.context,
                    consensus_prompt,
                    provider_name=str(first.get("provider") or ""),
                    model_name=str(first.get("model") or ""),
                    timeout=timeout,
                    session_id=f"{session_id}-review-consensus",
                )
            except Exception as exc:
                _log("debug", "LLM review consensus failed: %s", exc)
                merged, merged_engine = "", ""
            if merged:
                reviewed, engine, consensus_status = merged, merged_engine, "completed"
            else:
                consensus_status = "unavailable"
        updates = {
            "ocr_text_before_llm_review": original[:200000],
            "ocr_text": reviewed[:200000],
            "llm_review_text": reviewed[:200000],
            "llm_review_status": "completed",
            "llm_review_engine": engine,
            "llm_review_results": results,
            "llm_review_models_completed": len(successful),
            "llm_review_consensus_status": consensus_status,
            "ocr_engine": "llm-review",
            "ocr_confidence": max(float(record.get("ocr_confidence") or 0), 0.9),
            "extraction_status": "extracted" if reviewed.strip() else record.get("extraction_status"),
        }
        self.store.update(record["id"], **updates)
        record.update(updates)
        return record

    async def _extract_event_candidates(self, event: AstrMessageEvent) -> list[AttachmentCandidate]:
        if self._paused():
            return []  # 面板已暂停：新消息里的附件不捕获
        if not _event_can_capture(event, self.config):
            return []
        forward_payloads = await _fetch_forward_payloads(event)
        timeout = max(0.0, float(self.config.get("forward_file_timeout_seconds", 15) or 0))

        async def receipt(count: int) -> None:
            if _as_bool(self.config.get("capture_progress_notify", True)):
                wait = f"，最多等待约 {int(timeout) + FORWARD_URL_ACTION_TIMEOUT} 秒" if timeout else ""
                await _send(event, f"已收到聊天记录，正在获取其中的 {count} 个文件{wait}…")

        unavailable: list[str] = []
        images_only = _as_bool(self.config.get("forward_images_only", True))
        await _resolve_qq_file_urls(
            event, forward_payloads, get_file_timeout=timeout, on_start=receipt,
            images_only=images_only, unavailable=unavailable,
        )
        notice = _forward_unavailable_notice(unavailable)
        if notice:
            await _send_quoted(event, notice)
        return _extract_candidates(event, forward_payloads)

    async def process_panel_upload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._schedule_pending_uploads()
        candidates, errors = _panel_upload_candidates(payload, self.config)
        if not candidates:
            return {"ok": False, "message": "没有可处理的上传文件", "errors": errors}
        event = PanelUploadEvent()
        progress_id = self._start_progress(event, candidates, status="queued")
        records: list[dict[str, Any]] = []
        queued_records: list[dict[str, Any]] = []
        max_bytes = int(float(self.config.get("max_file_size_mb", 25) or 25) * 1024 * 1024)
        for candidate in candidates:
            content, status = _read_candidate(candidate, 1, max_bytes)
            if content is None or status != "ok":
                errors.append(f"{candidate.name}：{status}")
                continue
            record = self.store.add_bytes(
                candidate.name,
                content,
                kind=candidate.kind,
                mime=candidate.mime,
                source=candidate.source,
            )
            if self._recognition_completed(record):
                records.append(record)
                self._update_progress(progress_id, record=record, advance=True)
                continue
            updates = {
                "processing_status": "queued",
                "processing_updated_at": _now(),
                "processing_error": "",
                "upload_progress_id": progress_id,
            }
            self.store.update(record["id"], **updates)
            record.update(updates)
            records.append(record)
            queued_records.append(record)
            self._update_progress(progress_id, record=record, advance=False, status="queued")
        if not records:
            self._update_progress(progress_id, advance=False, status="failed", stage="failed")
            return {"ok": False, "message": "上传文件未能保存", "errors": errors, "progress_id": progress_id}
        artifact_ids = [str(record["id"]) for record in queued_records]
        if artifact_ids:
            self._scheduled_upload_ids.update(artifact_ids)
            task = self._track_background_task(self._run_queued_uploads(artifact_ids, progress_id))
            if task is None:
                self._scheduled_upload_ids.difference_update(artifact_ids)
        else:
            self._update_progress(progress_id, advance=False, status="completed")
        return {
            "ok": True,
            "status": "queued" if artifact_ids else "completed",
            "count": len(records),
            "records": [_public_record(record) for record in records],
            "errors": errors,
            "progress_id": progress_id,
        }

    def _stored_candidate(self, record: Mapping[str, Any]) -> Optional[AttachmentCandidate]:
        path = self.store.path_for(record)
        if not path or not path.exists():
            return None
        return AttachmentCandidate(
            name=str(record.get("name") or path.name),
            kind=str(record.get("kind") or _kind_for(path.name, str(record.get("mime") or ""))),
            mime=str(record.get("mime") or _mime_for(path)),
            value=str(path),
            source=dict(record.get("source") or {}),
            type_hint="panel-upload-resume",
        )

    def _checkpoint_archive_candidates(
        self,
        candidates: list[AttachmentCandidate],
        progress_id: str,
    ) -> list[AttachmentCandidate]:
        pending: list[AttachmentCandidate] = []
        max_bytes = int(float(self.config.get("max_file_size_mb", 25) or 25) * 1024 * 1024)
        for candidate in candidates:
            content, status = _read_candidate(candidate, 1, max_bytes)
            if content is None or status != "ok":
                continue
            record = self.store.add_bytes(
                candidate.name,
                content,
                kind=candidate.kind,
                mime=candidate.mime,
                source=candidate.source,
            )
            if self._recognition_completed(record):
                continue
            checkpoint = {
                "processing_status": "queued",
                "processing_updated_at": _now(),
                "processing_error": "",
                "upload_progress_id": progress_id,
            }
            updated = self.store.update(record["id"], **checkpoint) or record
            stored = self._stored_candidate(updated)
            if stored is not None:
                pending.append(stored)
        return pending

    async def _process_stored_upload(self, artifact_id: str, progress_id: str = "") -> list[dict[str, Any]]:
        if not artifact_id or artifact_id in self._processing_upload_ids:
            return []
        self._processing_upload_ids.add(artifact_id)
        record = self.store.get(artifact_id)
        if not record:
            self._processing_upload_ids.discard(artifact_id)
            return []
        if self._recognition_completed(record):
            self._processing_upload_ids.discard(artifact_id)
            return []
        candidate = self._stored_candidate(record)
        if candidate is None:
            self.store.update(
                artifact_id,
                processing_status="failed",
                processing_updated_at=_now(),
                processing_error="已上传文件不存在",
            )
            self._processing_upload_ids.discard(artifact_id)
            return []
        attempts = int(record.get("processing_attempts") or 0)
        if record.get("processing_status") == "running" and attempts >= 2:
            # It was already interrupted twice mid-recognition: most likely this
            # file is what brings the host down, so stop retrying it on boot.
            self.store.update(
                artifact_id,
                processing_status="failed",
                processing_updated_at=_now(),
                processing_error="识别多次被中断，已跳过；建议用本地识别工具处理后导入",
            )
            self._processing_upload_ids.discard(artifact_id)
            return []
        event = PanelUploadEvent()
        progress_id = progress_id or self._start_progress(event, [candidate], status="queued")
        self.store.update(
            artifact_id,
            processing_status="running",
            processing_attempts=attempts + 1,
            processing_updated_at=_now(),
            processing_error="",
            upload_progress_id=progress_id,
        )
        try:
            records = await self._process_candidates(event, [candidate], progress_id=progress_id, finalize=False)
            self.store.update(
                artifact_id,
                processing_status="completed",
                processing_updated_at=_now(),
                processing_error="",
            )
            return records
        except asyncio.CancelledError:
            self.store.update(
                artifact_id,
                processing_status="queued",
                processing_updated_at=_now(),
                processing_error="插件重载，等待恢复",
            )
            raise
        except Exception as exc:
            self.store.update(
                artifact_id,
                processing_status="failed",
                processing_updated_at=_now(),
                processing_error=str(exc)[:500],
            )
            _log("warning", "Panel upload processing failed for %s: %s", artifact_id, exc)
            return []
        finally:
            self._processing_upload_ids.discard(artifact_id)

    async def _run_queued_uploads(self, artifact_ids: list[str], progress_id: str) -> None:
        try:
            for artifact_id in artifact_ids:
                if self._terminating:
                    return
                await self._process_stored_upload(artifact_id, progress_id)
            self._update_progress(progress_id, advance=False, status="completed")
        except asyncio.CancelledError:
            self._update_progress(progress_id, advance=False, status="queued", stage="queued")
            raise
        finally:
            self._scheduled_upload_ids.difference_update(artifact_ids)

    async def _process_candidates(
        self,
        event: AstrMessageEvent,
        candidates: list[AttachmentCandidate],
        *,
        progress_id: str = "",
        finalize: bool = True,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        if not progress_id:
            progress_id = self._start_progress(event, candidates)
        records: list[dict[str, Any]] = []
        seen_record_ids: set[str] = set()
        queue = list(candidates)
        epoch = self._stop_epoch
        while queue:
            candidate = queue.pop(0)
            await self._wait_if_paused(progress_id, candidate.name)
            if self._stop_epoch != epoch:
                self._update_progress(progress_id, advance=False, status="cancelled", stage="cancelled", current_file="", current_model="")
                return records
            await self._wait_for_memory(progress_id, candidate.name)
            self._update_progress(
                progress_id,
                advance=False,
                status="running",
                stage="downloading",
                stage_percent=0,
                current_file=candidate.name,
                current_model="",
            )
            try:
                record = await asyncio.to_thread(self._capture_one, candidate)
                if self._recognition_completed(record):
                    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
                    if record.get("processing_status") != "completed" and (
                        source.get("upload_source") == "plugin-page"
                        or candidate.source.get("upload_source") == "plugin-page"
                    ):
                        checkpoint = {
                            "processing_status": "completed",
                            "processing_updated_at": _now(),
                            "processing_error": "",
                            "upload_progress_id": progress_id,
                        }
                        self.store.update(record["id"], **checkpoint)
                        record.update(checkpoint)
                    is_new_record = str(record.get("id")) not in seen_record_ids
                    self._update_progress(
                        progress_id,
                        record=record if is_new_record else None,
                        advance=True,
                    )
                    if is_new_record:
                        seen_record_ids.add(str(record.get("id")))
                        records.append(record)
                    continue
                if (
                    candidate.kind == "archive"
                    and not candidate.source.get("archive_id")
                    and _as_bool(self.config.get("archive_extraction_enabled", True))
                ):
                    archive_path = self.store.path_for(record)
                    if archive_path and archive_path.exists():
                        archive_children, archive_errors = await asyncio.to_thread(
                            _archive_candidates,
                            record,
                            archive_path.read_bytes(),
                            self.config,
                        )
                        pending_archive_children = await asyncio.to_thread(
                            self._checkpoint_archive_candidates,
                            archive_children,
                            progress_id,
                        )
                        archive_updates = {
                            "archive_member_count": len(archive_children),
                            "archive_extraction_status": (
                                ("partial" if archive_errors else "extracted")
                                if archive_children
                                else ("failed" if archive_errors else "empty")
                            ),
                            "archive_errors": archive_errors[:50],
                            "archive_limited_count": _archive_limited_count(archive_errors),
                        }
                        self.store.update(record["id"], **archive_updates)
                        record.update(archive_updates)
                        if pending_archive_children:
                            queue[0:0] = pending_archive_children
                            self._update_progress(
                                progress_id,
                                advance=False,
                                total_delta=len(pending_archive_children),
                                stage="extracting",
                                stage_percent=15,
                                current_file=candidate.name,
                            )
                self._update_progress(progress_id, advance=False, stage="extracting", stage_percent=20, current_file=candidate.name)
                record = await self._apply_vision_model(event, candidate, record, progress_id=progress_id)
                self._update_progress(progress_id, advance=False, stage="llm_review", stage_percent=90, current_file=candidate.name, current_model="")
                record = await self._apply_llm_review(event, record)
                record_source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
                is_root_archive = record.get("kind") == "archive" and not record_source.get("archive_id")
                is_archive_member = bool(candidate.source.get("archive_id") or record_source.get("archive_id"))
                if (record_source.get("upload_source") == "plugin-page" or candidate.source.get("upload_source") == "plugin-page") and (is_archive_member or not is_root_archive):
                    checkpoint = {
                        "processing_status": "completed",
                        "processing_updated_at": _now(),
                        "processing_error": "",
                        "upload_progress_id": progress_id,
                    }
                    self.store.update(record["id"], **checkpoint)
                    record.update(checkpoint)
            except Exception as exc:
                _log("warning", "Capture failed for %s: %s", candidate.name, exc)
                record = self.store.add_unavailable(candidate.name, kind=candidate.kind, mime=candidate.mime, source=candidate.source, reason=f"capture_failed:{exc}")
            is_new_record = str(record.get("id")) not in seen_record_ids
            snapshot = self._update_progress(progress_id, record=record if is_new_record else None, advance=True)
            if _as_bool(self.config.get("capture_progress_notify", True)) and snapshot:
                completed = int(snapshot.get("completed") or 0)
                total = int(snapshot.get("total") or 0)
                percent = int(snapshot.get("percent") or 0)
                # Emit quarter-ish milestones and always report completion;
                # the command /捕获进度 can query the exact live snapshot.
                milestone = percent >= 100 or completed == 1 or percent // 25 > int((completed - 1) * 100 / max(1, total)) // 25
                if milestone:
                    await _send(event, _progress_text(snapshot))
            if not is_new_record:
                continue
            seen_record_ids.add(str(record.get("id")))
            records.append(record)
            confidence = float(record.get("ocr_confidence") or 0)
            needs_review = (
                record.get("download_status") != "ok"
                or (record.get("kind") == "image" and confidence < float(self.config.get("review_threshold", 0.55)))
                or (
                    record.get("kind") in {"document", "spreadsheet", "presentation"}
                    and record.get("extraction_status") in {"no_text", "failed"}
                )
            )
            if record.get("reason") == FORWARD_UNRESOLVED_REASON:
                needs_review = False  # already answered with one quoted notice
            if self.config.get("request_review_on_low_confidence", True) and needs_review:
                await _send(event, _review_notice(record))
        if _as_bool(self.config.get("notify_archive_limits", True)):
            notice = _archive_notice(records)
            if notice:
                await _send(event, notice)
        if _as_bool(self.config.get("notify_encoding_issues", True)):
            notice = _encoding_notice(records)
            if notice:
                await _send(event, notice)
        if finalize:
            self._update_progress(progress_id, advance=False, status="completed")
        return records

    async def capture_event(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        """Capture one event immediately; retained for commands and integrations."""
        candidates = await self._extract_event_candidates(event)
        if not candidates:
            return []
        progress_id = self._start_progress(event, candidates)
        return await self._process_candidates(event, candidates, progress_id=progress_id)

    async def _flush_capture_window(self) -> None:
        interval = self._capture_interval()
        try:
            await asyncio.sleep(interval)
            async with self._capture_window_lock:
                entries = self._capture_window_entries
                self._capture_window_entries = []
                progress_id = self._capture_window_progress_id
                self._capture_window_progress_id = ""
            if not entries:
                return
            first_event = entries[0][0]
            candidates = [candidate for _event, event_candidates in entries for candidate in event_candidates]
            progress_id = progress_id or self._start_progress(
                first_event, candidates, status="running", window_seconds=interval
            )
            with self._progress_lock:
                progress = self._progress_runs.get(progress_id)
                if progress:
                    progress["status"] = "running"
                    progress["stage"] = "downloading"
                    progress["total"] = len(candidates)
                    progress["queued_events"] = len(entries)
            for event, event_candidates in entries:
                await self._process_candidates(event, event_candidates, progress_id=progress_id, finalize=False)
            self._update_progress(progress_id, advance=False, status="completed", queued_event_delta=-len(entries))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log("warning", "Capture window failed: %s", exc)
            if 'progress_id' in locals() and progress_id:
                self._update_progress(progress_id, advance=False, status="failed", stage="failed", error=str(exc)[:500])
        finally:
            async with self._capture_window_lock:
                self._capture_window_task = None
                if self._capture_window_entries and not self._terminating:
                    self._capture_window_task = asyncio.create_task(self._flush_capture_window())

    async def _queue_capture_event(self, event: AstrMessageEvent) -> None:
        if self._terminating:
            return
        candidates = await self._extract_event_candidates(event)
        if not candidates:
            return
        async with self._capture_window_lock:
            self._capture_window_entries.append((event, candidates))
            if not self._capture_window_progress_id:
                self._capture_window_progress_id = self._start_progress(
                    event, [], status="collecting", window_seconds=self._capture_interval()
                )
            self._update_progress(
                self._capture_window_progress_id,
                advance=False,
                status="collecting",
                stage="collecting",
                stage_percent=0,
                total_delta=len(candidates),
                queued_event_delta=1,
                current_file=candidates[-1].name,
            )
            if self._capture_window_task is None or self._capture_window_task.done():
                self._capture_window_task = asyncio.create_task(self._flush_capture_window())

    def _paused(self) -> bool:
        return _as_bool(self.config.get("paused", False))

    async def stop_all(self) -> dict[str, Any]:
        """Terminate every capture and recognition job that is waiting or running.

        Files already saved stay in the library; unfinished plugin-page uploads
        and model reviews are marked cancelled so they are not resumed on the
        next start. New messages are still captured unless the plugin is paused.
        """

        self._stop_epoch += 1
        async with self._capture_window_lock:
            dropped = sum(len(items) for _event, items in self._capture_window_entries)
            self._capture_window_entries = []
            window_task = self._capture_window_task
            self._capture_window_task = None
            self._capture_window_progress_id = ""
        current = asyncio.current_task()
        tasks = [task for task in [window_task, *self._background_tasks] if task is not None and not task.done() and task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        review_count = len(self._model_review_queue)
        self._model_review_queue.clear()
        self._model_review_task = None
        self._scheduled_upload_ids.clear()
        self._processing_upload_ids.clear()
        self._resume_scheduled = False
        cancelled_records = 0
        for record in self.store.list():
            updates: dict[str, Any] = {}
            if record.get("processing_status") in {"queued", "running"}:
                updates.update(processing_status="cancelled", processing_error="已在插件页终止", processing_updated_at=_now())
            if record.get("model_review_status") in {"queued", "running"}:
                updates["model_review_status"] = "cancelled"
            if updates:
                self.store.update(str(record.get("id")), **updates)
                cancelled_records += 1
        cancelled_runs = 0
        with self._progress_lock:
            for progress in self._progress_runs.values():
                if progress.get("status") in {"queued", "collecting", "running", "paused"}:
                    progress["status"] = "cancelled"
                    progress["stage"] = "cancelled"
                    progress["current_file"] = ""
                    progress["current_model"] = ""
                    progress["updated_at"] = _now()
                    cancelled_runs += 1
        _log("info", "NameSearcher stopped all jobs: %s tasks, %s runs, %s records, %s window files, %s reviews",
             len(tasks), cancelled_runs, cancelled_records, dropped, review_count)
        return {
            "ok": True,
            "cancelled_tasks": len(tasks),
            "cancelled_runs": cancelled_runs,
            "cancelled_records": cancelled_records,
            "dropped_window_files": dropped,
            "cancelled_model_reviews": review_count,
        }

    async def _wait_if_paused(self, progress_id: str = "", current: str = "") -> None:
        """Hold recognition between files while the panel has paused the plugin."""

        if not self._paused():
            return
        if progress_id:
            self._update_progress(progress_id, advance=False, status="paused", stage="paused", current_file=current)
        while self._paused() and not self._terminating:
            await asyncio.sleep(1)
        if progress_id and not self._terminating:
            self._update_progress(progress_id, advance=False, status="running", stage="downloading", current_file=current)

    async def _wait_for_memory(self, progress_id: str, current: str) -> None:
        """Pause between files while the host is short of memory instead of crashing."""

        try:
            floor = int(float(self.config.get("min_free_memory_mb", 200) or 0))
        except (TypeError, ValueError):
            floor = 200
        if floor <= 0:
            return
        for _attempt in range(120):  # at most ten minutes, then carry on
            available = _available_memory_mb()
            if available is None or available >= floor or self._terminating:
                return
            self._update_progress(progress_id, advance=False, stage="waiting_memory", current_file=current)
            await asyncio.sleep(5)

    def _capture_interval(self) -> int:
        try:
            return max(1, int(float(self.config.get("capture_interval_seconds", 30))))
        except (TypeError, ValueError):
            return 30

    def _capture_window_on(self) -> bool:
        """The window is on unless disabled or its length is set to 0."""

        if not _as_bool(self.config.get("capture_window_enabled", True)):
            return False
        try:
            return float(self.config.get("capture_interval_seconds", 30)) > 0
        except (TypeError, ValueError):
            return True

    def _name_threshold(self) -> float:
        try:
            value = float(self.config.get("name_match_threshold", 0.85))
        except (TypeError, ValueError):
            value = 0.85
        return min(1.0, max(0.6, value))

    def _lookup(self, name: str) -> str:
        matches = self._lookup_matches(name)
        return _format_result(name, matches, self.panel_base(), hidden_partial=self.people.last_hidden_partial)

    def _lookup_matches(self, name: str) -> list[dict[str, Any]]:
        return self.people.search(name, self._name_threshold())

    # ---- public portal -------------------------------------------------
    def _portal_session_minutes(self) -> float:
        try:
            return min(24 * 60.0, max(1.0, float(self.config.get("portal_session_minutes") or 30)))
        except (TypeError, ValueError):
            return 30.0

    def _portal_session_max_finds(self) -> int:
        """Finds allowed per web login; 0 means unlimited."""

        try:
            return min(10000, max(0, int(float(self.config.get("portal_session_max_finds") or 0))))
        except (TypeError, ValueError):
            return 0

    def _portal_dir(self) -> Path:
        path = self.store.root.parent / "portal"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _portal_audit(self, action: str, user: Mapping[str, Any], **details: Any) -> None:
        entry = {"time": _now(), "action": action, **_portal_public_user(user), **{key: value for key, value in details.items() if value not in (None, "")}}
        try:
            with self._portal_lock, (self._portal_dir() / "audit.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if action in {"find", "download", "login"}:
                self._portal_touch_profile(user, action)
        except OSError as exc:
            _log("warning", "NameSearcher portal audit write failed: %s", exc)

    def _portal_profiles(self) -> dict[str, Any]:
        try:
            data = json.loads((self._portal_dir() / "users.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _portal_touch_profile(self, user: Mapping[str, Any], action: str) -> None:
        user_id = str(user.get("user_id") or "")
        if not user_id:
            return
        with self._portal_lock:
            profiles = self._portal_profiles()
            profile = profiles.get(user_id) or {"user_id": user_id, "first_seen": _now(), "groups": [], "codes": 0, "logins": 0, "finds": 0, "downloads": 0}
            profile["nickname"] = str(user.get("nickname") or profile.get("nickname") or "")
            group_id = str(user.get("group_id") or "")
            if group_id and group_id not in profile["groups"]:
                profile["groups"].append(group_id)
            counter = {"code": "codes", "login": "logins", "find": "finds", "download": "downloads"}.get(action)
            if counter:
                profile[counter] = int(profile.get(counter) or 0) + 1
            profile["last_seen"] = _now()
            profiles[user_id] = profile
            path = self._portal_dir() / "users.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(path)

    def _portal_bans(self) -> dict[str, Any]:
        if self._portal_ban_cache is None:
            try:
                data = json.loads((self._portal_dir() / "bans.json").read_text(encoding="utf-8"))
                self._portal_ban_cache = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._portal_ban_cache = {}
        return self._portal_ban_cache

    def _portal_save_bans(self) -> None:
        path = self._portal_dir() / "bans.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._portal_bans(), ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path)

    def portal_is_banned(self, ip: str) -> bool:
        with self._portal_lock:
            return str(ip) in self._portal_bans()

    def portal_ban(self, ip: str, reason: str) -> None:
        ip = str(ip or "").strip()
        if not ip:
            return
        with self._portal_lock:
            bans = self._portal_bans()
            if ip in bans:
                return
            bans[ip] = {"ip": ip, "since": _now(), "reason": reason}
            self._portal_save_bans()
        self._portal_audit("ban", {}, client=ip, query=f"{ip}：{reason}")
        _log("warning", "NameSearcher portal banned IP %s: %s", ip, reason)

    def portal_unban(self, ip: str) -> bool:
        with self._portal_lock:
            bans = self._portal_bans()
            if bans.pop(str(ip or "").strip(), None) is None:
                return False
            self._portal_save_bans()
        return True

    def portal_delete_audit(self, ids: Any = None, clear_all: bool = False) -> int:
        """Delete web query audit lines by id (hash of the line) or all of them."""

        wanted = {str(item) for item in (ids if isinstance(ids, (list, tuple, set)) else [ids]) if item}
        if not wanted and not clear_all:
            return 0
        path = self._portal_dir() / "audit.jsonl"
        with self._portal_lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
            kept = [] if clear_all else [line for line in lines if _portal_audit_id(line) not in wanted]
            removed = len(lines) - len(kept)
            if removed:
                temp = path.with_suffix(".tmp")
                temp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
                temp.replace(path)
        return removed

    def portal_status(self, audit_limit: int = 50) -> dict[str, Any]:
        audit: list[dict[str, Any]] = []
        try:
            lines = (self._portal_dir() / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-audit_limit:]
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    entry["id"] = _portal_audit_id(line)
                    audit.append(entry)
        except OSError:
            pass
        profiles = sorted(self._portal_profiles().values(), key=lambda item: str(item.get("last_seen") or ""), reverse=True)
        return {
            "enabled": _as_bool(self.config.get("portal_enabled", False)),
            "running": self._portal_server.running,
            "listen": "%s:%s" % self._portal_server.bound if self._portal_server.running else "",
            "error": self._portal_error,
            "public_url": str(self.config.get("portal_public_url") or ""),
            "session_minutes": self._portal_session_minutes(),
            "session_max_finds": self._portal_session_max_finds(),
            "sessions": self.portal.active_sessions(),
            "users": profiles[:100],
            "audit": audit,
            "bans": sorted(self._portal_bans().values(), key=lambda item: str(item.get("since") or ""), reverse=True),
        }

    def portal_request_allowed(self, chat_type: str, chat_id: str, user_id: str) -> tuple[bool, str]:
        if not _as_bool(self.config.get("portal_enabled", False)):
            return False, "网页查询未开启"
        users = _parse_allowed_ids(self.config.get("portal_allowed_users", ""))
        groups = _parse_allowed_ids(self.config.get("portal_allowed_groups", ""))
        if users and "*" not in users and user_id not in users:
            return False, "你没有网页查询权限，请联系管理员"
        if chat_type == "group":
            if groups and "*" not in groups and chat_id not in groups:
                return False, "本群未开放网页查询"
            return True, ""
        if not _as_bool(self.config.get("portal_allow_private", True)):
            return False, "私聊申请未开放，请在群内发送"
        return True, ""

    def portal_issue_code(self, pair_code: str, chat_type: str, chat_id: str, user_id: str, nickname: str) -> str:
        allowed, reason = self.portal_request_allowed(chat_type, chat_id, user_id)
        if not allowed:
            return reason
        if not user_id:
            return "无法识别你的账号，请稍后再试"
        user = {"user_id": user_id, "nickname": nickname, "chat_type": chat_type, "group_id": chat_id if chat_type == "group" else ""}
        result = self.portal.claim(pair_code, user)
        if not result.get("ok"):
            return str(result.get("error") or "申请失败")
        self._portal_audit("code", user, pair_code=result["pair_code"])
        minutes = max(1, int(result.get("expires_in", 0) // 60))
        return (
            f"验证码：{result['code']}\n"
            f"仅对识别码 {result['pair_code']} 的查询页有效，{minutes} 分钟内输入；"
            f"登录后可查询和下载 {int(self._portal_session_minutes())} 分钟。"
        )

    def _schedule_portal_sync(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.sync_portal())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def sync_portal(self) -> None:
        """Start, stop or rebind the portal server to match the config."""

        want = _as_bool(self.config.get("portal_enabled", False)) and not self._terminating
        target = (str(self.config.get("portal_host") or "127.0.0.1").strip() or "127.0.0.1", int(self.config.get("portal_port") or 6197))
        server = self._portal_server
        if server.running and (not want or server.bound != target):
            await server.stop()
        if want and not server.running:
            try:
                await server.start()
                self._portal_error = ""
            except Exception as exc:
                self._portal_error = f"网页查询服务启动失败：{exc}"
                _log("error", "NameSearcher portal failed to start: %s", exc)
        elif not want:
            self._portal_error = ""

    def find_objects(self, query: str) -> dict[str, Any]:
        """Panel 查找对象: names (wildcards allowed) grouped with the files they appear in."""

        query = str(query or "").strip()[:64]
        if not query:
            return {"ok": False, "query": "", "names": [], "message": "请输入要查找的姓名或昵称"}
        if _has_wildcard(query):
            return self.people.wildcard_search(query)
        matches = self._lookup_matches(query)
        groups: dict[str, dict[str, Any]] = {}
        for item in matches:
            record = item.get("record") if isinstance(item.get("record"), Mapping) else {}
            source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
            strength = str(item.get("strength") or "exact")
            label = query if strength == "exact" else f"{query}（{'近似' if strength == 'fuzzy' else '包含'}匹配）"
            group = groups.setdefault(label, {"name": label, "strength": strength, "files": [], "library": []})
            if source.get("id"):
                group["files"].append({
                    "id": str(source.get("id")),
                    "name": str(source.get("name") or "未命名文件"),
                    "line": str(record.get("识别行") or "")[:300],
                    "kind": source.get("kind"),
                    "reviewed": source.get("llm_review_status") == "completed",
                })
            else:
                group["library"].append(dict(record))
        names = list(groups.values())
        return {
            "ok": True,
            "query": query,
            "wildcard": False,
            "names": names,
            "name_count": len(names),
            "file_count": len({entry["id"] for group in names for entry in group["files"]}),
            "truncated": 0,
        }

    def _match_records(self, matches: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Path]]:
        """Unique (record, path) pairs of the matched source files that still exist."""

        found: list[tuple[dict[str, Any], Path]] = []
        seen: set[str] = set()
        for item in matches:
            source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
            artifact_id = str(source.get("id") or "")
            if not artifact_id or artifact_id in seen:
                continue
            record = self.store.get(artifact_id)
            path = self.store.path_for(record) if record else None
            if not record or not path or not path.exists():
                continue
            seen.add(artifact_id)
            found.append((record, path))
        return found

    def _resource_chains(self, event: Any, matches: list[dict[str, Any]], include_files: bool = True) -> list[Any]:
        """Native message chains for matched resources.

        Text and images share one message; every non-image file is its own
        message, because QQ cannot mix a file with text in one message.
        """

        chain_result = getattr(event, "chain_result", None)
        if not callable(chain_result):
            return []
        try:
            import astrbot.api.message_components as Comp  # type: ignore
        except Exception:
            return []
        text_part: list[Any] = []
        file_parts: list[Any] = []
        for record, path in self._match_records(matches):
            name = str(record.get("name") or path.name)
            try:
                if record.get("kind") == "image":
                    text_part.append(Comp.Plain(f"来源文件资源：{name}\n"))
                    text_part.append(Comp.Image.fromFileSystem(str(path)))
                elif include_files:
                    file_parts.append([Comp.File(file=str(path), name=name)])
            except Exception as exc:
                _log("debug", "Unable to build resource component for %s: %s", name, exc)
        chains: list[Any] = []
        for components in ([text_part] if text_part else []) + file_parts:
            try:
                chains.append(chain_result(components))
            except Exception as exc:
                _log("debug", "Unable to build lookup resource chain: %s", exc)
        return chains

    def _private_upload_ready(self, event: Any) -> bool:
        bot = getattr(event, "bot", None)
        return _event_chat_identity(event)[0] == "private" and callable(getattr(bot, "call_action", None))

    async def _upload_private_files(self, event: Any, matches: list[dict[str, Any]]) -> list[str]:
        """Send matched non-image files to a private chat via NapCat upload_private_file.

        Files embedded in a private merged forward (or a plain private file
        segment) arrive as 0 B on QQ, so private chats upload them directly.
        Returns names that could not be uploaded.
        """

        files = [(record, path) for record, path in self._match_records(matches) if record.get("kind") != "image"]
        if not files:
            return []
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        user_id = str(_event_sender_id(event) or "")
        failed: list[str] = []
        for record, path in files:
            name = str(record.get("name") or path.name)
            if not callable(call_action) or not user_id:
                failed.append(name)
                continue
            try:
                result = call_action(action="upload_private_file", user_id=int(user_id) if user_id.isdigit() else user_id, file=str(path), name=name)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=120)
            except Exception as exc:
                _log("warning", "NameSearcher upload_private_file failed for %s: %s", name, exc)
                failed.append(name)
        return failed

    def _merged_forward_result(self, event: Any, query: str, matches: list[dict[str, Any]]) -> Any:
        """Build lookup results as one merged-forward message when supported."""

        chain_result = getattr(event, "chain_result", None)
        if not callable(chain_result):
            return None
        platform_name = _event_value(event, "get_platform_name")
        if platform_name and str(platform_name).casefold() not in {"aiocqhttp", "satori"}:
            return None
        try:
            import astrbot.api.message_components as Comp  # type: ignore
        except Exception:
            return None
        if not all(callable(getattr(Comp, name, None)) for name in ("Node", "Nodes", "Plain")):
            return None

        private = self._private_upload_ready(event)
        node_name = "姓名查找器"
        nodes = [Comp.Node(content=[Comp.Plain(f"查找对象：{query}\n匹配数量：{len(matches)}")], name=node_name, uin="0")]
        if not matches:
            nodes.append(Comp.Node(content=[Comp.Plain(f"未找到“{query}”。请确认姓名，或先发送包含名单的文档/图片。")], name=node_name, uin="0"))
        for index, item in enumerate(matches, 1):
            formatted = _format_result(query, [item], self.panel_base()).splitlines()
            content: list[Any] = [Comp.Plain(f"结果 {index}\n" + "\n".join(formatted[2:]))]
            source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
            artifact_id = str(source.get("id") or "")
            record = self.store.get(artifact_id) if artifact_id else None
            path = self.store.path_for(record) if record else None
            file_node: Any = None
            if record and path and path.exists():
                try:
                    if record.get("kind") == "image":
                        content.append(Comp.Image.fromFileSystem(str(path)))
                    elif private:
                        content.append(Comp.Plain(f"\n文件“{record.get('name') or path.name}”将单独发送"))
                    else:
                        file_node = Comp.Node(content=[Comp.File(file=str(path), name=str(record.get("name") or path.name))], name=node_name, uin="0")
                except Exception as exc:
                    _log("debug", "Unable to attach merged-forward resource %s: %s", artifact_id, exc)
            nodes.append(Comp.Node(content=content, name=node_name, uin="0"))
            if file_node is not None:
                nodes.append(file_node)
        try:
            return chain_result([Comp.Nodes(nodes)])
        except Exception as exc:
            _log("debug", "Unable to build merged-forward lookup result: %s", exc)
            return None

    async def image_to_table(self, artifact_id: str = "") -> dict[str, Any]:
        record = self.store.get(artifact_id) if artifact_id else next((item for item in self.store.list() if item.get("kind") == "image"), None)
        if not record:
            return {"ok": False, "message": "没有可转换的图片"}
        path = self.store.path_for(record)
        if not path or not path.exists():
            return {"ok": False, "message": "图片文件不可用"}
        try:
            recognition = await asyncio.to_thread(
                _recognize_image,
                path,
                str(self.config.get("ocr_lang") or "eng"),
                self.config,
            )
            data = recognition.get("data") or {}
            grouped: dict[tuple[Any, ...], list[tuple[int, str]]] = {}
            texts = data.get("text", [])
            for index, raw_text in enumerate(texts):
                text = str(raw_text).strip()
                if not text:
                    continue
                try:
                    confidence = float(data.get("conf", [])[index])
                    if confidence < 0:
                        continue
                except (IndexError, TypeError, ValueError):
                    pass
                row = tuple(
                    (data.get(field, [0] * len(texts))[index] if index < len(data.get(field, [])) else 0)
                    for field in ("page_num", "block_num", "par_num", "line_num")
                )
                left_values = data.get("left", [])
                left = int(left_values[index]) if index < len(left_values) else index
                grouped.setdefault(row, []).append((left, text))
            rows = [[word for _left, word in sorted(words)] for words in grouped.values()]
        except Exception as exc:
            return {"ok": False, "message": f"图片转表格需要 Pillow 与 pytesseract：{exc}"}
        if not rows:
            return {"ok": False, "message": "未识别到表格内容"}
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows(rows)
        result_record = self.store.add_bytes(
            f"{Path(str(record.get('name') or 'image')).stem}_table.csv",
            buffer.getvalue().encode("utf-8-sig"),
            kind="spreadsheet",
            mime="text/csv",
            source={"derived_from": record.get("id"), "name": record.get("name")},
        )
        return {"ok": True, "artifact": result_record, "rows": rows, "source": _record_source(record)}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        self._schedule_pending_uploads()
        if self._capture_window_on():
            await self._queue_capture_event(event)
        else:
            await self.capture_event(event)

    @filter.command("验证码", alias={"查询码"})
    async def portal_code(self, event: AstrMessageEvent, pair_code: str = ""):
        code = _parse_command_text(event, "验证码", pair_code) or _parse_command_text(event, "查询码", pair_code)
        code = re.sub(r"[^A-Za-z0-9]", "", code)
        if not _as_bool(self.config.get("portal_enabled", False)):
            yield _plain(event, "网页查询未开启")
            return
        if not code:
            url = str(self.config.get("portal_public_url") or "").strip()
            hint = f"请先打开查询页 {url} 获取识别码，" if url else "请先在查询页获取识别码，"
            yield _plain(event, hint + "然后发送：/验证码 识别码")
            return
        if not self._portal_server.running:
            await self.sync_portal()
        chat_type, chat_id = _event_chat_identity(event)
        user_id = str(_event_sender_id(event) or "")
        nickname = str(_event_value(event, "get_sender_name", "sender_name") or "")
        yield _plain(event, self.portal_issue_code(code, chat_type, chat_id, user_id, nickname))

    @filter.command("查找")
    async def find(self, event: AstrMessageEvent, name: str = ""):
        if not self.is_event_allowed(event):
            return
        query = _parse_command_text(event, "查找", name)
        if not query:
            yield _plain(event, "用法：/查找 姓名（支持通配符：/查找 张* 、/查找 王小?）")
            return
        if _has_wildcard(query):
            yield _plain(event, _format_wildcard_result(self.people.wildcard_search(query)))
            return
        matches = self._lookup_matches(query)
        private = self._private_upload_ready(event)
        merged_result = self._merged_forward_result(event, query, matches) if _as_bool(self.config.get("merged_forward_results", False)) else None
        if merged_result is not None:
            yield merged_result
        else:
            yield _plain(event, _format_result(query, matches, self.panel_base(), hidden_partial=self.people.last_hidden_partial))
            for chain in self._resource_chains(event, matches, include_files=not private):
                yield chain
        if private:
            failed = await self._upload_private_files(event, matches)
            if failed:
                yield _plain(event, "以下文件私聊发送失败，请到群里查询或用网页下载：" + "、".join(failed))

    @filter.command("图片转表格")
    async def image_table(self, event: AstrMessageEvent, artifact_id: str = ""):
        if not self.is_event_allowed(event):
            return
        query = _parse_command_text(event, "图片转表格", artifact_id)
        result = await self.image_to_table(query.strip())
        if not result.get("ok"):
            yield _plain(event, str(result.get("message")))
            return
        artifact = result.get("artifact") or {}
        yield _plain(event, f"图片已转换为表格：{artifact.get('name')}，文件编号 {artifact.get('id')}。来源：{result.get('source')}")

    @filter.command("文件面板")
    async def file_panel(self, event: AstrMessageEvent):
        if not self.is_event_allowed(event):
            return
        url = self.panel_base() + "/"
        latest = self.latest_progress()
        suffix = f"\n{_progress_text(latest)}" if latest else ""
        configured = bool(str(self.config.get("panel_public_url") or "").strip())
        address_label = "直达地址" if configured else "旧版兼容地址"
        yield _plain(
            event,
            "文件管理面板已注册到 AstrBot 网页端。\n"
            "打开方式：插件 -> 姓名文件查找器 -> 文件管理与预览。\n"
            f"{address_label}：{url}"
            f"{suffix}",
        )

    @filter.command("捕获进度")
    async def capture_progress(self, event: AstrMessageEvent, progress_id: str = ""):
        if not self.is_event_allowed(event):
            return
        query = _parse_command_text(event, "捕获进度", progress_id)
        progress = self.get_progress(query.strip())
        yield _plain(event, _progress_text(progress))

    async def terminate(self) -> None:
        """AstrBot calls terminate on unload; indexes are already persisted atomically."""
        self._terminating = True
        try:
            await self._portal_server.stop()
        except Exception as exc:
            _log("debug", "NameSearcher portal stop failed: %s", exc)
        task = self._capture_window_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._capture_window_task = None
        self._capture_window_entries = []
        self._capture_window_progress_id = ""
        background_tasks = [task for task in self._background_tasks if not task.done()]
        for background_task in background_tasks:
            background_task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._scheduled_upload_ids.clear()
        self._processing_upload_ids.clear()
        with self._progress_lock:
            for progress in self._progress_runs.values():
                if progress.get("status") in {"queued", "collecting", "running", "paused"}:
                    progress["status"] = "cancelled"
                    progress["stage"] = "cancelled"
                    progress["updated_at"] = _now()


# Some AstrBot versions discover a module-level web app factory instead of a
# method on Star.  Exposing this keeps the panel available on those versions.
def get_webui(plugin: Optional[NameSearcherPlugin] = None) -> Any:
    return _build_web_app(plugin) if plugin is not None else None


def get_plugin_page() -> Path:
    """Return the static plugin-page directory used by newer AstrBot hosts."""

    return PLUGIN_PAGE_ROOT


