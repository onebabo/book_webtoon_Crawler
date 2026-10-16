"""마이 북: 현재 PC 에 보관 중인 웹툰 폴더/이미지를 분석해서 DB(my_toon_*) 로 등록합니다.

0) 원본 폴더의 {회차}.zip 은 같은 이름 폴더가 없을 때만 풀어서 폴더를 만듭니다 (있으면 zip 무시)
1) --TYPE 에 맞춰 원본 폴더 구조를 분석 (회차 폴더 / 이미지 순서) → 분석 결과 출력
2) my_toon_info 등록 (--bookid 가 없으면 현재 일시 YYYYMMDDHHMMSS 로 신규 아이디 생성)
3) 회차별로 이미지를 <기본경로>/{my_toon_id}/{episode_no}/001.jpg ... 로 복사
   → my_toon_episode / my_toon_episode_image 갱신 (회차 단위 커밋)

   TYPE 2 는 가로로 긴 이미지(한 장에 2페이지 펼침면)를 좌/우 반으로 잘라 1페이지씩 저장합니다.

이미 복사된 파일은 건너뛰므로, 중간에 끊겨도 같은 --bookid 로 다시 실행하면 이어서 진행합니다.
폴더 구조 TYPE 별 규칙은 01_Claude_job/MYBOOK_CREATE.md 에 기록합니다.

  python toon_create.py --TYPE 1 --name "도시정벌 1부" --path "D:\\mynas\\도시정벌 1부(01~12)"
  python toon_create.py --TYPE 1 --name "도시정벌 1부" --path "D:\\mynas\\도시정벌 1부(01~12)" --dry-run
  python toon_create.py --TYPE 2 --name "도시정벌 5부" --path "D:\\mynas\\도시정벌 5부"
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from db import config_path, episode_type_from_text, get_connection, load_mysql_config, schema_statements

DEFAULT_BASE_PATH = Path("D:/booktoon")
MY_SCHEMA_SQL = Path(__file__).resolve().parent / "book_sql" / "30_my_toon_테이블생성.sql"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
SPLIT_TYPES = {2}  # 펼침면(가로 이미지)을 좌/우 2페이지로 나누는 TYPE
SPLIT_JPEG_QUALITY = 92


# ---------------------------------------------------------------------
# 폴더 구조 분석
# ---------------------------------------------------------------------
def natural_key(name: str) -> list:
    """'2.jpg' < '10.jpg' 처럼 숫자를 숫자로 비교하는 정렬 키."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def list_images(folder: Path) -> list[Path]:
    """폴더 바로 아래의 이미지 파일(Thumbs.db 등 제외)을 파일명 숫자 순서로 반환합니다."""
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: natural_key(p.name))


def episode_no_from_name(name: str) -> int | None:
    """폴더명의 마지막 숫자를 회차 번호로 봅니다. ('도시정벌 2부 01' → 1)"""
    found = re.findall(r"\d+", name)
    return int(found[-1]) if found else None


def number_gaps(images: list[Path]) -> list[int]:
    """원본 파일명이 숫자일 때 중간에 빠진 번호 목록 (참고용 경고)."""
    numbers = sorted(int(p.stem) for p in images if p.stem.isdigit())
    if not numbers:
        return []
    have = set(numbers)
    return [n for n in range(numbers[0], numbers[-1] + 1) if n not in have]


def analyze_type1(root: Path) -> tuple[list[dict], list[str]]:
    """TYPE 1 : {path}\\{회차폴더}\\{이미지}  (회차폴더명 끝 숫자 = 회차 번호, 이미지 1단계)."""
    episodes: list[dict] = []
    warnings: list[str] = []

    loose = list_images(root)
    if loose:
        warnings.append(f"루트에 회차 폴더 밖 이미지 {len(loose)}개가 있어 무시합니다 (예: {loose[0].name})")

    seen: dict[int, str] = {}
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
        no = episode_no_from_name(folder.name)
        if no is None:
            raise ValueError(f"회차 번호를 찾을 수 없는 폴더입니다: {folder.name} (TYPE 1 아님 → 새 TYPE 필요)")
        if no in seen:
            raise ValueError(f"회차 번호 {no} 가 중복됩니다: '{seen[no]}' / '{folder.name}'")
        seen[no] = folder.name

        subdirs = [p.name for p in folder.iterdir() if p.is_dir()]
        if subdirs:
            raise ValueError(f"회차 폴더 안에 하위 폴더가 있습니다: {folder.name}\\{subdirs[0]} (TYPE 1 아님 → 새 TYPE 필요)")

        images = list_images(folder)
        if not images:
            warnings.append(f"이미지가 없는 회차 폴더를 건너뜁니다: {folder.name}")
            continue
        gaps = number_gaps(images)
        if gaps:
            warnings.append(f"{folder.name}: 원본 파일 번호 빠짐 {gaps[:10]}{' ...' if len(gaps) > 10 else ''}")
        episodes.append({"episode_no": no, "title": folder.name, "src_dir": folder, "images": images})

    if not episodes:
        raise ValueError(f"회차 폴더(이미지 포함)를 찾지 못했습니다: {root}")

    episodes.sort(key=lambda ep: ep["episode_no"])
    numbers = [ep["episode_no"] for ep in episodes]
    missing = [n for n in range(numbers[0], numbers[-1] + 1) if n not in set(numbers)]
    if missing:
        warnings.append(f"빠진 회차 번호: {missing}")
    return episodes, warnings


