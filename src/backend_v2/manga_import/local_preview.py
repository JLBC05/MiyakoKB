import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp"}

def natural_sort_key(s: str) -> list[int | str]:
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]

def is_valid_image_filename(filename: str) -> bool:
    name = os.path.basename(filename)
    if name.startswith(".") or name.startswith("__MACOSX"):
        return False
    if name.lower() == "thumbs.db":
        return False
    ext = os.path.splitext(name)[1].lower()
    return ext in IMAGE_EXTENSIONS

def parse_comic_info_xml(xml_content: str | bytes) -> dict[str, Any]:
    """Parse ComicInfo.xml metadata standard (with regex fallback for non-strict XML)."""
    result: dict[str, Any] = {}
    if not xml_content:
        return result
    
    text = xml_content.decode("utf-8", errors="replace") if isinstance(xml_content, bytes) else str(xml_content)
    # Strip potential utf-8 bom
    text = text.lstrip("\ufeff").strip()

    try:
        # Strip XML declaration if present to avoid parser issues with encoding/quotes
        clean_xml = re.sub(r"<\?xml.*?\?>", "", text, flags=re.DOTALL).strip()
        root = ET.fromstring(clean_xml)
        number_elem = root.find("Number")
        if number_elem is not None and number_elem.text:
            result["number"] = number_elem.text.strip()
        volume_elem = root.find("Volume")
        if volume_elem is not None and volume_elem.text:
            result["volume"] = volume_elem.text.strip()
        title_elem = root.find("Title")
        if title_elem is not None and title_elem.text:
            result["title"] = title_elem.text.strip()
        series_elem = root.find("Series")
        if series_elem is not None and series_elem.text:
            result["series"] = series_elem.text.strip()
    except Exception:
        pass

    # Regex fallback if ET failed or missed tags
    if not result.get("number"):
        m_num = re.search(r"<Number[^>]*>([^<]+)</Number>", text, re.IGNORECASE)
        if m_num:
            result["number"] = m_num.group(1).strip()
    if not result.get("title"):
        m_title = re.search(r"<Title[^>]*>([^<]+)</Title>", text, re.IGNORECASE)
        if m_title:
            result["title"] = m_title.group(1).strip()
    if not result.get("series"):
        m_series = re.search(r"<Series[^>]*>([^<]+)</Series>", text, re.IGNORECASE)
        if m_series:
            result["series"] = m_series.group(1).strip()

    return result

def extract_chapter_from_filename(name: str) -> Optional[tuple[float, str]]:
    """
    Layer 1: Filename regex matcher.
    Returns (ordinal, normalized_title) or None.
    """
    clean_name = os.path.splitext(name)[0].strip()

    # Pattern 1: 第 01 话/卷/章 [副标题]
    m1 = re.search(r"第\s*(\d+(?:\.\d+)?)\s*[话話卷巻回章期集](?:[\s_—\-:]*(.*))?", clean_name)
    if m1:
        num = float(m1.group(1))
        sub = (m1.group(2) or "").strip()
        sub = re.sub(r"^[—\-:_]+", "", sub).strip()
        title = f"第{int(num) if num.is_integer() else num:02d}话"
        if sub:
            title = f"{title} {sub}"
        return (num, title)

    # Pattern 2: Ch/Chapter/Vol/Volume/Ep/Episode 01 [副标题]
    m2 = re.search(r"(?:^|[\s_—\-(\[])(?:[Cc]h(?:apter)?|[Vv]ol(?:ume)?|[Ee]p(?:isode)?)\.?\s*(\d+(?:\.\d+)?)(?:[\s_—\-:]*(.*))?", clean_name)
    if m2:
        num = float(m2.group(1))
        sub = (m2.group(2) or "").strip()
        sub = re.sub(r"^[—\-:_]+", "", sub).strip()
        title = f"第{int(num) if num.is_integer() else num:02d}话"
        if sub:
            title = f"{title} {sub}"
        return (num, title)

    # Pattern 3: 纯数字开头或结尾，如 "01 两面宿傩", "#01", "001"
    m3 = re.search(r"^[#№]?\s*(\d+(?:\.\d+)?)(?:[\s_—\-:]+(.+))?$", clean_name)
    if m3:
        num = float(m3.group(1))
        sub = (m3.group(2) or "").strip()
        sub = re.sub(r"^[—\-:_]+", "", sub).strip()
        title = f"第{int(num) if num.is_integer() else num:02d}话"
        if sub:
            title = f"{title} {sub}"
        return (num, title)

    # Pattern 4: 中间或括号内包含数字 (如 "[Title][01][720P]")
    m4 = re.findall(r"(?:^|[\s_—\-\[#№])(\d{1,4}(?:\.\d+)?)(?:[\s_—\-\]]|$)", clean_name)
    if m4:
        # 排除 720, 1080, 2020, 2021, 2022, 2023, 2024, 2025 等分辨率或年份
        candidates = []
        for c in m4:
            val = float(c)
            if val in (720, 1080, 2160, 480) or (1980 <= val <= 2040):
                continue
            candidates.append(val)
        if candidates:
            num = candidates[0]
            title = f"第{int(num) if num.is_integer() else num:02d}话"
            return (num, title)

    return None

