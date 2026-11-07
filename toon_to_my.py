"""웹툰(toon_*) → 마이 북(my_toon_*) 전환: 수집이 끝난 웹툰을 마이 북으로 새로 만듭니다.

- 기존 웹툰 아이디를 쓰지 않고 마이 북 아이디(YYYYMMDDHHMMSS)를 새로 만들어 등록합니다 (TYPE 5).
- 원본 = toon_episode / toon_episode_image 의 file_path (D:/booktoon/{toon_id}/{회차}/001.jpg ...)
  → <기본경로>/{my_toon_id}/{회차}/001.jpg ... 로 복사. 회차 번호·제목·구분은 웹툰 그대로.
- 제목·작가·상태·플랫폼·소개는 toon_info 에서, 썸네일은 cover/{toon_id}.jpg 를 cover/{my_toon_id}.jpg 로 복사합니다.
- 기존 웹툰 데이터와 이미지 폴더는 지우지 않고, toon_work_list.used = 'N' (사용중지) 로만 바꿉니다.

이미 복사된 파일은 건너뛰므로, 중간에 끊겨도 같은 --bookid 로 다시 실행하면 이어서 진행합니다.

  python toon_to_my.py --toon-id 9264 --dry-run
  python toon_to_my.py --toon-id 9264
  python toon_to_my.py --toon-id 9264 --bookid 20261031120000   # 이어하기
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from db import config_path, get_connection, load_mysql_config
from toon_create import (
    DEFAULT_BASE_PATH,
    copy_episode_images,
    cover_file_path,
    ensure_cover,
    ensure_my_schema,
    my_toon_exists,
    new_my_toon_id,
    save_my_episode,
    update_my_toon_cover,
    update_my_toon_totals,
    upsert_my_toon_info,
    write_info_file,
)

SRC_TYPE = 5  # 웹툰(toon_*) 전환 (MYBOOK_CREATE.md 참고)


def load_toon(cursor, toon_id: str) -> tuple[dict, list[dict], list[str]]:
    """toon_info + 회차/이미지 목록을 읽어 toon_create 의 episodes 형식으로 돌려줍니다."""
    cursor.execute(
        "SELECT title, author, status, platform, description, cover_path FROM toon_info WHERE toon_id = %s",
        (toon_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"toon_info 에 없는 웹툰입니다: {toon_id}")
    info = dict(zip(("title", "author", "status", "platform", "description", "cover_path"), row))

    cursor.execute(
        """
        SELECT episode_code, episode_no, episode_title, image_dir, image_cnt, saved_image_cnt
          FROM toon_episode WHERE toon_id = %s ORDER BY episode_no, episode_seq
        """,
        (toon_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ValueError(f"toon_episode 에 회차가 없습니다: {toon_id}")

    warnings: list[str] = []
    episodes: list[dict] = []
    seen: set[int] = set()
    for code, no, title, image_dir, image_cnt, saved_cnt in rows:
        if no in seen:
            raise ValueError(f"회차 번호 {no} 가 중복됩니다 ({code})")
        seen.add(no)
        if saved_cnt < image_cnt:
            warnings.append(f"{no}회: 웹툰 이미지 수집 미완료 ({saved_cnt}/{image_cnt})")
        cursor.execute(
            "SELECT file_path FROM toon_episode_image WHERE episode_code = %s ORDER BY page_no",
            (code,),
        )
        images = [Path(p) for (p,) in cursor.fetchall() if p]
        missing = [p for p in images if not p.is_file()]
        if missing:
            warnings.append(f"{no}회: 이미지 파일 없음 {len(missing)}장 (예: {missing[0].as_posix()})")
            images = [p for p in images if p.is_file()]
        if not images:
            warnings.append(f"이미지가 없는 회차를 건너뜁니다: {no}회 {title}")
            continue
        episodes.append({
            "episode_no": no,
            "title": title or f"{no}화",
            "src_dir": Path(image_dir) if image_dir else images[0].parent,
            "images": images,
            "pages": [(p, None) for p in images],
            "spreads": 0,
        })
    if not episodes:
        raise ValueError(f"복사할 이미지가 있는 회차가 없습니다: {toon_id}")
    return info, episodes, warnings


def update_my_toon_meta(cursor, my_toon_id: int, info: dict) -> None:
    cursor.execute(
        """
        UPDATE my_toon_info
           SET author = %s, status = %s, platform = %s, description = %s
         WHERE my_toon_id = %s
        """,
        (info["author"], info["status"], info["platform"], info["description"], my_toon_id),
    )


def copy_toon_cover(base_path: Path, toon_id: str, my_toon_id: int, cover_path: str | None) -> Path | None:
    """웹툰 썸네일을 cover/{my_toon_id}.jpg 로 복사합니다 (이미 있으면 그대로)."""
    dest = cover_file_path(base_path, my_toon_id)
    if dest.is_file():
        return dest
    src = Path(cover_path) if cover_path else base_path / "cover" / f"{toon_id}.jpg"
    if not src.is_file():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    print(f"썸네일을 복사했습니다: {src.as_posix()} → {dest.as_posix()}")
    return dest


def disable_toon(cursor, toon_id: str, my_toon_id: int) -> None:
    """기존 웹툰은 지우지 않고 사용중지(used = 'N') 처리하고, 비고에 전환 내역을 남깁니다."""
    cursor.execute(
        """
        UPDATE toon_work_list
           SET used = 'N',
               remark = LEFT(CONCAT('마이 북 전환 → ', %s, IF(remark IS NULL OR remark = '', '', CONCAT(' / ', remark))), 500)
         WHERE toon_id = %s
        """,
        (str(my_toon_id), toon_id),
    )
    if cursor.rowcount == 0:
        print(f"주의: toon_work_list 에 {toon_id} 가 없어 사용중지 처리를 하지 못했습니다.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="웹툰(toon_*) → 마이 북(my_toon_*) 신규 생성 전환")
    parser.add_argument("--toon-id", required=True, help="웹툰 아이디 (toon_info.toon_id)")
    parser.add_argument("--bookid", nargs="?", const="", default="", help="마이 북 아이디 (없으면 YYYYMMDDHHMMSS 로 신규 생성)")
    parser.add_argument("--name", default="", help="제목 (기본: toon_info.title)")
    parser.add_argument("-o", "--output", default=str(DEFAULT_BASE_PATH), help="복사 루트 경로 (기본 D:/booktoon)")
    parser.add_argument("--keep-used", action="store_true", help="기존 웹툰을 사용중지(used=N) 하지 않음")
    parser.add_argument("--dry-run", action="store_true", help="전환 대상만 출력 (복사/DB 없음)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toon_id = args.toon_id.strip()
    bookid = (args.bookid or "").strip()
    if bookid and not bookid.isdigit():
        print(f"--bookid 는 숫자만 가능합니다: {bookid}")
        return 2
    if not config_path().is_file():
        print(f"DB 설정 파일이 없습니다: {config_path()}")
        return 2
    load_mysql_config()

    base_path = Path(args.output)
    with get_connection() as conn:
        with conn.cursor() as cursor:
            try:
                info, episodes, warnings = load_toon(cursor, toon_id)
            except ValueError as exc:
                print(f"[분석 실패] {exc}")
                return 1
        title = args.name.strip() or info["title"]
        total = sum(len(ep["pages"]) for ep in episodes)
        print(f"[분석] 웹툰 {toon_id} '{title}' / {info['author']} / {info['status']}")
        print(f"  회차 {len(episodes)}개 ({episodes[0]['episode_no']}~{episodes[-1]['episode_no']}), 이미지 {total}장")
        for msg in warnings:
            print(f"  ! {msg}")
        if args.dry_run:
            print("--dry-run: 복사/DB 등록 없이 종료합니다.")
            return 0

        ensure_my_schema(conn)
        with conn.cursor() as cursor:
            if bookid:
                my_toon_id = int(bookid)
                print(f"{'기존' if my_toon_exists(cursor, my_toon_id) else '지정 아이디로 신규'} 마이 북: {my_toon_id}")
            else:
                my_toon_id = new_my_toon_id(cursor)
                print(f"신규 마이 북 아이디: {my_toon_id}")
            toon_dir = base_path / str(my_toon_id)
            upsert_my_toon_info(cursor, {
                "my_toon_id": my_toon_id,
                "title": title,
                "total_episode": len(episodes),
                "src_type": SRC_TYPE,
                "src_path": (base_path / toon_id).as_posix(),
                "image_root": toon_dir.as_posix(),
            })
            update_my_toon_meta(cursor, my_toon_id, info)
        conn.commit()

        failed = 0
        for ep in episodes:
            dest_dir = toon_dir / str(ep["episode_no"])
            saved = copy_episode_images(ep, dest_dir)
            with conn.cursor() as cursor:
                save_my_episode(cursor, my_toon_id, ep, dest_dir.as_posix(), saved)
                update_my_toon_totals(cursor, my_toon_id)
            conn.commit()
            ok = len(saved) == len(ep["pages"])
            failed += not ok
            if not ok:
                print(f"  - {ep['episode_no']}회 일부 실패: {len(saved)}/{len(ep['pages'])}장 → {dest_dir.as_posix()}")
        print(f"  회차 복사 끝: {len(episodes) - failed}/{len(episodes)}회 완료")

        cover = copy_toon_cover(base_path, toon_id, my_toon_id, info["cover_path"]) \
            or ensure_cover(base_path, my_toon_id, toon_dir)
        with conn.cursor() as cursor:
            if cover:
                update_my_toon_cover(cursor, my_toon_id, cover)
            if not failed and not args.keep_used:
                disable_toon(cursor, toon_id, my_toon_id)
                print(f"기존 웹툰 {toon_id} 사용중지 처리 (toon_work_list.used = 'N')")
        conn.commit()

    write_info_file(toon_dir, my_toon_id, title, SRC_TYPE, base_path / toon_id, episodes)
    print(f"마이 북 전환 끝: 웹툰 {toon_id} → {my_toon_id} '{title}' ({len(episodes)}회차, 실패 회차 {failed})")
    if failed:
        print(f"  실패 회차가 있어 기존 웹툰은 사용중지하지 않았습니다. 이어하기: python toon_to_my.py --toon-id {toon_id} --bookid {my_toon_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