ANALYZERS = {
    1: analyze_type1,
    2: analyze_type1,  # 폴더 구조는 TYPE 1 과 같고, 펼침면 분리만 다름 (SPLIT_TYPES)
}
SUPPORTED_TYPES = tuple(ANALYZERS)


# ---------------------------------------------------------------------
# 페이지 구성 (TYPE 2 펼침면 분리)
# ---------------------------------------------------------------------
def jpeg_size(path: Path) -> tuple[int, int] | None:
    """JPEG 헤더(SOF)만 읽어 (가로, 세로) 를 돌려줍니다. 읽을 수 없으면 None."""
    with path.open("rb") as f:
        if f.read(2) != b"\xff\xd8":
            return None
        while True:
            byte = f.read(1)
            while byte and byte != b"\xff":
                byte = f.read(1)
            while byte == b"\xff":
                byte = f.read(1)
            if not byte:
                return None
            marker = byte[0]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            length = struct.unpack(">H", f.read(2))[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">xHH", f.read(5))
                return width, height
            f.seek(length - 2, 1)


FORMAT_EXTS = {"jpeg": ".jpg", "png": ".png", "gif": ".gif"}


def image_format(path: Path) -> str | None:
    """확장자가 아니라 파일 앞부분으로 형식을 판단합니다 (예: 8부 54권은 PNG 인데 이름이 .jpg)."""
    with path.open("rb") as f:
        head = f.read(8)
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"GIF":
        return "gif"
    return None


def image_size(path: Path) -> tuple[int, int] | None:
    """JPEG / GIF / PNG 의 (가로, 세로) 를 헤더만 읽어 구합니다. 그 외 형식이나 읽기 실패는 None."""
    fmt = image_format(path)
    if fmt == "jpeg":
        return jpeg_size(path)
    with path.open("rb") as f:
        head = f.read(24)
    if fmt == "gif":
        return struct.unpack("<HH", head[6:10])
    if fmt == "png":
        return struct.unpack(">II", head[16:24])
    return None


def plan_pages(episodes: list[dict], split: bool) -> None:
    """ep['pages'] = [(원본 이미지, 'L'|'R'|None), ...] 를 채웁니다.

    split 이면 가로가 세로보다 긴 이미지(JPEG/GIF/PNG)를 펼침면으로 보고 왼쪽 → 오른쪽 순서로 2페이지로 나눕니다.
    세로 이미지(표지 등 단면)는 그대로 1페이지입니다.
    """
    for ep in episodes:
        pages: list[tuple[Path, str | None]] = []
        spreads = 0
        for src in ep["images"]:
            size = image_size(src) if split else None
            if size and size[0] > size[1]:
                pages += [(src, "L"), (src, "R")]
                spreads += 1
            else:
                pages.append((src, None))
        ep["pages"] = pages
        ep["spreads"] = spreads


