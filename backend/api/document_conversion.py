"""Offline document conversion with a bounded lifetime for binary parsers."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

from .file_paths import fs_path


SUPPORTED_RAW_SUFFIXES = {".doc", ".docx", ".xlsx", ".xls", ".pptx", ".pdf", ".md", ".txt", ".csv", ".html", ".htm"}
CONVERTER_VERSION = "markitdown-0.1.9-direct2-doc"
BINARY_SUFFIXES = {".doc", ".docx", ".xlsx", ".xls", ".pptx", ".pdf"}
_LOG_LOCK = threading.Lock()


def conversion_error_details(error):
    """Bounded root causes, including MarkItDown's wrapped converter attempts."""
    pending, seen, details = [error], set(), []
    while pending and len(details) < 6:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        details.append({"type": type(current).__name__, "message": " ".join(str(current).split())[:600]})
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        for attempt in (getattr(current, "attempts", None) or [])[:3]:
            info = getattr(attempt, "exc_info", None)
            if info and isinstance(info[1], BaseException):
                pending.append(info[1])
    return details


def conversion_event(stage, path, **fields):
    with _LOG_LOCK:
        print("CONVERT " + json.dumps({"stage": stage, "file": Path(path).name,
                                       "suffix": Path(path).suffix.lower(), **fields}, ensure_ascii=False), flush=True)


def _convert_legacy_word(path):
    configured = os.getenv("LIBREOFFICE_PATH", "").strip()
    candidates = [configured] if configured else [shutil.which("soffice"), shutil.which("libreoffice"),
        "C:/Program Files/LibreOffice/program/soffice.exe", "C:/Program Files (x86)/LibreOffice/program/soffice.exe"]
    executable = next((value for value in candidates if value and Path(value).is_file()), None)
    if not executable:
        raise RuntimeError("DOC 解析需要 LibreOffice：请安装 LibreOffice，或将 LIBREOFFICE_PATH 设置为 soffice 可执行文件路径；XLS 无需此组件")
    with tempfile.TemporaryDirectory(prefix="wiki-word-") as directory:
        root = Path(directory)
        # A private profile avoids connecting to an interactive Office instance.
        result = subprocess.run([executable, "-env:UserInstallation=" + (root / "profile").as_uri(),
                                 "--headless", "--convert-to", "docx", "--outdir", str(root), str(path)],
                                capture_output=True, timeout=max(1, int(os.getenv("DOCUMENT_CONVERSION_TIMEOUT", "120"))),
                                check=False)
        converted = root / (path.stem + ".docx")
        if result.returncode or not converted.is_file():
            raise RuntimeError("LibreOffice 未生成 DOCX，请检查 DOC 是否损坏、加密或格式与扩展名不符")
        return convert_to_markdown(converted)


def _convert_direct(path):
    path = fs_path(path)
    if path.suffix.lower() == ".doc":
        return _convert_legacy_word(path)
    try:
        with warnings.catch_warnings():
            # Optional audio import warns even though none of these formats uses ffmpeg.
            warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv.*", category=RuntimeWarning)
            from markitdown import StreamInfo, converters
    except ImportError as exc:
        raise RuntimeError("文档转换依赖未安装，请安装 backend/requirements.txt 后重试") from exc
    converter_type = {
        ".docx": converters.DocxConverter, ".xlsx": converters.XlsxConverter,
        ".xls": converters.XlsConverter, ".pptx": converters.PptxConverter,
        ".pdf": converters.PdfConverter, ".csv": converters.CsvConverter,
        ".html": converters.HtmlConverter, ".htm": converters.HtmlConverter,
    }.get(path.suffix.lower(), converters.PlainTextConverter)
    # The extension is known. Do not create MarkItDown/Magika's ONNX detector
    # for every document, and do not fall back to ZIP listings on corrupt Office files.
    info = StreamInfo(extension=path.suffix.lower(), filename=path.name, local_path=str(path))
    with path.open("rb") as stream:
        return converter_type().convert(stream, info, heading_style="ATX").text_content


def convert_to_markdown(path):
    path = fs_path(Path(path))
    if path.suffix.lower() not in SUPPORTED_RAW_SUFFIXES:
        raise ValueError(f"暂不支持这个文件类型：{path.name}")
    started = time.perf_counter()
    timeout = max(1, int(os.getenv("DOCUMENT_CONVERSION_TIMEOUT", "120")))
    conversion_event("start", path, sizeBytes=path.stat().st_size, timeoutSeconds=timeout)
    try:
        if path.suffix.lower() == ".doc" or path.suffix.lower() not in BINARY_SUFFIXES:
            text = _convert_direct(path)
        else:
            # A stuck parser cannot be stopped safely by a Python thread.
            with tempfile.TemporaryDirectory(prefix="wiki-convert-") as directory:
                output = Path(directory) / "document.md"
                worker = Path(__file__).resolve().parents[1] / "tools" / "convert_document.py"
                stderr_path = Path(directory) / "stderr.log"
                with stderr_path.open("wb") as stderr:
                    process = subprocess.run([sys.executable, str(worker), str(path), str(output)],
                                             stdout=subprocess.DEVNULL, stderr=stderr,
                                             timeout=timeout, check=False,
                                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                if process.returncode != 0:
                    error_path = output.with_suffix(".error.json")
                    error = json.loads(error_path.read_text(encoding="utf-8")) if error_path.exists() else {}
                    detail = " <- ".join(f"{value['type']}: {value['message']}" for value in error.get("causes", []))
                    if not detail:
                        with stderr_path.open("rb") as stderr:
                            stderr.seek(max(0, stderr_path.stat().st_size - 2048))
                            detail = " ".join(stderr.read().decode("utf-8", errors="replace").split())[-1000:]
                    detail = detail or ", ".join(str(error[key]) for key in ("errorType", "causeType", "missingModule") if error.get(key))
                    raise RuntimeError(f"转换子进程失败（{detail or 'exit ' + str(process.returncode)}）")
                text = output.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        conversion_event("timeout", path, seconds=round(time.perf_counter() - started, 3))
        raise ValueError(f"文档转换超时：{path.name}，超过 {timeout} 秒，已终止转换进程；可调大 DOCUMENT_CONVERSION_TIMEOUT 或检查文件") from exc
    except Exception as exc:
        conversion_event("failed", path, seconds=round(time.perf_counter() - started, 3), errorType=type(exc).__name__, detail=str(exc)[:2000])
        raise ValueError(f"文件转 Markdown 失败：{path.name}（{exc}），请检查文件是否损坏、加密或缺少转换依赖") from exc
    conversion_event("done", path, seconds=round(time.perf_counter() - started, 3), outputChars=len(text))
    return text
