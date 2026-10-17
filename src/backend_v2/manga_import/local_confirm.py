import os
import uuid
import json
import zipfile
from pathlib import Path
from typing import Any, Optional
from sqlalchemy import insert

from src.backend_v2.storage.schema import books, chapters, pages, page_assets
from src.backend_v2.content.image_import import ImageImportService
from src.backend_v2.content.page_style import resolve_new_page_style
from src.backend_v2.timestamps import utcnow
from src.backend_v2.auth.constants import LOCAL_USER_ID
from src.backend_v2.manga_import.local_preview import (
    is_valid_image_filename,
    natural_sort_key,
)

def import_single_local_book(
    book_data: dict[str, Any],
    *,
    data_root: Path,
    engine: object,
    importer: ImageImportService,
) -> dict[str, Any]:
    title = (book_data.get("title") or "").strip()
    if not title:
        raise ValueError("漫画标题不能为空")

    chapters_input = book_data.get("chapters") or []
    if not isinstance(chapters_input, list) or not chapters_input:
        raise ValueError(f"漫画 '{title}' 缺少有效的章节列表")

    # 1. Validate ordinals
    ordinals_seen = set()
    validated_chapters = []
    for idx, ch in enumerate(chapters_input, start=1):
        ch_title = (ch.get("title") or "").strip() or f"第{idx}话"
        raw_ord = ch.get("ordinal")
        if raw_ord is None:
            raise ValueError(f"漫画 '{title}' 中章节 '{ch_title}' 缺少序号 (ordinal)，请在前端手动指定顺序")
        
        try:
            ord_val = int(raw_ord)
        except (ValueError, TypeError):
            raise ValueError(f"漫画 '{title}' 中章节 '{ch_title}' 序号无效: {raw_ord}")

        if ord_val < 1:
            raise ValueError(f"漫画 '{title}' 中章节 '{ch_title}' 序号必须大于等于 1: {ord_val}")

        if ord_val in ordinals_seen:
            raise ValueError(f"漫画 '{title}' 中章节序号存在冲突 (ordinal={ord_val})，请调整章节顺序避免重复")
        ordinals_seen.add(ord_val)

        source_type = ch.get("source_type") or "folder"
        source_path = ch.get("source_path") or ""
        p = Path(source_path).resolve()
        if not p.exists():
            raise ValueError(f"章节文件路径不存在: {source_path}")

        validated_chapters.append({
            "title": ch_title,
            "ordinal": ord_val,
            "source_type": source_type,
            "path": p,
        })

    # Sort chapters ascending by ordinal
    validated_chapters.sort(key=lambda x: x["ordinal"])

    # 2. Publish Cover Asset if available
    cover_asset_id = None
    cover_source_path = book_data.get("cover_source_path")
    if cover_source_path and Path(cover_source_path).exists():
        try:
            with open(cover_source_path, "rb") as cf:
                cover_asset = importer.publish_cover(cf)
                cover_asset_id = cover_asset.id
        except Exception:
            pass

    # Fallback cover: use the first image of the first chapter
    if not cover_asset_id and validated_chapters:
        first_ch = validated_chapters[0]
        try:
            if first_ch["source_type"] == "folder":
                folder_imgs = [
                    f for f in first_ch["path"].iterdir()
                    if f.is_file() and is_valid_image_filename(f.name)
                ]
                folder_imgs.sort(key=lambda x: natural_sort_key(x.name))
                if folder_imgs:
                    with folder_imgs[0].open("rb") as cf:
                        cover_asset = importer.publish_cover(cf)
                        cover_asset_id = cover_asset.id
            elif first_ch["source_type"] == "archive":
                with zipfile.ZipFile(first_ch["path"], "r") as zf:
                    archive_imgs = [
                        name for name in zf.namelist()
                        if is_valid_image_filename(name)
                    ]
                    archive_imgs.sort(key=natural_sort_key)
                    if archive_imgs:
                        with zf.open(archive_imgs[0]) as cf:
                            cover_asset = importer.publish_cover(cf)
                            cover_asset_id = cover_asset.id
        except Exception:
            pass

    # 3. Create Book Record
    book_id = str(uuid.uuid4())
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(books).values(
                id=book_id,
                owner_user_id=LOCAL_USER_ID,
                kind="library",
                title=title,
                chapter_order_revision=1,
                cover_asset_id=cover_asset_id,
                created_at=now,
                updated_at=now,
            )
        )

    # 4. Resolve default page style
    with engine.connect() as conn:
        default_font_id, style_defaults = resolve_new_page_style(conn)

    total_chapters = 0
    total_pages = 0

    # 5. Process Chapters and Pages
    for ch_info in validated_chapters:
        chapter_id = str(uuid.uuid4())
        ch_now = utcnow()
        with engine.begin() as conn:
            conn.execute(
                insert(chapters).values(
                    id=chapter_id,
                    book_id=book_id,
                    ordinal=ch_info["ordinal"],
                    title=ch_info["title"],
                    page_order_revision=1,
                    settings_memory_json="{}",
                    settings_memory_revision=1,
                    created_at=ch_now,
                    updated_at=ch_now,
                )
            )

        if ch_info["source_type"] == "folder":
            img_files = [
                f for f in ch_info["path"].iterdir()
                if f.is_file() and is_valid_image_filename(f.name)
            ]
            img_files.sort(key=lambda x: natural_sort_key(x.name))

            batch_size = 50
            for b_start in range(0, len(img_files), batch_size):
                chunk = img_files[b_start : b_start + batch_size]
                
                # Step A: Publish image assets outside open DB transaction
                published_chunk: list[tuple[int, str, Any, Any]] = []
                for p_offset, p_file in enumerate(chunk):
                    p_ordinal = b_start + p_offset + 1
                    with p_file.open("rb") as pf:
                        source_asset, thumb_asset = importer.publish_standalone_image(pf)
                    published_chunk.append((p_ordinal, p_file.name, source_asset, thumb_asset))

                # Step B: Insert page records in dedicated transaction
                with engine.begin() as conn:
                    for p_ordinal, fname, source_asset, thumb_asset in published_chunk:
                        page_id = str(uuid.uuid4())
                        logical_path = f"{chapter_id}/{fname}"
                        p_now = utcnow()

                        conn.execute(
                            insert(pages).values(
                                id=page_id,
                                chapter_id=chapter_id,
                                ordinal=p_ordinal,
                                logical_source_path=logical_path,
                                source_revision=1,
                                document_revision=1,
                                render_status="not_rendered",
                                detection_state="unprocessed",
                                default_font_id=default_font_id,
                                page_style_defaults_json=json.dumps(style_defaults),
                                warnings_json="[]",
                                created_at=p_now,
                                updated_at=p_now,
                            )
                        )

                        conn.execute(
                            insert(page_assets),
                            [
                                {
                                    "page_id": page_id,
                                    "role": "source",
                                    "asset_id": source_asset.id,
                                    "input_source_revision": 1,
                                    "parent_asset_id": None,
                                },
                                {
                                    "page_id": page_id,
                                    "role": "thumbnail_source",
                                    "asset_id": thumb_asset.id,
                                    "input_source_revision": 1,
                                    "parent_asset_id": source_asset.id,
                                },
                            ],
                        )
                        total_pages += 1

        elif ch_info["source_type"] == "archive":
            with zipfile.ZipFile(ch_info["path"], "r") as zf:
                archive_imgs = [
                    name for name in zf.namelist()
                    if is_valid_image_filename(name)
                ]
                archive_imgs.sort(key=natural_sort_key)

                batch_size = 50
                for b_start in range(0, len(archive_imgs), batch_size):
                    chunk = archive_imgs[b_start : b_start + batch_size]
                    
                    published_chunk = []
                    for p_offset, img_name in enumerate(chunk):
                        p_ordinal = b_start + p_offset + 1
                        with zf.open(img_name) as pf:
                            source_asset, thumb_asset = importer.publish_standalone_image(pf)
                        published_chunk.append((p_ordinal, os.path.basename(img_name), source_asset, thumb_asset))

                    with engine.begin() as conn:
                        for p_ordinal, fname, source_asset, thumb_asset in published_chunk:
                            page_id = str(uuid.uuid4())
                            logical_path = f"{chapter_id}/{fname}"
                            p_now = utcnow()

                            conn.execute(
                                insert(pages).values(
                                    id=page_id,
                                    chapter_id=chapter_id,
                                    ordinal=p_ordinal,
                                    logical_source_path=logical_path,
                                    source_revision=1,
                                    document_revision=1,
                                    render_status="not_rendered",
                                    detection_state="unprocessed",
                                    default_font_id=default_font_id,
                                    page_style_defaults_json=json.dumps(style_defaults),
                                    warnings_json="[]",
                                    created_at=p_now,
                                    updated_at=p_now,
                                )
                            )

                            conn.execute(
                                insert(page_assets),
                                [
                                    {
                                        "page_id": page_id,
                                        "role": "source",
                                        "asset_id": source_asset.id,
                                        "input_source_revision": 1,
                                        "parent_asset_id": None,
                                    },
                                    {
                                        "page_id": page_id,
                                        "role": "thumbnail_source",
                                        "asset_id": thumb_asset.id,
                                        "input_source_revision": 1,
                                        "parent_asset_id": source_asset.id,
                                    },
                                ],
                            )
                            total_pages += 1

        total_chapters += 1

    return {
        "book_id": book_id,
        "title": title,
        "chapters_count": total_chapters,
        "pages_count": total_pages,
        "status": "success",
    }
