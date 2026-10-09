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
from typing import Any, Iterable, Iterator, Mapping, Optional


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
PLUGIN_VERSION = "1.7.1"
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
    "vision_review_max_pages": 4,
    "vision_consensus_enabled": True,
    "vision_consensus_prompt": "请综合本地解析和多个视觉模型结果，逐行核对姓名、编号、班级、专业及表格列关系。不得概括、删行或合并不同人员；保留完整可检索原文和表格结构，只输出最终校正文本。",
    "vision_prompt": "请完整识别这份文件页面中的正文、表格和图片文字，逐行保留姓名、编号、班级、专业及所有单元格关系。不得概括、删行或合并不同人员，只输出可检索的完整原文。",
    "vision_timeout_seconds": 60,
    "capture_window_enabled": True,
    "capture_interval_seconds": 30,
    "capture_progress_notify": True,
    "notify_encoding_issues": True,
    "archive_extraction_enabled": True,
    "archive_max_depth": 3,
    "archive_max_members": 300,
    "archive_max_total_size_mb": 200,
    "llm_review_enabled": False,
    "llm_review_provider": "",
    "llm_review_model": "",
    "llm_review_prompt": "请审核下面的文件识别结果。判断是否包含可检索的人名及班级、专业等信息；如有明显错误，请给出修正后的简洁文本。只输出审核结论和修正文本。\n\n文件名：{name}\n识别结果：{text}",
    "llm_review_timeout_seconds": 60,
    "panel_mount_path": "/api/name-searcher",
    "panel_public_url": "",
    "panel_access_token": "",
    "name_match_threshold": 0.85,
    "min_free_memory_mb": 200,
    "import_max_bundle_mb": 2048,
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


def _mime_for(path: Path, hint: str = "") -> str:
    return hint or MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


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
            record, outcome = store.import_record(adopted, content)
            summary[outcome] += 1
            old_id = str(fields.get("id") or "")
            if old_id:
                id_map[old_id] = str(record.get("id"))
            if outcome != "exists":
                touched.append(str(record.get("id")))
            if len(touched) and len(touched) % 200 == 0:
                store.save()
        store.remap_archive_ids(id_map, touched)
        store.save()
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
    for item in result or []:
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


def _iter_nested(value: Any, depth: int = 0, seen: Optional[set[int]] = None) -> Iterator[Any]:
    if depth > 8 or value is None:
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

    if depth > 8 or value is None:
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


async def _resolve_qq_file_urls(event: Any, roots: Iterable[Any]) -> None:
    """Add downloadable URLs to OneBot file segments inside forward nodes.

    NapCat frequently returns only ``file_id`` in ``get_forward_msg``. The
    objects are mutable JSON dictionaries, so enriching them here lets the
    regular attachment extractor handle the result without QQ-specific logic.
    """

    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        return
    chat_type, chat_id = _event_chat_identity(event)
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None) if message_obj is not None else None
    self_id = _object_value(raw_message, "self_id")
    routing = {"self_id": self_id} if self_id not in (None, "") else {}
    seen: set[str] = set()
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
            actions: list[tuple[str, dict[str, Any]]] = []
            if chat_type == "group" and chat_id:
                actions.append(("get_group_file_url", {"file_id": file_id, "group_id": chat_id}))
            else:
                actions.append(("get_private_file_url", {"file_id": file_id}))
            for action, kwargs in actions:
                try:
                    result = call_action(action=action, **kwargs, **routing)
                    if inspect.isawaitable(result):
                        result = await result
                except Exception as exc:
                    _log("debug", "Unable to resolve QQ forwarded file %s: %s", file_id, exc)
                    continue
                result_data = result.get("data") if isinstance(result, Mapping) and isinstance(result.get("data"), Mapping) else result
                if not isinstance(result_data, Mapping):
                    continue
                url = result_data.get("url") or result_data.get("file_url") or result_data.get("download_url")
                if url:
                    data["url"] = url
                    data.setdefault("name", result_data.get("file_name") or result_data.get("name") or data.get("file") or file_id)
                    break


