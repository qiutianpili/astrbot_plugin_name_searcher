"""NameSearcher 本地识别工具。

在自己的电脑上用和插件完全相同的识别代码处理一个文件夹，然后打包成
“识别包”，到插件页点“导入识别包”即可。服务器只做校验和写索引，不再跑 OCR。

每次识别都是独立的一次：结果放在 工作目录/runs/时间/ 下，打包只包含本次的文件，
不会把以前识别过的文件再打进去。

用法：
    python name_searcher_local.py run  <文件或文件夹>...   识别并打包（最常用，可同时给多个文件/文件夹）
    python name_searcher_local.py scan <文件或文件夹>...   只识别
    python name_searcher_local.py pack                    把最近一次的识别结果打包（--run 指定某一次）
    python name_searcher_local.py status                  查看最近一次的识别情况

常用参数：
    --work DIR        工作目录（默认：本工具旁边的 ns_local_work）
    --run DIR         pack/status 时指定某一次识别的目录（默认最近一次）
    --out FILE        识别包输出路径（默认：工作目录下 bundle-时间.zip）
    --lang LANG       Tesseract 语言（默认 chi_sim+eng；RapidOCR 不受影响）
    --max-mb N        单文件大小上限（默认 200 MB）
    --no-archives     不展开压缩包
    --archive-max-depth N / --archive-max-members N / --archive-max-size-mb N
                      压缩包展开上限（默认 3 层 / 300 个 / 200 MB）；超出的成员会在日志里列出
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


def _new_run(work: Path) -> Path:
    """为本次识别新建独立目录：work/runs/YYYYmmdd-HHMMSS[-n]。"""
    base = work / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run = base
    index = 1
    while run.exists():
        index += 1
        run = base.with_name(f"{base.name}-{index}")
    run.mkdir(parents=True)
    return run


def _latest_run(work: Path) -> Path | None:
    runs = work / "runs"
    candidates = sorted((p for p in runs.glob("*") if (p / "store").is_dir()), key=lambda p: p.name) if runs.is_dir() else []
    if candidates:
        return candidates[-1]
    if (work / "store").is_dir():  # 1.7.x 旧版工作目录
        return work
    return None


def _plugin(run: Path, args: argparse.Namespace) -> "ns.NameSearcherPlugin":
    config = {
        "storage_dir": str((run / "store").resolve()),
        "people_file": "",
        "restrict_chat_scope": False,
        "ocr_lang": args.lang,
        "max_file_size_mb": args.max_mb,
        "vision_enabled": False,
        "llm_review_enabled": False,
        "capture_progress_notify": False,
        "request_review_on_low_confidence": False,
        "notify_encoding_issues": False,
        "notify_archive_limits": False,
        "archive_extraction_enabled": not args.no_archives,
        "archive_max_depth": int(getattr(args, "archive_max_depth", 3) or 3),
        "archive_max_members": int(getattr(args, "archive_max_members", 300) or 300),
        "archive_max_total_size_mb": float(getattr(args, "archive_max_size_mb", 200) or 200),
        "capture_window_enabled": False,
        "min_free_memory_mb": 0,
    }
    return ns.NameSearcherPlugin(object(), config)


def _wanted(path: Path) -> bool:
    return path.is_file() and path.name.lower() not in SKIP_NAMES and not path.name.startswith("~$")


def _collect(inputs) -> list[tuple[Path, str]]:
    """把文件和文件夹展开成 (文件, 相对路径) 列表，去重并保持顺序。"""
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    seen: set[str] = set()
    files: list[tuple[Path, str]] = []
    for item in inputs:
        item = Path(item).expanduser()
        if item.is_dir():
            pairs = [(p, f"{item.name}/{p.relative_to(item).as_posix()}") for p in sorted(item.rglob("*")) if _wanted(p)]
        elif _wanted(item):
            pairs = [(item, item.name)]
        else:
            pairs = []
        for path, relative in pairs:
            key = str(path.resolve()).lower() if os.name == "nt" else str(path.resolve())
            if key not in seen:
                seen.add(key)
                files.append((path, relative))
    return files


def _label_for(inputs) -> str:
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    paths = [Path(p) for p in inputs]
    if len(paths) == 1:
        return paths[0].stem if paths[0].is_file() else paths[0].name
    return f"{paths[0].stem if paths[0].is_file() else paths[0].name} 等 {len(paths)} 项" if paths else ""


async def _scan(inputs, run: Path, args: argparse.Namespace, log=print, progress=None, should_stop=None) -> int:
    """识别 inputs（文件和/或文件夹）到本次识别目录 run。

    progress(index, total, status) 每个文件后调用；should_stop() 为真时在文件之间停下，返回 130。
    """
    plugin = _plugin(run, args)
    files = _collect(inputs)
    if not files:
        log("没有可识别的文件")
        return 1
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    event = ns.PanelUploadEvent()
    started = time.time()
    failed = 0
    limited = 0
    log(f"共 {len(files)} 个文件，本次识别目录 {run}")
    for index, (path, relative) in enumerate(files, 1):
        if should_stop and should_stop():
            log(f"已停止：完成 {index - 1}/{len(files)}。可点“打包本次结果”只打包已完成的部分。")
            return 130
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
            log(f"[{index}/{len(files)}] ✗ {relative}：{exc}")
            if progress:
                progress(index, len(files), "failed")
            continue
        record = records[0] if records else {}
        status = ns._public_record(record).get("extraction_status") if record else "未处理"
        names = {"extracted": "已识别", "no_text": "无文字", "stored_only": "仅保存", "failed": "识别失败", "unavailable": "读取失败"}
        label = names.get(str(status), str(status))
        if record.get("archive_extraction_status") == "partial":
            label = "部分解压"
        if status in {"failed", "unavailable"}:
            failed += 1
        extra = f"，含 {len(records) - 1} 个压缩包成员" if len(records) > 1 else ""
        log(f"[{index}/{len(files)}] {label} {relative}（{time.time() - t0:.1f}s{extra}）")
        notice = ns._archive_notice(records)
        if notice:
            limited += 1
            for line in notice.splitlines()[:-1]:
                log(f"    ⚠ {line}")
        if progress:
            progress(index, len(files), str(status))
    log(f"完成：{len(files)} 个文件，{failed} 个失败，用时 {time.time() - started:.0f}s")
    if limited:
        log(f"注意：{limited} 个压缩包有成员超出层数、个数或大小上限未识别。可先解压再识别，或用命令行参数 --archive-max-depth / --archive-max-members / --archive-max-size-mb 调高上限。")
    return 0


def _pack(run: Path, out: Path | None, label: str = "", log=print) -> Path:
    """只打包 run（某一次识别）里的文件。识别包默认放在工作目录下。"""
    store = ns.ArtifactStore(run / "store")
    records = [item for item in store.list() if item.get("download_status") == "ok" and item.get("path")]
    if not records:
        raise SystemExit("这次识别还没有结果，先运行 scan")
    work = run.parent.parent if run.parent.name == "runs" else run
    out = out or work / f"bundle-{run.name if run is not work else datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
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
    digest, sidecar = _write_sha256(out)
    log(f"已打包 {len(manifest_records)} 个文件 → {out}（{size:.1f} MB）")
    log(f"SHA256 识别码：{digest}")
    log(f"识别码已保存为 {sidecar.name}。")
    log("下一步：打开 AstrBot 插件页，点“导入识别包”，把 zip 和 .sha256 一起选上（或按提示粘贴识别码）；服务器先校验 SHA256 再导入。")
    return out


def _write_sha256(bundle: Path) -> tuple[str, Path]:
    """写出 sha256sum 格式的识别码文件：<bundle>.zip.sha256。"""
    digest = ns._file_sha256(bundle)
    sidecar = bundle.with_name(bundle.name + ".sha256")
    sidecar.write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
    return digest, sidecar


def _status_counts(run: Path | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if run is None or not (run / "store").is_dir():
        return counts
    store = ns.ArtifactStore(run / "store")
    for item in store.list():
        key = str(item.get("extraction_status") or item.get("download_status"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _status(run: Path | None) -> None:
    counts = _status_counts(run)
    print(json.dumps({"识别目录": str(run or "无"), "文件数": sum(counts.values()), "状态": counts}, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    parser = argparse.ArgumentParser(description="NameSearcher 本地识别工具")
    parser.add_argument("command", choices=["run", "scan", "pack", "status"])
    parser.add_argument("inputs", nargs="*", help="要识别的文件或文件夹，可以给多个")
    parser.add_argument("--work", default=str(HERE / "ns_local_work"))
    parser.add_argument("--run", help="pack/status 时指定某一次识别的目录")
    parser.add_argument("--out")
    parser.add_argument("--lang", default="chi_sim+eng")
    parser.add_argument("--max-mb", type=float, default=200)
    parser.add_argument("--no-archives", action="store_true")
    parser.add_argument("--archive-max-depth", type=int, default=3, help="压缩包最多展开几层（默认 3，最大 8）")
    parser.add_argument("--archive-max-members", type=int, default=300, help="每个压缩包最多识别几个成员（默认 300）")
    parser.add_argument("--archive-max-size-mb", type=float, default=200, help="每个压缩包解压总大小上限 MB（默认 200）")
    args = parser.parse_args(argv)
    work = Path(args.work).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    if args.command in {"run", "scan"}:
        if not args.inputs:
            parser.error("请给出要识别的文件或文件夹")
        inputs = [Path(item).expanduser().resolve() for item in args.inputs]
        missing = [str(item) for item in inputs if not item.exists()]
        if missing:
            parser.error("找不到：" + "，".join(missing))
        run = _new_run(work)
        try:
            code = asyncio.run(_scan(inputs, run, args))
        except KeyboardInterrupt:
            print(f"\n已中断。可运行 pack --run \"{run}\" 打包已完成的部分。")
            return 130
        if code or args.command == "scan":
            return code
        _pack(run, Path(args.out) if args.out else None, _label_for(inputs))
        return 0
    run = Path(args.run).expanduser().resolve() if args.run else _latest_run(work)
    if args.command == "pack":
        if run is None:
            raise SystemExit("还没有识别记录，先运行 run 或 scan")
        _pack(run, Path(args.out) if args.out else None)
        return 0
    _status(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
