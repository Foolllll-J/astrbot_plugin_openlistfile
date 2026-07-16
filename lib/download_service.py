import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Callable, Dict
from xml.etree import ElementTree as ET

import chardet


def normalize_download_headers(headers: Dict) -> Dict[str, str]:
    normalized = {}
    if not isinstance(headers, dict):
        return normalized
    for key, value in headers.items():
        if value is None:
            continue
        if isinstance(value, list):
            values = [str(v) for v in value if v is not None]
            if not values:
                continue
            normalized[key] = (
                "; ".join(values) if key.lower() == "cookie" else ",".join(values)
            )
        else:
            normalized[key] = str(value)
    return normalized


def parse_link_content_length(link: Dict) -> int:
    try:
        value = link.get("content_length") if isinstance(link, dict) else None
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def build_temp_file_path(
    base_dir: str, prefix: str, unique_suffix: str, safe_filename: str
) -> str:
    return str(Path(base_dir) / f"{prefix}_{unique_suffix}_{safe_filename}")


def build_plugin_temp_dir(base_dir: str, purpose: str) -> str:
    return str(Path(base_dir) / "astrbot_plugin_openlistfile" / purpose)


def build_download_link_text(
    file_name: str, file_size_text: str, file_path: str, download_url: str
) -> str:
    return (
        "OpenList 下载链接\n\u200b\n"
        f"文件: {file_name}\n"
        f"路径: {file_path}\n"
        f"大小: {file_size_text}\n"
        f"链接: {download_url}\n"
    )


def decode_text_preview(
    content_bytes: bytes, text_length: int, detector: Callable = None
) -> Dict[str, str]:
    detector = detector or chardet.detect
    detection = detector(content_bytes)
    encoding = detection.get("encoding", "utf-8") or "utf-8"
    confidence = detection.get("confidence", 0)

    try:
        decoded_text = content_bytes.decode(encoding, errors="ignore").strip()
    except Exception:
        encoding = "utf-8"
        decoded_text = content_bytes.decode("utf-8", errors="ignore").strip()

    preview_text = decoded_text[:text_length]
    if len(decoded_text) > text_length:
        preview_text += "\n\u200b\n..."
    return {
        "encoding": encoding,
        "confidence": confidence,
        "text": preview_text,
    }


def extract_epub_preview_text(epub_path: str, max_chars: int = 1000) -> str:
    if not zipfile.is_zipfile(epub_path):
        return "错误：不是有效的 EPUB 文件（无效的 ZIP 结构）。"

    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            container_content = archive.read("META-INF/container.xml")
            container_root = ET.fromstring(container_content)
            container_ns = {"ns": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile = container_root.find(".//ns:rootfile", container_ns)
            if rootfile is None:
                return "错误：EPUB 结构异常，未找到 rootfile。"

            opf_path = rootfile.attrib.get("full-path")
            if not opf_path:
                return "错误：未找到 OPF 文件路径。"

            opf_content = archive.read(opf_path)
            opf_root = ET.fromstring(opf_content)
            opf_ns = {"opf": "http://www.idpf.org/2007/opf"}
            opf_dir = Path(opf_path).parent.as_posix()

            manifest = {}
            for item in opf_root.findall(".//opf:manifest/opf:item", opf_ns):
                item_id = item.attrib.get("id")
                item_href = item.attrib.get("href")
                if item_id and item_href:
                    manifest[item_id] = item_href

            spine_items = []
            for itemref in opf_root.findall(".//opf:spine/opf:itemref", opf_ns):
                idref = itemref.attrib.get("idref")
                if idref in manifest:
                    href = manifest[idref]
                    full_href = (
                        Path(opf_dir, href).as_posix()
                        if opf_dir not in ("", ".")
                        else href
                    )
                    spine_items.append(full_href)

            if not spine_items:
                return "错误：EPUB 内容为空或无法解析阅读顺序。"

            re_scripts = re.compile(
                r"<(script|style).*?>.*?</\1>", re.DOTALL | re.IGNORECASE
            )
            re_block_tags = re.compile(
                r"<(p|div|br|li|h[1-6]|tr|blockquote|section|article).*?>",
                re.IGNORECASE,
            )
            re_tags = re.compile(r"<[^>]+>")
            re_spaces = re.compile(r"[ \t\f\v]+")
            re_newlines = re.compile(r"\n{3,}")

            full_text = []
            current_len = 0

            for item_path in spine_items:
                if current_len >= max_chars * 2:
                    break
                try:
                    html_content = archive.read(item_path).decode(
                        "utf-8", errors="ignore"
                    )
                except Exception:
                    continue

                text = re_scripts.sub("", html_content)
                text = text.replace("\r", " ").replace("\n", " ")
                text = re_block_tags.sub("\n", text)
                text = re_tags.sub("", text)
                text = unescape(text)
                text = re_spaces.sub(" ", text)
                lines = [line.strip() for line in text.split("\n") if line.strip()]
                text = re_newlines.sub("\n\u200b\n", "\n".join(lines)).strip()
                if not text:
                    continue

                full_text.append(text)
                current_len += len(text)

            if not full_text:
                return "错误：EPUB 中未提取到可预览文本。"

            preview_text = "\n\u200b\n".join(full_text)[:max_chars]
            if sum(len(chunk) for chunk in full_text) > max_chars:
                preview_text += "\n\u200b\n..."
            return preview_text
    except Exception as exc:
        return f"错误：EPUB 解析失败：{exc}"
