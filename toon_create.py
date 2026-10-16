"""마이 북: 현재 PC 에 보관 중인 웹툰 폴더/이미지를 분석해서 DB(my_toon_*) 로 등록합니다.

1) --TYPE 에 맞춰 원본 폴더 구조를 분석 (회차 폴더 / 이미지 순서) → 분석 결과 출력
2) my_toon_info 등록 (--bookid 가 없으면 현재 일시 YYYYMMDDHHMMSS 로 신규 아이디 생성)
3) 회차별로 이미지를 <기본경로>/{my_toon_id}/{episode_no}/001.jpg ... 로 복사
   → my_toon_episode / my_toon_episode_image 갱신 (회차 단위 커밋)

이미 같은 크기로 복사된 파일은 건너뛰므로, 중간에 끊겨도 같은 --bookid 로 다시 실행하면 이어서 진행합니다.
폴더 구조 TYPE 별 규칙은 01_Claude_job/MYBOOK_CREATE.md 에 기록합니다.

  python toon_create.py --TYPE 1 --name "도시정벌 1부" --path "D:\\mynas\\도시정벌 1부(01~12)"
  python toon_create.py --TYPE 1 --name "도시정벌 1부" --path "D:\\mynas\\도시정벌 1부(01~12)" --dry-run
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from db import config_path, episode_type_from_text, get_connection, load_mysql_config, schema_statements

DEFAULT_BASE_PATH = Path("D:/booktoon")
MY_SCHEMA_SQL = Path(__file__).resolve().parent / "book_sql" / "30_my_toon_테이블생성.sql"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
SUPPORTED_TYPES = (1,)


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


ANALYZERS = {1: analyze_type1}


def print_analysis(root: Path, src_type: int, episodes: list[dict], warnings: list[str]) -> None:
    total = sum(len(ep["images"]) for ep in episodes)
    print(f"[분석] TYPE {src_type} : {root}")
    print(f"  회차 {len(episodes)}개 ({episodes[0]['episode_no']}~{episodes[-1]['episode_no']}), 이미지 {total}장")
    for ep in episodes:
        imgs = ep["images"]
        print(f"  - {ep['episode_no']:>4}회 | {ep['title']} | {len(imgs)}장 ({imgs[0].name} ~ {imgs[-1].name})")
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
            len(ep["images"]),
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
def copy_episode_images(ep: dict, dest_dir: Path) -> list[dict]:
    """원본 이미지를 dest_dir/001.jpg ... 로 복사 (같은 크기로 이미 있으면 건너뜀)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for page_no, src in enumerate(ep["images"], start=1):
        dest = dest_dir / f"{page_no:03d}{src.suffix.lower()}"
        size = src.stat().st_size
        try:
            if not (dest.is_file() and dest.stat().st_size == size):
                shutil.copy2(src, dest)
        except OSError as exc:
            print(f"    복사 실패: {src} → {dest} ({exc})")
            continue
        saved.append(
            {
                "page_no": page_no,
                "src_file": src.as_posix(),
                "file_path": dest.as_posix(),
                "file_size": size,
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
    total = sum(len(ep["images"]) for ep in episodes)
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
        f"{ep['episode_no']} | {len(ep['images'])} | {ep['title']} | {ep['src_dir'].as_posix()}" for ep in episodes
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
        episodes, warnings = ANALYZERS[src_type](root)
    except ValueError as exc:
        print(f"[분석 실패] {exc}")
        return 1
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
                print(f"{'기존' if my_toon_exists(cursor, my_toon_id) else '지정 아이디로 신규'} 마이 북: {my_toon_id}")
            else:
                my_toon_id = new_my_toon_id(cursor)
                print(f"신규 마이 북 아이디: {my_toon_id}")
            toon_dir = base_path / str(my_toon_id)
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
            mark = "완료" if len(saved) == len(ep["images"]) else "일부 실패"
            failed += len(saved) != len(ep["images"])
            print(f"  - {ep['episode_no']}회 {mark}: {len(saved)}/{len(ep['images'])}장 → {dest_dir.as_posix()}")

    write_info_file(toon_dir, my_toon_id, args.name.strip(), src_type, root, episodes)
    print(f"마이 북 등록 끝: {my_toon_id} '{args.name.strip()}' ({len(episodes)}회차, 실패 회차 {failed})")
    print(f"  다시 실행(이어하기): python toon_create.py --TYPE {src_type} --bookid {my_toon_id} --name \"{args.name.strip()}\" --path \"{root}\"")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