# ---------------------------------------------------------------------
# 압축 파일 (루트의 {회차}.zip)
# ---------------------------------------------------------------------
def zip_member_name(info: zipfile.ZipInfo) -> str:
    """UTF-8 플래그가 없는 zip 은 zipfile 이 cp437 로 읽으므로 cp949(한글 Windows) 로 다시 해석합니다."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def extract_zip_folder(zip_path: Path, target: Path) -> int:
    """zip 안의 이미지를 target 폴더 바로 아래에 풉니다(zip 내부 폴더 경로는 버림). 푼 이미지 수 반환.

    임시 폴더에 다 푼 뒤 이름을 바꾸므로, 중간에 끊겨도 반쯤 풀린 회차 폴더가 남지 않습니다.
    """
    temp = target.with_name(target.name + ".unzip_tmp")
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(zip_member_name(info).replace("\\", "/")).name  # 경로는 버리고 파일명만 (zip slip 방지)
            if Path(name).suffix.lower() not in IMAGE_EXTS:
                continue
            dest = temp / name
            if dest.exists():
                shutil.rmtree(temp)
                raise ValueError(f"{zip_path.name}: 같은 이름의 이미지가 여러 개 있습니다 ({name})")
            with zf.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    temp.rename(target)
    return count


def prepare_zip_folders(root: Path, dry_run: bool) -> None:
    """루트의 *.zip 마다: 같은 이름 폴더가 있으면 무시, 없으면 풀어서 폴더를 만듭니다."""
    zips = sorted(root.glob("*.zip"), key=lambda p: natural_key(p.name))
    if not zips:
        return
    todo = [z for z in zips if not (root / z.stem).is_dir()]
    print(f"[압축] zip {len(zips)}개 중 폴더 있음(무시) {len(zips) - len(todo)}개, 해제 대상 {len(todo)}개")
    for zip_path in todo:
        if dry_run:
            print(f"  - (dry-run) 해제 예정: {zip_path.name} → {zip_path.stem}\\")
            continue
        count = extract_zip_folder(zip_path, root / zip_path.stem)
        print(f"  - 해제: {zip_path.name} → {zip_path.stem}\\ ({count}장)")


def print_analysis(root: Path, src_type: int, episodes: list[dict], warnings: list[str]) -> None:
    total = sum(len(ep["images"]) for ep in episodes)
    pages = sum(len(ep["pages"]) for ep in episodes)
    spreads = sum(ep["spreads"] for ep in episodes)
    print(f"[분석] TYPE {src_type} : {root}")
    print(f"  회차 {len(episodes)}개 ({episodes[0]['episode_no']}~{episodes[-1]['episode_no']}), 이미지 {total}장", end="")
    print(f" → 펼침면 {spreads}장 분리, {pages}페이지" if src_type in SPLIT_TYPES else "")
    for ep in episodes:
        imgs = ep["images"]
        split_note = f" → {len(ep['pages'])}페이지 (펼침면 {ep['spreads']})" if src_type in SPLIT_TYPES else ""
        print(f"  - {ep['episode_no']:>4}회 | {ep['title']} | {len(imgs)}장 ({imgs[0].name} ~ {imgs[-1].name}){split_note}")
    for msg in warnings:
        print(f"  ! {msg}")


# ---------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------
def ensure_my_schema(conn) -> None:
    with conn.cursor() as cursor:
        for stmt in schema_statements(MY_SCHEMA_SQL):
            cursor.execute(stmt)


def my_toon_exists(cursor, my_toon_id: int) -> bool:
    cursor.execute("SELECT 1 FROM my_toon_info WHERE my_toon_id = %s", (my_toon_id,))
    return cursor.fetchone() is not None


def my_toon_src_type(cursor, my_toon_id: int) -> int | None:
    """등록된 마이 북의 TYPE (없으면 None)."""
    cursor.execute("SELECT src_type FROM my_toon_info WHERE my_toon_id = %s", (my_toon_id,))
    row = cursor.fetchone()
    return int(row[0]) if row else None


def clear_episode_dirs(toon_dir: Path) -> int:
    """toon_dir 아래 숫자 이름 회차 폴더(이 프로그램이 만든 복사본)만 지웁니다."""
    if not toon_dir.is_dir():
        return 0
    targets = [p for p in toon_dir.iterdir() if p.is_dir() and p.name.isdigit()]
    for folder in targets:
        shutil.rmtree(folder)
    return len(targets)


def new_my_toon_id(cursor) -> int:
    """현재 일시 YYYYMMDDHHMMSS (14자리, BIGINT). 같은 초에 이미 있으면 1초 뒤 값으로 다시 만듭니다."""
    while True:
        my_toon_id = int(datetime.now().strftime("%Y%m%d%H%M%S"))
        if not my_toon_exists(cursor, my_toon_id):
            return my_toon_id
        time.sleep(1)


def upsert_my_toon_info(cursor, info: dict) -> None:
    cursor.execute(
        """
        INSERT INTO my_toon_info (my_toon_id, title, total_episode, src_type, src_path, image_root)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title),
            total_episode = VALUES(total_episode),
            src_type = VALUES(src_type),
            src_path = VALUES(src_path),
            image_root = VALUES(image_root)
        """,
        (
            info["my_toon_id"],
            info["title"],
            info["total_episode"],
            info["src_type"],
            info["src_path"],
            info["image_root"],
        ),
    )


def save_my_episode(cursor, my_toon_id: int, ep: dict, image_dir: str, images: list[dict]) -> None:
    """회차 1건 반영 (해당 회차 이미지 행은 새로 교체)."""
    episode_code = f"my-{my_toon_id}-{ep['episode_no']}"
    cursor.execute(
        """
        INSERT INTO my_toon_episode (
            my_toon_id, episode_code, episode_no, episode_type, episode_title,
            src_dir, image_dir, image_cnt, saved_image_cnt, collected_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            episode_type = VALUES(episode_type),
            episode_title = VALUES(episode_title),
            src_dir = VALUES(src_dir),
            image_dir = VALUES(image_dir),
            image_cnt = VALUES(image_cnt),
            saved_image_cnt = VALUES(saved_image_cnt),
            collected_at = NOW()
        """,
        (
            my_toon_id,
            episode_code,
            ep["episode_no"],
            episode_type_from_text(ep["title"]),
            ep["title"],
            ep["src_dir"].as_posix(),
            image_dir,
            len(ep["pages"]),
            len(images),
        ),
    )
    cursor.execute("DELETE FROM my_toon_episode_image WHERE episode_code = %s", (episode_code,))
    if images:
        cursor.executemany(
            """
            INSERT INTO my_toon_episode_image (my_toon_id, episode_code, page_no, src_file, file_path, file_size)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (my_toon_id, episode_code, img["page_no"], img["src_file"], img["file_path"], img["file_size"])
                for img in images
            ],
        )