def _read_candidate(candidate: AttachmentCandidate, timeout: int, max_bytes: int) -> tuple[Optional[bytes], str]:
    value = candidate.value
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
                    if len(members) >= max_members:
                        skipped += 1
                        continue
                    safe = _safe_archive_member(info.filename)
                    if safe is None:
                        errors.append(f"跳过不安全路径：{info.filename}")
                        continue
                    if info.is_dir():
                        continue
                    # Unix mode 0o120000 marks symlinks in ZIP metadata.
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        errors.append(f"跳过符号链接：{safe}")
                        continue
                    if info.file_size < 0 or info.file_size > max_total - declared_total:
                        skipped += 1
                        errors.append(f"跳过超出解压大小限制：{safe}")
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
                        if safe is None or len(allowed) >= max_members or size < 0 or size > max_total - declared_total:
                            skipped += 1
                            errors.append(f"跳过不安全或超限成员：{member_name}")
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
                        if len(members) >= max_members:
                            skipped += 1
                            continue
                        safe = _safe_archive_member(info.filename)
                        if safe is None or info.isdir():
                            if safe is None:
                                errors.append(f"跳过不安全路径：{info.filename}")
                            continue
                        size = int(getattr(info, "file_size", 0) or 0)
                        if size < 0 or size > max_total - declared_total:
                            skipped += 1
                            errors.append(f"跳过超出解压大小限制：{safe}")
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
                    if len(members) >= max_members:
                        skipped += 1
                        continue
                    safe = _safe_archive_member(info.name)
                    if safe is None:
                        errors.append(f"跳过不安全路径：{info.name}")
                        continue
                    if not info.isfile():
                        continue
                    if info.size < 0 or info.size > max_total - declared_total:
                        skipped += 1
                        errors.append(f"跳过超出解压大小限制：{safe}")
                        continue
                    try:
                        handle = archive.extractfile(info)
                        if handle is not None:
                            payload = handle.read(max_total - declared_total + 1)
                            if len(payload) > max_total - declared_total:
                                skipped += 1
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
                errors.append(f"跳过超出解压大小限制：{name}")
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
        return ArchiveExpansion([], [f"达到嵌套深度上限：{name}"])
    pending = _archive_reader_members(name, content, max_members, max_total)
    output: list[tuple[str, bytes]] = []
    errors = list(pending[1])
    total = 0
    skipped = pending[2]
    for member_name, payload in pending[0]:
        if len(output) >= max_members:
            skipped += 1
            continue
        if len(payload) > max_total - total:
            skipped += 1
            errors.append(f"跳过超出解压大小限制：{member_name}")
            continue
        total += len(payload)
        output.append((member_name, payload))
        if _kind_for(member_name, "") == "archive" and depth + 1 < max_depth:
            nested = _expand_archive_bytes(member_name, payload, config, depth=depth + 1)
            errors.extend(nested.errors)
            skipped += nested.skipped
            for nested_name, nested_payload in nested.members:
                if len(output) >= max_members or len(nested_payload) > max_total - total:
                    skipped += 1
                    continue
                total += len(nested_payload)
                output.append((f"{member_name}/{nested_name}", nested_payload))
    return ArchiveExpansion(output, errors, skipped)


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
    if expansion.skipped:
        expansion.errors.append(f"跳过 {expansion.skipped} 个超出归档限制的成员")
    return candidates, expansion.errors


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


