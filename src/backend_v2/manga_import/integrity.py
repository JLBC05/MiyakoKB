"""导入完整性核对：磁盘上存在的章节是否全部进了数据库。

设计口径（与产品语义对齐，不要"顺手"改）：

* **以文件系统为准**。磁盘（Venera 章节目录 / 压缩包内条目 / 本地子目录）上实际
  存在的内容，是否都能在库里找到对应记录。
* **Venera 库里登记但磁盘没有的章节标记为「未下载」，不算丢件**。Venera 的
  ``downloadedChapters`` 会保留已被用户删掉文件的记录，把它算成导入失败会
  永远红着。
* **索引是 ordinal**。导入器按枚举顺序写 ``ordinal=1..N``（Venera 按
  ``downloaded_chapters`` 顺序，本地按 preview 的章序），所以「磁盘第 k 项」
  对「库里 ordinal=k」。这也是唯一可靠的对应关系：Venera 的章节目录名是
  UUID，和标题无关。

只读：本模块不写数据库、不改文件，也不发起任何网络请求。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import Engine, select

from src.backend_v2.storage.schema import chapters, pages

# 与 ``image_import`` / ``venera_importer`` 保持一致的图片后缀集合。
_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif", ".avif"}
)
_ARCHIVE_SUFFIXES = frozenset({".zip", ".cbz"})


@dataclass(slots=True)
class ChapterProbe:
    """磁盘上的一个章节（候选）。"""

    ordinal: int
    label: str
    source: str
    #: 该章节里可识别的图片数量；压缩包读失败时为 ``None``。
    images: int | None = None
    #: 读盘失败的原因（存在但读不了时非空）。
    problem: str | None = None
    #: 页码（去掉扩展名的主名，例如 ``001``）集合 —— 用来与库里的
    #: ``logical_source_path`` 逐页对账，比拿 ordinal 猜可靠得多。
    image_stems: frozenset[str] = frozenset()
    #: Venera 章节目录名（UUID）——库里的页路径前缀就是它。
    source_dir_name: str | None = None


@dataclass(slots=True)
class ChapterVerdict:
    ordinal: int
    label: str
    images: int | None
    status: str
    detail: str = ""


@dataclass(slots=True)
class ImportIntegrityReport:
    source: str
    target: str
    book_id: str | None
    filesystem_chapters: int
    imported_chapters: int
    #: 磁盘有、库里没有（或没有页）的章节 —— 这是"丢件"的定义。
    lost: list[ChapterVerdict] = field(default_factory=list)
    #: 磁盘有、库里也有，但页数对不上的章节 —— 单独一档，不算丢件但值得看。
    incomplete: list[ChapterVerdict] = field(default_factory=list)
    #: 只出现在库里、磁盘上找不到的章节。Venera 场景通常是"没下载"。
    not_downloaded: list[ChapterVerdict] = field(default_factory=list)
    #: 磁盘上读不了/不认识的条目。
    problems: list[ChapterVerdict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有丢件、没有读盘错误就是通过。``incomplete`` 只作提示。"""

        return not self.lost and not self.problems

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "bookId": self.book_id,
            "filesystemChapters": self.filesystem_chapters,
            "importedChapters": self.imported_chapters,
            "lost": [_verdict_payload(item) for item in self.lost],
            "incomplete": [_verdict_payload(item) for item in self.incomplete],
            "notDownloaded": [_verdict_payload(item) for item in self.not_downloaded],
            "problems": [_verdict_payload(item) for item in self.problems],
            "notes": list(self.notes),
            "ok": self.ok,
        }


def _verdict_payload(item: ChapterVerdict) -> dict[str, Any]:
    return {
        "ordinal": item.ordinal,
        "label": item.label,
        "images": item.images,
        "status": item.status,
        "detail": item.detail,
    }


def _dir_image_stems(path: Path) -> frozenset[str]:
    return frozenset(
        item.stem
        for item in path.iterdir()
        if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
    )


def _zip_image_stems(archive: Path) -> frozenset[str]:
    with zipfile.ZipFile(archive, "r") as handle:
        return frozenset(
            Path(name).stem
            for name in handle.namelist()
            if not name.endswith("/")
            and Path(name).suffix.lower() in _IMAGE_SUFFIXES
        )