def update_my_toon_totals(cursor, my_toon_id: int) -> None:
    cursor.execute(
        """
        UPDATE my_toon_info
           SET total_image_cnt = (
                   SELECT COALESCE(SUM(e.saved_image_cnt), 0) FROM my_toon_episode AS e
                    WHERE e.my_toon_id = %s
               )
         WHERE my_toon_id = %s
        """,
        (my_toon_id, my_toon_id),
    )


# ---------------------------------------------------------------------
# 복사
# ---------------------------------------------------------------------
# 펼침면 분리: Pillow 를 설치할 수 없는(인터넷 차단) PC 에서도 되도록 Windows 기본 System.Drawing 을 사용합니다.
# 목록 파일 한 줄 = 원본<TAB>왼쪽 저장 경로<TAB>오른쪽 저장 경로. .tmp 에 저장한 뒤 이름을 바꿉니다.
SPLIT_PS1 = r"""
param([string]$ListFile, [long]$Quality)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$params = New-Object System.Drawing.Imaging.EncoderParameters 1
$params.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, $Quality)
foreach ($line in [System.IO.File]::ReadAllLines($ListFile, [System.Text.Encoding]::UTF8)) {
    if (-not $line) { continue }
    $src, $leftDest, $rightDest = $line -split "`t"
    $img = [System.Drawing.Image]::FromFile($src)
    try {
        $half = [int][Math]::Floor($img.Width / 2)
        foreach ($part in @(@(0, $half, $leftDest), @($half, ($img.Width - $half), $rightDest))) {
            $x = $part[0]; $w = $part[1]; $dest = $part[2]
            $bmp = New-Object System.Drawing.Bitmap $w, $img.Height
            $bmp.SetResolution($img.HorizontalResolution, $img.VerticalResolution)
            $g = [System.Drawing.Graphics]::FromImage($bmp)
            $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
            $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
            $g.DrawImage($img, (New-Object System.Drawing.Rectangle 0, 0, $w, $img.Height),
                (New-Object System.Drawing.Rectangle $x, 0, $w, $img.Height), [System.Drawing.GraphicsUnit]::Pixel)
            $g.Dispose()
            $tmp = "$dest.tmp"
            $bmp.Save($tmp, $codec, $params)
            $bmp.Dispose()
            Move-Item -LiteralPath $tmp -Destination $dest -Force
        }
    } finally { $img.Dispose() }
}
"""