def inspect_zip_archive(zip_path: Path) -> dict[str, Any]:
    """Inspect contents of a zip or cbz archive."""
    image_names = []
    comic_info_xml = None
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            fname = info.filename
            if is_valid_image_filename(fname):
                image_names.append(fname)
            elif os.path.basename(fname).lower() == "comicinfo.xml":
                try:
                    comic_info_xml = zf.read(info.filename)
                except Exception:
                    pass

    image_names.sort(key=natural_sort_key)
    meta = parse_comic_info_xml(comic_info_xml) if comic_info_xml else {}
    return {
        "images": image_names,
        "page_count": len(image_names),
        "meta": meta,
        "first_image": image_names[0] if image_names else None,
    }

def inspect_folder(folder_path: Path) -> dict[str, Any]:
    """Inspect images and ComicInfo.xml directly in a folder."""
    image_names = []
    comic_info_xml = None
    for entry in folder_path.iterdir():
        if entry.is_file():
            if is_valid_image_filename(entry.name):
                image_names.append(entry.name)
            elif entry.name.lower() == "comicinfo.xml":
                try:
                    comic_info_xml = entry.read_bytes()
                except Exception:
                    pass

    image_names.sort(key=natural_sort_key)
    meta = parse_comic_info_xml(comic_info_xml) if comic_info_xml else {}
    return {
        "images": image_names,
        "page_count": len(image_names),
        "meta": meta,
        "first_image": str(folder_path / image_names[0]) if image_names else None,
    }

def recognize_chapter(
    name: str,
    meta: dict[str, Any],
    fallback_ordinal: int,
    enable_ai_vision: bool = False,
) -> dict[str, Any]:
    """
    Apply 4-layer recognition:
    Layer 1: Filename regex
    Layer 2: ComicInfo / metadata
    Layer 3: AI Vision (marked or invoked)
    Layer 4: Fallback
    """
    # Layer 1: Filename
    l1 = extract_chapter_from_filename(name)
    if l1:
        ord_val, title = l1
        return {
            "ordinal": ord_val,
            "title": title,
            "recognition_layer": "filename",
            "confidence": "high",
            "needs_review": False,
            "needs_ordinal": False,
        }

    # Layer 2: Metadata (ComicInfo.xml)
    if meta.get("number"):
        try:
            num = float(meta["number"])
            title = f"第{int(num) if num.is_integer() else num:02d}话"
            if meta.get("title"):
                title = f"{title} {meta['title']}"
            return {
                "ordinal": num,
                "title": title,
                "recognition_layer": "metadata",
                "confidence": "high",
                "needs_review": False,
                "needs_ordinal": False,
            }
        except ValueError:
            pass

    # Layer 3 / Layer 4: Needs AI review or Fallback
    # Fallback chapters must have ordinal: None and needs_ordinal: True to avoid unique constraint collisions
    clean_stem = os.path.splitext(name)[0].strip()
    return {
        "ordinal": None,
        "title": f"第?话 ({clean_stem or '待确认'})",
        "recognition_layer": "fallback",
        "confidence": "low",
        "needs_review": True,
        "needs_ordinal": True,
    }