def _count_images_in_dir(path: Path) -> int:
    return sum(
        1
        for item in path.iterdir()
        if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
    )


def _zip_image_entries(archive: Path) -> int:
    with zipfile.ZipFile(archive, "r") as handle:
        return sum(
            1
            for name in handle.namelist()
            if not name.endswith("/")
            and Path(name).suffix.lower() in _IMAGE_SUFFIXES
        )


def _natural_key(name: str) -> tuple:
    """按十进制数值排序，``2`` 排在 ``10`` 前面。

    导入器与 Venera 都用「词法 + 数值」的自然序（页文件 ``0.jpg 1.jpg 2.jpg``），
    纯字符串排序会把 ``10`` 排到 ``2`` 之前，序位就整体错开一位。
    """

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", name)
        if part
    )


def probe_local_paths(paths: Iterable[Path]) -> list[ChapterProbe]:
    """把用户给的路径展开成章节序列（顺序即导入顺序）。

    规则与 ``local_preview.preview_local_paths`` 保持一致：
    单个 ``.zip``/``.cbz`` 算一个章节；一个目录若其子项里有图片就整体算一个章节、
    否则把子目录与压缩包各算一个章节。
    """

    probes: list[ChapterProbe] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            probes.append(
                ChapterProbe(
                    ordinal=len(probes) + 1,
                    label=str(path),
                    source="local",
                    problem="路径不存在",
                )
            )
            continue
        if path.is_file() and path.suffix.lower() in _ARCHIVE_SUFFIXES:
            try:
                probes.append(
                    ChapterProbe(
                        ordinal=len(probes) + 1,
                        label=path.name,
                        source="archive",
                        images=_zip_image_entries(path),
                        image_stems=_zip_image_stems(path),
                    )
                )
            except (OSError, zipfile.BadZipFile) as error:
                probes.append(
                    ChapterProbe(
                        ordinal=len(probes) + 1,
                        label=path.name,
                        source="archive",
                        problem=f"压缩包无法读取：{error}",
                    )
                )
            continue

        child_dirs = sorted(
            (item for item in path.iterdir() if item.is_dir()),
            key=lambda item: _natural_key(item.name),
        )
        child_archives = sorted(
            (
                item
                for item in path.iterdir()
                if item.is_file() and item.suffix.lower() in _ARCHIVE_SUFFIXES
            ),
            key=lambda item: _natural_key(item.name),
        )
        direct_images = sum(
            1
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
        )
        if direct_images and not child_dirs and not child_archives:
            probes.append(
                ChapterProbe(
                    ordinal=len(probes) + 1,
                    label=path.name,
                    source="folder",
                    images=direct_images,
                    image_stems=_dir_image_stems(path),
                )
            )
            continue
        for child in [*child_dirs, *child_archives]:
            if child.is_dir():
                try:
                    probes.append(
                        ChapterProbe(
                            ordinal=len(probes) + 1,
                            label=child.name,
                            source="folder",
                            images=_count_images_in_dir(child),
                            image_stems=_dir_image_stems(child),
                        )
                    )
                except OSError as error:
                    probes.append(
                        ChapterProbe(
                            ordinal=len(probes) + 1,
                            label=child.name,
                            source="folder",
                            problem=f"目录无法读取：{error}",
                        )
                    )
            else:
                try:
                    probes.append(
                        ChapterProbe(
                            ordinal=len(probes) + 1,
                            label=child.name,
                            source="archive",
                            images=_zip_image_entries(child),
                            image_stems=_zip_image_stems(child),
                        )
                    )
                except (OSError, zipfile.BadZipFile) as error:
                    probes.append(
                        ChapterProbe(
                            ordinal=len(probes) + 1,
                            label=child.name,
                            source="archive",
                            problem=f"压缩包无法读取：{error}",
                        )
                    )
    return probes