def split_spreads(jobs: list[tuple[Path, Path, Path]]) -> None:
    """(원본, 왼쪽 저장, 오른쪽 저장) 목록을 PowerShell 한 번으로 처리합니다."""
    if not jobs:
        return
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "split.ps1"
        script.write_text(SPLIT_PS1, encoding="utf-8-sig")
        list_file = Path(tmp) / "jobs.tsv"
        list_file.write_text("\n".join(f"{s}\t{l}\t{r}" for s, l, r in jobs), encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-ListFile", str(list_file), "-Quality", str(SPLIT_JPEG_QUALITY)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"    펼침면 분리 실패: {(result.stderr or result.stdout).strip()[:500]}")


def copy_episode_images(ep: dict, dest_dir: Path) -> list[dict]:
    """ep['pages'] 를 dest_dir/001.jpg ... 로 저장합니다 (이미 있으면 건너뜀).

    단면은 원본 복사(같은 크기면 건너뜀), 펼침면은 좌/우로 잘라 저장(파일이 있으면 건너뜀).
    목록에 없는 이전 이미지 파일(예: 페이지 수가 줄어든 경우)은 지웁니다.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    targets: list[tuple[int, Path, str | None, Path]] = []
    for page_no, (src, part) in enumerate(ep["pages"], start=1):
        suffix = ".jpg" if part else FORMAT_EXTS.get(image_format(src) or "", src.suffix.lower())
        targets.append((page_no, src, part, dest_dir / f"{page_no:03d}{suffix}"))

    jobs: dict[Path, list[Path]] = {}
    for _page_no, src, part, dest in targets:
        try:
            if part:
                jobs.setdefault(src, [])
                jobs[src].append(dest)
            elif not (dest.is_file() and dest.stat().st_size == src.stat().st_size):
                shutil.copy2(src, dest)
        except OSError as exc:
            print(f"    복사 실패: {src} → {dest} ({exc})")
    split_spreads([(src, left, right) for src, (left, right) in jobs.items()
                   if not (left.is_file() and right.is_file())])

    keep = {dest for *_rest, dest in targets}
    for old in dest_dir.iterdir():
        if old.is_file() and old.suffix.lower() in IMAGE_EXTS | {".tmp"} and old not in keep:
            old.unlink()

    saved: list[dict] = []
    for page_no, src, part, dest in targets:
        if not dest.is_file():
            continue
        saved.append(
            {
                "page_no": page_no,
                "src_file": src.as_posix() + (f"#{part}" if part else ""),
                "file_path": dest.as_posix(),
                "file_size": dest.stat().st_size,
            }
        )
    return saved


# ---------------------------------------------------------------------
# 정보 파일 (db_toon_step.py 의 {toon_id}_{제목}.txt 와 같은 형식)
# ---------------------------------------------------------------------
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def info_file_path(toon_dir: Path, my_toon_id: int, title: str) -> Path:
    """{toon_dir}/{my_toon_id}_{제목}.txt (파일명에 못 쓰는 문자는 _ 로 치환)."""
    safe = INVALID_FILENAME_RE.sub("_", title or "").strip().rstrip(".") or "untitled"
    return toon_dir / f"{my_toon_id}_{safe[:80]}.txt"


def write_info_file(toon_dir: Path, my_toon_id: int, title: str, src_type: int, root: Path, episodes: list[dict]) -> None:
    """원본 폴더·저장 위치·회차 목록을 텍스트로 남깁니다. 매 실행마다 최신 내용으로 덮어씁니다."""
    dest = info_file_path(toon_dir, my_toon_id, title)
    total = sum(len(ep["pages"]) for ep in episodes)
    lines = [
        f"경로 : {root.as_posix()}",
        f"제목 : {title}",
        "작가 : ",
        "상태 : ",
        f"구조 TYPE : {src_type}",
        f"총 회차 : {len(episodes)}",
        f"총 이미지 : {total}",
        f"이미지 저장 : {toon_dir.as_posix()}/<회차번호>/001.jpg ...",
        f"갱신일시 : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"[회차 목록] {len(episodes)}건 (회차 | 장수 | 제목 | 원본 폴더)",
    ]
    lines += [
        f"{ep['episode_no']} | {len(ep['pages'])} | {ep['title']} | {ep['src_dir'].as_posix()}" for ep in episodes
    ]
    try:
        toon_dir.mkdir(parents=True, exist_ok=True)
        for old in toon_dir.glob(f"{my_toon_id}_*.txt"):  # 제목이 바뀐 경우 이전 파일 정리
            if old != dest:
                old.unlink(missing_ok=True)
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    except OSError as exc:
        print(f"정보 파일 저장에 실패했습니다: {exc}")
        return
    print(f"정보 파일을 저장했습니다: {dest.as_posix()}")


# ---------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="마이 북: 로컬 웹툰 폴더/이미지 분석 → my_toon_* DB 등록")
    parser.add_argument("--TYPE", "--type", dest="type", default="1", help="원본 폴더 구조 TYPE (기본 1, MYBOOK_CREATE.md 참고)")
    # PowerShell 5.1 은 --bookid "" 의 빈 문자열을 빼고 넘기므로 값 없는 --bookid 도 허용
    parser.add_argument("--bookid", nargs="?", const="", default="", help="마이 북 아이디 (없으면 YYYYMMDDHHMMSS 로 신규 생성)")
    parser.add_argument("--name", required=True, help="제목")
    parser.add_argument("--path", required=True, help="원본 폴더 경로")
    parser.add_argument("-o", "--output", default=str(DEFAULT_BASE_PATH), help="복사 루트 경로 (기본 D:/booktoon)")
    parser.add_argument("--dry-run", action="store_true", help="폴더 구조 분석 결과만 출력 (복사/DB 없음)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    src_type = int((args.type or "1").strip() or "1")
    if src_type not in SUPPORTED_TYPES:
        print(f"지원하지 않는 TYPE 입니다: {src_type} (지원: {SUPPORTED_TYPES})")
        return 2
    root = Path(args.path)
    if not root.is_dir():
        print(f"원본 폴더가 없습니다: {root}")
        return 2
    bookid = (args.bookid or "").strip()
    if bookid and not bookid.isdigit():
        print(f"--bookid 는 숫자만 가능합니다: {bookid}")
        return 2

    try:
        prepare_zip_folders(root, args.dry_run)
        episodes, warnings = ANALYZERS[src_type](root)
    except ValueError as exc:
        print(f"[분석 실패] {exc}")
        return 1
    plan_pages(episodes, src_type in SPLIT_TYPES)
    print_analysis(root, src_type, episodes, warnings)
    if args.dry_run:
        print("--dry-run: 복사/DB 등록 없이 종료합니다.")
        return 0

    if not config_path().is_file():
        print(f"DB 설정 파일이 없습니다: {config_path()}")
        return 2
    load_mysql_config()

    base_path = Path(args.output)
    with get_connection() as conn:
        ensure_my_schema(conn)
        with conn.cursor() as cursor:
            if bookid:
                my_toon_id = int(bookid)
                old_type = my_toon_src_type(cursor, my_toon_id)
                print(f"{'기존' if old_type is not None else '지정 아이디로 신규'} 마이 북: {my_toon_id}")
            else:
                my_toon_id = new_my_toon_id(cursor)
                old_type = None
                print(f"신규 마이 북 아이디: {my_toon_id}")
            toon_dir = base_path / str(my_toon_id)
            if old_type is not None and old_type != src_type:
                # 같은 파일명(001.jpg ...)이라도 내용이 달라지므로 이전 TYPE 으로 만든 회차 폴더를 지우고 새로 만듭니다.
                removed = clear_episode_dirs(toon_dir)
                print(f"TYPE {old_type} → {src_type} 변경: 이전 회차 폴더 {removed}개를 지우고 새로 만듭니다.")
            upsert_my_toon_info(
                cursor,
                {
                    "my_toon_id": my_toon_id,
                    "title": args.name.strip(),
                    "total_episode": len(episodes),
                    "src_type": src_type,
                    "src_path": root.as_posix(),
                    "image_root": toon_dir.as_posix(),
                },
            )
        conn.commit()

        failed = 0
        for ep in episodes:
            dest_dir = toon_dir / str(ep["episode_no"])
            saved = copy_episode_images(ep, dest_dir)
            with conn.cursor() as cursor:
                save_my_episode(cursor, my_toon_id, ep, dest_dir.as_posix(), saved)
                update_my_toon_totals(cursor, my_toon_id)
            conn.commit()
            mark = "완료" if len(saved) == len(ep["pages"]) else "일부 실패"
            failed += len(saved) != len(ep["pages"])
            print(f"  - {ep['episode_no']}회 {mark}: {len(saved)}/{len(ep['pages'])}장 → {dest_dir.as_posix()}")

    write_info_file(toon_dir, my_toon_id, args.name.strip(), src_type, root, episodes)
    print(f"마이 북 등록 끝: {my_toon_id} '{args.name.strip()}' ({len(episodes)}회차, 실패 회차 {failed})")
    print(f"  다시 실행(이어하기): python toon_create.py --TYPE {src_type} --bookid {my_toon_id} --name \"{args.name.strip()}\" --path \"{root}\"")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