def _parse_vision_targets(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    raw = config.get("vision_model_targets", "")
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
        if target not in seen:
            seen.add(target)
            targets.append(target)
    if not targets:
        targets.append((
            str(config.get("vision_provider") or "").strip(),
            str(config.get("vision_model") or "").strip(),
        ))
    return targets


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
    max_pages = max(1, min(12, int(max_pages or 4)))
    if suffix == ".pdf":
        try:
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


def _review_notice(record: Mapping[str, Any]) -> str:
    if record.get("llm_review_status") == "completed":
        return f"文件“{record.get('name')}”已完成 LLM 审核：{record.get('llm_review_text')}"
    return f"文件“{record.get('name')}”识别置信度较低，LLM 审核暂不可用。请重新发送更清晰或可解析的原文件。"


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
        "vision_models_completed", "vision_consensus_status", "llm_review_status",
        "encoding_issue", "encoding_status", "source_encoding", "target_encoding",
        "encoding_repair", "sheet_count", "sheet_names", "archive_member_count",
        "archive_extraction_status", "archive_errors", "processing_status",
        "processing_updated_at", "processing_error", "upload_progress_id",
    )
    item = {key: record.get(key) for key in public_fields if key in record}
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    item["mime_type"] = record.get("mime") or "application/octet-stream"
    item["source_name"] = source.get("sender_name") or source.get("sender_id") or source.get("unified_msg_origin") or "未知来源"
    item["ocr_excerpt"] = str(record.get("ocr_text") or "")[:2000]
    return item


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
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"error": f"读取预览失败：{exc}"}, 500
    # Bridge JSON is not a good transport for very large originals. Resize a
    # preview when Pillow is available and retain the original for download.
    if len(raw) > 4 * 1024 * 1024:
        try:
            from PIL import Image  # type: ignore

            with Image.open(io.BytesIO(raw)) as image:
                image.thumbnail((1800, 1800))
                output = io.BytesIO()
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(output, format="JPEG", quality=86, optimize=True)
                raw = output.getvalue()
                mime = "image/jpeg"
        except Exception:
            if len(raw) > 12 * 1024 * 1024:
                return {"error": "图片过大，请下载后查看"}, 413
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return {"id": record.get("id"), "name": record.get("name"), "data_url": data_uri}, 200


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
        "queued": "已排队", "collecting": "捕获窗口收集中", "running": "进行中",
        "completed": "已完成", "failed": "失败", "cancelled": "已取消",
    }.get(status, status)
    stage = str(progress.get("stage") or "")
    stage_name = {
        "collecting": "收集附件", "downloading": "下载文件", "extracting": "本地解析",
        "vision_review": "视觉模型审核", "consensus": "多模型共识",
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
                payload, status = _preview_payload(
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
        payload, status = _preview_payload(plugin.store, request.path_params.get("artifact_id", ""))
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
        Route("/api/files/upload", upload_files, methods=["POST"]),
        Route("/api/files/{artifact_id}/table", table, methods=["POST"]),
        Route("/api/files/{artifact_id}/preview", preview_file, methods=["GET"]),
        Route("/api/files/{artifact_id}/content", get_file, methods=["GET"]),
        Route("/api/files/{artifact_id}", get_file, methods=["GET"]),
        Route("/api/config", config, methods=["GET", "PUT", "PATCH", "POST"]),
    ]
    return Starlette(routes=routes)


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
        self.web_app = _build_web_app(self)
        self._register_page_apis()
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
            if record.get("vision_status") not in {"extracted", "no_review_pages", "unavailable"}:
                return False
        if _as_bool(self.config.get("llm_review_enabled", False)):
            if record.get("llm_review_status") not in {"completed", "unavailable"}:
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
            (f"/{PLUGIN_ID}/files/<artifact_id>/table", self._page_image_table, ["POST"], "Convert image to table"),
            (f"/{PLUGIN_ID}/import/chunk", self._page_import_chunk, ["POST"], "Upload a recognition bundle chunk"),
            (f"/{PLUGIN_ID}/import/finish", self._page_import_finish, ["POST"], "Import an uploaded recognition bundle"),
            (f"/{PLUGIN_ID}/import/scan", self._page_import_scan, ["POST"], "Import bundles from the inbox folder"),
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

    def import_staged(self, upload_id: str, label: str = "") -> tuple[dict[str, Any], int]:
        if not _IMPORT_ID.match(upload_id):
            return {"ok": False, "message": "upload_id 无效"}, 400
        part = self._import_staging() / f"{upload_id}.part"
        if not part.exists():
            return {"ok": False, "message": "没有找到已上传的识别包"}, 404
        try:
            summary = _import_bundle(self.store, part, {**self.config, "_label": label})
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
                summary = _import_bundle(self.store, bundle, self.config)
                done.mkdir(exist_ok=True)
                bundle.replace(done / bundle.name)
                results.append({"bundle": bundle.name, **summary})
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
            self.import_staged, str(payload.get("upload_id") or ""), str(payload.get("name") or "")
        )
        return _page_json_response(result, status)

    async def _page_import_scan(self) -> Any:
        result = await asyncio.to_thread(self.import_from_inbox)
        return _page_json_response(result)

    async def _page_file_preview(self, artifact_id: str) -> Any:
        payload, status = _preview_payload(self.store, artifact_id)
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
        path = self.store.path_for(record)
        if not path or not path.exists():
            return record
        source = _event_source(event)
        session_id = str(source.get("unified_msg_origin") or source.get("message_id") or "name-searcher")
        existing = str(record.get("ocr_text") or "").strip()
        targets = _parse_vision_targets(self.config)
        max_pages = max(1, min(12, int(self.config.get("vision_review_max_pages", 4) or 4)))
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

        updates = {
            "ocr_text_local": existing[:200000],
            "ocr_text": final_text[:200000],
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
        """Audit extracted text with the configured AstrBot text model.

        The original local/vision output is retained for diagnostics. The
        reviewed text becomes the searchable value because the model prompt
        explicitly asks for concise corrected text.
        """
        if not _as_bool(self.config.get("llm_review_enabled", False)):
            return record
        original = str(record.get("ocr_text") or "").strip()
        if not original and record.get("download_status") == "ok":
            return record
        template = str(self.config.get("llm_review_prompt") or DEFAULT_CONFIG["llm_review_prompt"])
        try:
            prompt = template.format(name=str(record.get("name") or "未命名文件"), text=original[:200000])
        except Exception:
            prompt = f"请审核文件 {record.get('name') or '未命名文件'} 的识别结果并输出可检索文本：\n{original[:200000]}"
        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        session_id = str(source.get("unified_msg_origin") or source.get("message_id") or "name-searcher-review")
        try:
            reviewed, engine = await _call_text_model(
                self.context,
                prompt,
                provider_name=str(self.config.get("llm_review_provider") or "").strip(),
                model_name=str(self.config.get("llm_review_model") or "").strip(),
                timeout=int(self.config.get("llm_review_timeout_seconds", 60) or 60),
                session_id=session_id,
            )
        except Exception as exc:
            _log("debug", "LLM review failed: %s", exc)
            reviewed, engine = "", ""
        if not reviewed:
            updates = {"llm_review_status": "unavailable", "llm_review_engine": engine}
            self.store.update(record["id"], **updates)
            record.update(updates)
            return record
        updates = {
            "ocr_text_before_llm_review": original[:200000],
            "ocr_text": reviewed[:200000],
            "llm_review_text": reviewed[:200000],
            "llm_review_status": "completed",
            "llm_review_engine": engine,
            "ocr_engine": "llm-review",
            "ocr_confidence": max(float(record.get("ocr_confidence") or 0), 0.9),
            "extraction_status": "extracted" if reviewed.strip() else record.get("extraction_status"),
        }
        self.store.update(record["id"], **updates)
        record.update(updates)
        return record

    async def _extract_event_candidates(self, event: AstrMessageEvent) -> list[AttachmentCandidate]:
        if not _event_can_capture(event, self.config):
            return []
        forward_payloads = await _fetch_forward_payloads(event)
        await _resolve_qq_file_urls(event, forward_payloads)
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
        while queue:
            candidate = queue.pop(0)
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
                            "archive_extraction_status": "extracted" if archive_children else ("failed" if archive_errors else "empty"),
                        }
                        if archive_errors:
                            archive_updates["archive_errors"] = archive_errors[:50]
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
            if self.config.get("request_review_on_low_confidence", True) and needs_review:
                await _send(event, _review_notice(record))
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

    def _resource_chain(self, event: Any, matches: list[dict[str, Any]]) -> Any:
        """Build a native message chain containing matched local resources."""

        chain_result = getattr(event, "chain_result", None)
        if not callable(chain_result):
            return None
        try:
            import astrbot.api.message_components as Comp  # type: ignore
        except Exception:
            return None
        components: list[Any] = []
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
            name = str(record.get("name") or path.name)
            components.append(Comp.Plain(f"来源文件资源：{name}\n"))
            try:
                if record.get("kind") == "image":
                    components.append(Comp.Image.fromFileSystem(str(path)))
                else:
                    components.append(Comp.File(file=str(path), name=name))
            except Exception as exc:
                _log("debug", "Unable to build resource component for %s: %s", name, exc)
        if not components:
            return None
        try:
            return chain_result(components)
        except Exception as exc:
            _log("debug", "Unable to build lookup resource chain: %s", exc)
            return None

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
            if record and path and path.exists():
                try:
                    if record.get("kind") == "image":
                        content.append(Comp.Image.fromFileSystem(str(path)))
                    else:
                        content.append(Comp.File(file=str(path), name=str(record.get("name") or path.name)))
                except Exception as exc:
                    _log("debug", "Unable to attach merged-forward resource %s: %s", artifact_id, exc)
            nodes.append(Comp.Node(content=content, name=node_name, uin="0"))
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

    @filter.command("查找")
    async def find(self, event: AstrMessageEvent, name: str = ""):
        if not self.is_event_allowed(event):
            return
        query = _parse_command_text(event, "查找", name)
        if not query:
            yield _plain(event, "用法：/查找 姓名")
            return
        matches = self._lookup_matches(query)
        if _as_bool(self.config.get("merged_forward_results", False)):
            merged_result = self._merged_forward_result(event, query, matches)
            if merged_result is not None:
                yield merged_result
                return
        yield _plain(event, _format_result(query, matches, self.panel_base(), hidden_partial=self.people.last_hidden_partial))
        resource_chain = self._resource_chain(event, matches)
        if resource_chain is not None:
            yield resource_chain

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
                if progress.get("status") in {"queued", "collecting", "running"}:
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