def probe_venera_comic(base_dir: Path, comic_id: str) -> tuple[list[ChapterProbe], list[str]]:
    """按 Venera 的「已下载列表」展开磁盘章节。

    返回 ``(磁盘上真实存在的章节, 备注)``；只登记在 Venera、磁盘没有的章节
    不放进 probes —— 它们会在 ``check`` 里落到 ``not_downloaded``。
    """

    from src.backend_v2.manga_import.routes import _get_venera_comic_detail

    comic = _get_venera_comic_detail(base_dir, comic_id)
    if comic is None:
        raise FileNotFoundError(f"Venera 本地库里没有这本书：{comic_id}")
    directory = comic.get("directory")
    if not directory:
        raise FileNotFoundError(f"Venera 记录里没有目录名：{comic_id}")
    comic_dir = base_dir / "local" / str(directory)
    if not comic_dir.is_dir():
        raise FileNotFoundError(f"Venera 漫画目录不存在：{comic_dir}")

    notes: list[str] = []
    probes: list[ChapterProbe] = []
    downloaded: list[Any] = list(comic.get("downloaded_chapters") or [])
    titles: dict[str, str] = comic.get("chapter_title_map") or {}
    for index, chapter_id in enumerate(downloaded, start=1):
        chapter_dir = comic_dir / str(chapter_id)
        label = titles.get(str(chapter_id)) or f"第{index}话"
        if not chapter_dir.is_dir():
            notes.append(
                f"第{index}话（{label}）在 Venera 里登记为已下载，但磁盘没有该目录"
            )
            continue
        try:
            probes.append(
                ChapterProbe(
                    ordinal=index,
                    label=label,
                    source="venera",
                    images=_count_images_in_dir(chapter_dir),
                    image_stems=_dir_image_stems(chapter_dir),
                    source_dir_name=str(chapter_id),
                )
            )
        except OSError as error:
            probes.append(
                ChapterProbe(
                    ordinal=index,
                    label=label,
                    source="venera",
                    problem=f"章节目录无法读取：{error}",
                )
            )
    return probes, notes


def _imported_page_stems(engine: Engine, book_id: str) -> dict[int, set[str]]:
    """``ordinal -> 该章已入库页的文件主名集合``。

    页的 ``logical_source_path`` 形如 ``m1837907/0.jpg`` 或 ``001.png``，
    取 basename 的主名与磁盘文件对账。这样才能区分「导入丢了页」和
    「磁盘和库指向不同目录」——只比数量会把两种情况混成一样。
    """

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                chapters.c.ordinal,
                pages.c.logical_source_path,
            )
            .join(pages, pages.c.chapter_id == chapters.c.id, isouter=True)
            .where(chapters.c.book_id == book_id)
            .order_by(chapters.c.ordinal)
        ).all()
        stems: dict[int, set[str]] = {}
        for ordinal, logical_path in rows:
            bucket = stems.setdefault(int(ordinal), set())
            if logical_path:
                bucket.add(Path(str(logical_path)).stem)
    return stems


def _imported_page_counts(engine: Engine, book_id: str) -> dict[int, int]:
    """``ordinal -> 该章已入库页数``。"""

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                chapters.c.ordinal,
                chapters.c.title,
                chapters.c.id,
            )
            .where(chapters.c.book_id == book_id)
            .order_by(chapters.c.ordinal)
        ).all()
        counts: dict[int, int] = {}
        for ordinal, _title, chapter_id in rows:
            total = connection.execute(
                select(pages.c.id).where(pages.c.chapter_id == chapter_id)
            ).all()
            counts[int(ordinal)] = len(total)
    return counts