def preview_local_paths(paths: list[str], enable_ai_vision: bool = False) -> dict[str, Any]:
    """
    Scan local folders / zip / cbz files and return structured preview tree.
    """
    books_result = []
    total_ai_vision_needed = 0

    for path_str in paths:
        p = Path(path_str).resolve()
        if not p.exists():
            continue

        ext = p.suffix.lower()
        if ext == ".cbr":
            raise ValueError(f"暂不支持 CBR 格式，请将 '{p.name}' 转换为 ZIP 或 CBZ 后再导入")

        # Case 1: Path is an individual zip or cbz archive
        if p.is_file() and ext in (".zip", ".cbz"):
            inspected = inspect_zip_archive(p)
            if inspected["page_count"] == 0:
                continue

            rec = recognize_chapter(p.name, inspected["meta"], fallback_ordinal=1, enable_ai_vision=enable_ai_vision)
            if rec["needs_review"]:
                total_ai_vision_needed += 1

            book_title = inspected["meta"].get("series") or os.path.splitext(p.name)[0]
            books_result.append({
                "temp_id": f"book-tmp-{len(books_result) + 1}",
                "title": book_title,
                "root_path": str(p.parent),
                "cover_preview_path": None,
                "total_chapters": 1,
                "needs_manual_review": rec["needs_review"],
                "chapters": [
                    {
                        "temp_id": "chap-tmp-1",
                        "title": rec["title"],
                        "ordinal": rec["ordinal"],
                        "source_type": "archive",
                        "source_path": str(p),
                        "page_count": inspected["page_count"],
                        "recognition_layer": rec["recognition_layer"],
                        "confidence": rec["confidence"],
                        "needs_review": rec["needs_review"],
                        "needs_ordinal": rec["needs_ordinal"],
                    }
                ],
            })
            continue

        # Case 2: Path is a folder
        if p.is_dir():
            # Check if this folder itself directly contains images
            direct_inspect = inspect_folder(p)
            
            # Check sub-items (subdirectories and zip/cbz files)
            sub_items = []
            for child in p.iterdir():
                if child.name.startswith(".") or child.name.startswith("__MACOSX"):
                    continue
                child_ext = child.suffix.lower()
                if child_ext == ".cbr":
                    raise ValueError(f"暂不支持 CBR 格式，请将 '{child.name}' 转换为 ZIP 或 CBZ 后再导入")
                if child.is_file() and child_ext in (".zip", ".cbz"):
                    sub_items.append(child)
                elif child.is_dir():
                    sub_items.append(child)

            sub_items.sort(key=lambda x: natural_sort_key(x.name))

            # If there are sub-items that look like chapters
            chapter_entries = []
            for item in sub_items:
                if item.is_file() and item.suffix.lower() in (".zip", ".cbz"):
                    ins = inspect_zip_archive(item)
                    if ins["page_count"] > 0:
                        chapter_entries.append((item, "archive", ins))
                elif item.is_dir():
                    ins = inspect_folder(item)
                    if ins["page_count"] > 0:
                        chapter_entries.append((item, "folder", ins))

            if chapter_entries:
                # This folder is a manga book containing multiple chapters!
                book_title = direct_inspect["meta"].get("series") or p.name
                book_chapters = []
                book_needs_review = False

                for idx, (cpath, ctype, cins) in enumerate(chapter_entries, 1):
                    rec = recognize_chapter(
                        cpath.name,
                        cins["meta"],
                        fallback_ordinal=idx,
                        enable_ai_vision=enable_ai_vision,
                    )
                    if rec["needs_review"]:
                        total_ai_vision_needed += 1
                        book_needs_review = True

                    book_chapters.append({
                        "temp_id": f"chap-tmp-{len(book_chapters) + 1}",
                        "title": rec["title"],
                        "ordinal": rec["ordinal"],
                        "source_type": ctype,
                        "source_path": str(cpath),
                        "page_count": cins["page_count"],
                        "recognition_layer": rec["recognition_layer"],
                        "confidence": rec["confidence"],
                        "needs_review": rec["needs_review"],
                        "needs_ordinal": rec["needs_ordinal"],
                    })

                # Sort chapters: valid ordinals first, None ordinals at the end
                book_chapters.sort(key=lambda x: (x["ordinal"] is None, x["ordinal"] if x["ordinal"] is not None else 999999))

                # Find a cover preview if possible
                cover_preview = None
                for c in chapter_entries:
                    if c[1] == "folder" and c[2].get("first_image"):
                        cover_preview = c[2]["first_image"]
                        break

                books_result.append({
                    "temp_id": f"book-tmp-{len(books_result) + 1}",
                    "title": book_title,
                    "root_path": str(p),
                    "cover_preview_path": cover_preview,
                    "total_chapters": len(book_chapters),
                    "needs_manual_review": book_needs_review,
                    "chapters": book_chapters,
                })
            elif direct_inspect["page_count"] > 0:
                # The folder itself is a single chapter
                rec = recognize_chapter(p.name, direct_inspect["meta"], fallback_ordinal=1, enable_ai_vision=enable_ai_vision)
                if rec["needs_review"]:
                    total_ai_vision_needed += 1

                books_result.append({
                    "temp_id": f"book-tmp-{len(books_result) + 1}",
                    "title": direct_inspect["meta"].get("series") or p.name,
                    "root_path": str(p.parent),
                    "cover_preview_path": direct_inspect.get("first_image"),
                    "total_chapters": 1,
                    "needs_manual_review": rec["needs_review"],
                    "chapters": [
                        {
                            "temp_id": "chap-tmp-1",
                            "title": rec["title"],
                            "ordinal": rec["ordinal"],
                            "source_type": "folder",
                            "source_path": str(p),
                            "page_count": direct_inspect["page_count"],
                            "recognition_layer": rec["recognition_layer"],
                            "confidence": rec["confidence"],
                            "needs_review": rec["needs_review"],
                            "needs_ordinal": rec["needs_ordinal"],
                        }
                    ],
                })

    return {
        "status": "ok",
        "ai_vision_needed_count": total_ai_vision_needed,
        "books": books_result,
    }
