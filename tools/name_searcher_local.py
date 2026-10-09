"""NameSearcher 本地识别工具。

在自己的电脑上用和插件完全相同的识别代码处理一个文件夹，然后打包成
“识别包”，到插件页点“导入识别包”即可。服务器只做校验和写索引，不再跑 OCR。

用法：
    python name_searcher_local.py run  <文件夹>      识别并打包（最常用）
    python name_searcher_local.py scan <文件夹>      只识别（可随时中断，重跑会自动跳过已完成的文件）
    python name_searcher_local.py pack               把已识别的结果打包
    python name_searcher_local.py status             查看工作目录里的识别情况

常用参数：
    --work DIR        工作目录（默认：本工具旁边的 ns_local_work）
    --out FILE        识别包输出路径（默认：工作目录下 bundle-时间.zip）
    --lang LANG       Tesseract 语言（默认 chi_sim+eng；RapidOCR 不受影响）
    --max-mb N        单文件大小上限（默认 200 MB）
    --no-archives     不展开压缩包
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent):
    if (candidate / "main.py").exists():
        sys.path.insert(0, str(candidate))
        break

import main as ns  # noqa: E402  (插件本体，提供识别代码)

SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _plugin(work: Path, args: argparse.Namespace) -> "ns.NameSearcherPlugin":
    config = {
        "storage_dir": str((work / "store").resolve()),
        "people_file": "",
        "restrict_chat_scope": False,
        "ocr_lang": args.lang,
        "max_file_size_mb": args.max_mb,
        "vision_enabled": False,
        "llm_review_enabled": False,
        "capture_progress_notify": False,
        "request_review_on_low_confidence": False,
        "notify_encoding_issues": False,
        "archive_extraction_enabled": not args.no_archives,
        "capture_window_enabled": False,
        "min_free_memory_mb": 0,
    }
    return ns.NameSearcherPlugin(object(), config)


def _iter_files(folder: Path):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name.lower() not in SKIP_NAMES and not path.name.startswith("~$"):
            yield path


async def _scan(folder: Path, work: Path, args: argparse.Namespace) -> int:
    plugin = _plugin(work, args)
    files = list(_iter_files(folder))
    if not files:
        print(f"{folder} 里没有文件")
        return 1
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    event = ns.PanelUploadEvent()
    started = time.time()
    failed = 0
    print(f"共 {len(files)} 个文件，工作目录 {work}")
    for index, path in enumerate(files, 1):
        relative = path.relative_to(folder).as_posix()
        source = {
            "sender_id": "local-tool",
            "sender_name": "本地识别工具",
            "message_id": f"local-{run_id}",
            "unified_msg_origin": "local-tool",
            "chat_type": "local",
            "upload_source": "local-tool",
            "relative_path": relative,
        }
        mime = ns._mime_for(path)
        candidate = ns.AttachmentCandidate(
            name=path.name,
            kind=ns._kind_for(path.name, mime),
            mime=mime,
            value=str(path),
            source=source,
            type_hint="local-tool",
        )
        t0 = time.time()
        try:
            records = await plugin._process_candidates(event, [candidate], finalize=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # 一个文件出错不影响其他文件
            failed += 1
            print(f"[{index}/{len(files)}] ✗ {relative}：{exc}")
            continue
        record = records[0] if records else {}
        status = ns._public_record(record).get("extraction_status") if record else "未处理"
        names = {"extracted": "已识别", "no_text": "无文字", "stored_only": "仅保存", "failed": "识别失败", "unavailable": "读取失败"}
        label = names.get(str(status), str(status))
        if status in {"failed", "unavailable"}:
            failed += 1
        extra = f"，含 {len(records) - 1} 个压缩包成员" if len(records) > 1 else ""
        print(f"[{index}/{len(files)}] {label} {relative}（{time.time() - t0:.1f}s{extra}）")
    print(f"完成：{len(files)} 个文件，{failed} 个失败，用时 {time.time() - started:.0f}s")
    return 0


def _pack(work: Path, out: Path | None, label: str = "") -> Path:
    store = ns.ArtifactStore(work / "store")
    records = [item for item in store.list() if item.get("download_status") == "ok" and item.get("path")]
    if not records:
        raise SystemExit("工作目录里还没有识别结果，先运行 scan")
    out = out or work / f"bundle-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest_records = []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for record in records:
            path = store.path_for(record)
            if not path or not path.exists():
                continue
            member = f"files/{path.name}"
            bundle.write(path, member)
            item = {key: value for key, value in record.items() if key != "path"}
            item["bundle_file"] = member
            manifest_records.append(item)
        manifest = {
            "format": ns.BUNDLE_FORMAT,
            "version": ns.BUNDLE_VERSION,
            "plugin_version": ns.PLUGIN_VERSION,
            "created_at": ns._now(),
            "label": label or out.name,
            "records": manifest_records,
        }
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, default=ns._json_default))
    size = out.stat().st_size / 1024 / 1024
    print(f"已打包 {len(manifest_records)} 个文件 → {out}（{size:.1f} MB）")
    print("下一步：打开 AstrBot 插件页，点“导入识别包”选择这个文件。")
    return out


def _status(work: Path) -> None:
    store = ns.ArtifactStore(work / "store")
    counts: dict[str, int] = {}
    for item in store.list():
        key = str(item.get("extraction_status") or item.get("download_status"))
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"工作目录": str(work), "文件数": sum(counts.values()), "状态": counts}, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    parser = argparse.ArgumentParser(description="NameSearcher 本地识别工具")
    parser.add_argument("command", choices=["run", "scan", "pack", "status"])
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--work", default=str(HERE / "ns_local_work"))
    parser.add_argument("--out")
    parser.add_argument("--lang", default="chi_sim+eng")
    parser.add_argument("--max-mb", type=float, default=200)
    parser.add_argument("--no-archives", action="store_true")
    args = parser.parse_args(argv)
    work = Path(args.work).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    if args.command in {"run", "scan"}:
        if not args.folder:
            parser.error("请给出要识别的文件夹")
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            parser.error(f"{folder} 不是文件夹")
        try:
            code = asyncio.run(_scan(folder, work, args))
        except KeyboardInterrupt:
            print("\n已中断。重新运行同样的命令会跳过已完成的文件。")
            return 130
        if code or args.command == "scan":
            return code
        _pack(work, Path(args.out) if args.out else None, folder.name)
        return 0
    if args.command == "pack":
        _pack(work, Path(args.out) if args.out else None)
        return 0
    _status(work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