def check_import_integrity(
    *,
    engine: Engine,
    book_id: str,
    probes: list[ChapterProbe],
    source: str,
    target: str,
    notes: Iterable[str] = (),
    unmapped_ordinals: Iterable[int] = (),
) -> ImportIntegrityReport:
    """把磁盘章节序列与库里的章节/页数逐项对照。

    ``unmapped_ordinals`` 是「库里有、但磁盘上没有」的 ordinal（Venera 的未下载
    章节就是这种），单独归到 ``not_downloaded``，不计入 ``lost``。
    """

    page_counts = _imported_page_counts(engine, book_id)
    page_stems = _imported_page_stems(engine, book_id)
    skipped = set(int(value) for value in unmapped_ordinals)

    report = ImportIntegrityReport(
        source=source,
        target=target,
        book_id=book_id,
        filesystem_chapters=sum(1 for item in probes if item.problem is None),
        imported_chapters=len(page_counts),
        notes=list(notes),
    )

    for probe in probes:
        if probe.problem is not None:
            report.problems.append(
                ChapterVerdict(
                    ordinal=probe.ordinal,
                    label=probe.label,
                    images=None,
                    status="unreadable",
                    detail=probe.problem,
                )
            )
            continue
        imported = page_counts.get(probe.ordinal)
        if imported is None:
            report.lost.append(
                ChapterVerdict(
                    ordinal=probe.ordinal,
                    label=probe.label,
                    images=probe.images,
                    status="missing_in_database",
                    detail="磁盘有这一章，但库里没有该 ordinal 的章节记录",
                )
            )
            continue
        if imported == 0:
            report.lost.append(
                ChapterVerdict(
                    ordinal=probe.ordinal,
                    label=probe.label,
                    images=probe.images,
                    status="empty_in_database",
                    detail="章节记录存在但一页都没有",
                )
            )
            continue
        # 逐页对账：磁盘上哪些文件在库里找不到。
        if probe.image_stems:
            imported_stems = page_stems.get(probe.ordinal, set())
            missing = sorted(probe.image_stems - imported_stems)
            if not imported_stems:
                # 库里这一章没有任何可解析的页路径，数量比较是兜底。
                if probe.images is not None and imported < probe.images:
                    report.incomplete.append(
                        ChapterVerdict(
                            ordinal=probe.ordinal,
                            label=probe.label,
                            images=probe.images,
                            status="fewer_pages_than_disk",
                            detail=f"磁盘 {probe.images} 张图，库里 {imported} 页",
                        )
                    )
                continue
            if not missing:
                continue
            if len(missing) == len(probe.image_stems):
                report.incomplete.append(
                    ChapterVerdict(
                        ordinal=probe.ordinal,
                        label=probe.label,
                        images=probe.images,
                        status="pages_from_elsewhere",
                        detail=(
                            f"磁盘 {probe.images} 张图与库里 {imported} 页没有一张文件名相同；"
                            "库里这一章的页面来自别的目录（序位与磁盘不一致），"
                            "请按本章标题核对，不要按序位判断"
                        ),
                    )
                )
                continue
            head = "、".join(missing[:5])
            more = f" 等 {len(missing)} 张" if len(missing) > 5 else ""
            report.incomplete.append(
                ChapterVerdict(
                    ordinal=probe.ordinal,
                    label=probe.label,
                    images=probe.images,
                    status="missing_pages",
                    detail=(
                        f"磁盘 {probe.images} 张图，库里 {imported} 页；"
                        f"库里找不到这些页：{head}{more}"
                    ),
                )
            )
            continue
        # 没有逐页信息（例如压缩包读失败之外的老路径）时退回数量比较。
        if probe.images is not None and imported < probe.images:
            report.incomplete.append(
                ChapterVerdict(
                    ordinal=probe.ordinal,
                    label=probe.label,
                    images=probe.images,
                    status="fewer_pages_than_disk",
                    detail=f"磁盘 {probe.images} 张图，库里 {imported} 页",
                )
            )

    probed_ordinals = {item.ordinal for item in probes}
    # 「未下载」要同时覆盖两种情形：
    #   ① 库里根本没有这一章（从未导入）—— 只看 page_counts 会漏掉；
    #   ② 库里有这一章，但磁盘上没有对应目录。
    # 两种情况都不是丢件，必须单独归到 not_downloaded。
    for ordinal in sorted(skipped | set(page_counts)):
        if ordinal in probed_ordinals:
            continue
        in_database = ordinal in page_counts
        report.not_downloaded.append(
            ChapterVerdict(
                ordinal=ordinal,
                label=f"第{ordinal}话",
                images=None,
                status="not_downloaded",
                detail=(
                    "Venera 登记为已下载但磁盘没有该目录，不计入丢件"
                    if in_database
                    else "Venera 登记为已下载但磁盘没有该目录（也从未导入），不计入丢件"
                ),
            )
        )
    return report
