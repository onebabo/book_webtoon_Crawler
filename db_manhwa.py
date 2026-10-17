"""만화(manhwa_*) 테이블 DB 처리 (db.py 의 웹툰 toon_* 버전과 같은 구조).

접속 설정(config.ini)·공통 유틸은 db.py 를 그대로 쓰고, 테이블만 manhwa_* 로 분리합니다.
사이트 ID 가 웹툰과 겹치므로(/webtoon/2 ≠ /manhwa/2) toon_* 에 섞지 않습니다.
"""

from contextlib import contextmanager
from pathlib import Path

import pymysql

from db import (
    episode_type_from_text,
    get_local_ip,
    load_mysql_config,
    normalize_categories,
    normalize_status,
)

SCHEMA_SQL = Path(__file__).resolve().parent / "book_sql" / "50_manhwa_테이블생성.sql"  # 테이블 DDL


def schema_statements(path: Path = SCHEMA_SQL) -> list[str]:
    """book_sql/50_manhwa_테이블생성.sql 을 '--' 주석 제거 후 ';' 기준 구문 목록으로 반환합니다."""
    lines = [
        line
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if not line.strip().startswith("--")
    ]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def ensure_schema(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SET NAMES utf8mb4")
        for stmt in schema_statements():
            cursor.execute(stmt)


_schema_checked = False  # 프로세스당 1회만 스키마 보정


@contextmanager
def get_connection():
    global _schema_checked
    settings = load_mysql_config()
    conn = pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=settings["database"],
        charset=settings["charset"],
        autocommit=False,
        connect_timeout=15,
        init_command="SET NAMES utf8mb4",
    )
    try:
        if not _schema_checked:
            ensure_schema(conn)
            _schema_checked = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------
# 만화 기본 정보 / 카테고리 / 회차 목록
# ---------------------------------------------------------------------
def upsert_manhwa_info(conn, info: dict) -> None:
    status_value = normalize_status(info.get("status"))
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO manhwa_info (
                manhwa_id, title, author, total_episode, status, weekday, platform,
                description, rating, bookmark_cnt, source_url, cover_url, cover_path
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                author = COALESCE(VALUES(author), author),
                total_episode = VALUES(total_episode),
                status = VALUES(status),
                weekday = COALESCE(VALUES(weekday), weekday),
                platform = COALESCE(VALUES(platform), platform),
                description = COALESCE(VALUES(description), description),
                rating = COALESCE(VALUES(rating), rating),
                bookmark_cnt = COALESCE(VALUES(bookmark_cnt), bookmark_cnt),
                source_url = VALUES(source_url),
                cover_url = COALESCE(VALUES(cover_url), cover_url),
                cover_path = COALESCE(VALUES(cover_path), cover_path)
            """,
            (
                info["manhwa_id"],
                info["title"],
                info.get("author"),
                info.get("total_episode") or 0,
                status_value,
                info.get("weekday"),
                info.get("platform"),
                info.get("description"),
                info.get("rating"),
                info.get("bookmark_cnt"),
                info.get("source_url"),
                info.get("cover_url"),
                info.get("cover_path"),
            ),
        )


def upsert_manhwa_categories(conn, manhwa_id: int, category_names: list[str]) -> None:
    names = normalize_categories(category_names)
    with conn.cursor() as cursor:
        if names:
            cursor.executemany(
                """
                INSERT INTO manhwa_category (manhwa_id, category_name, use_yn)
                VALUES (%s, %s, 'Y')
                ON DUPLICATE KEY UPDATE use_yn = 'Y'
                """,
                [(manhwa_id, name) for name in names],
            )
            placeholders = ", ".join(["%s"] * len(names))
            cursor.execute(
                f"""
                UPDATE manhwa_category SET use_yn = 'N'
                 WHERE manhwa_id = %s AND category_name NOT IN ({placeholders})
                """,
                (manhwa_id, *names),
            )
        else:
            cursor.execute("UPDATE manhwa_category SET use_yn = 'N' WHERE manhwa_id = %s", (manhwa_id,))


def upsert_manhwa_episode_rows(conn, manhwa_id: int, episodes: list[dict]) -> None:
    """목록 페이지 기준 회차 메타정보만 갱신합니다. 이미지 수집 컬럼은 건드리지 않습니다."""
    sql = """
        INSERT INTO manhwa_episode (
            manhwa_id, episode_code, episode_no, episode_type, episode_title,
            episode_date, org_url, like_cnt, comment_cnt, rating
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            episode_no = VALUES(episode_no),
            episode_type = VALUES(episode_type),
            episode_title = COALESCE(VALUES(episode_title), episode_title),
            episode_date = COALESCE(VALUES(episode_date), episode_date),
            org_url = COALESCE(VALUES(org_url), org_url),
            like_cnt = COALESCE(VALUES(like_cnt), like_cnt),
            comment_cnt = COALESCE(VALUES(comment_cnt), comment_cnt),
            rating = COALESCE(VALUES(rating), rating)
    """
    rows = [
        (
            manhwa_id,
            item["episode_code"],
            item["episode_no"],
            item.get("episode_type") or episode_type_from_text(item.get("episode_title")),
            item.get("episode_title"),
            item.get("episode_date"),
            item.get("org_url"),
            item.get("like_cnt"),
            item.get("comment_cnt"),
            item.get("rating"),
        )
        for item in episodes
    ]
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)


def save_manhwa_basic(info: dict, category_names: list[str], episodes: list[dict]) -> None:
    """manhwa_info + manhwa_category + manhwa_episode(목록 메타) + manhwa_work_list 를 한 번에 갱신합니다."""
    with get_connection() as conn:
        upsert_manhwa_info(conn, info)
        upsert_manhwa_categories(conn, info["manhwa_id"], category_names)
        upsert_manhwa_episode_rows(conn, info["manhwa_id"], episodes)
        with conn.cursor() as cursor:
            sync_work_list(cursor, info["manhwa_id"], mark_started=False)


def fetch_episode_status(manhwa_id: int) -> dict[str, dict]:
    """episode_code -> {episode_no, image_cnt, saved_image_cnt, has_images, image_dir}."""
    sql = """
        SELECT episode_code, episode_no, image_cnt, saved_image_cnt, has_images, image_dir
          FROM manhwa_episode
         WHERE manhwa_id = %s
    """
    result: dict[str, dict] = {}
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (manhwa_id,))
            for code, no, image_cnt, saved, has_images, image_dir in cursor.fetchall():
                result[code] = {
                    "episode_no": no,
                    "image_cnt": int(image_cnt or 0),
                    "saved_image_cnt": int(saved or 0),
                    "has_images": bool(has_images),
                    "image_dir": image_dir,
                }
    return result


def save_episode_images(
    manhwa_id: int,
    episode_code: str,
    image_dir: str,
    image_cnt: int,
    images: list[dict],
) -> None:
    """회차 1건의 이미지 수집 결과를 반영합니다(해당 회차 이미지 행은 새로 교체)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM manhwa_episode_image WHERE episode_code = %s", (episode_code,))
            if images:
                cursor.executemany(
                    """
                    INSERT INTO manhwa_episode_image (
                        manhwa_id, episode_code, page_no, org_url, file_path, file_size
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            manhwa_id,
                            episode_code,
                            img["page_no"],
                            (img.get("org_url") or "")[:1000] or None,
                            img.get("file_path"),
                            img.get("file_size"),
                        )
                        for img in images
                    ],
                )
            cursor.execute(
                """
                UPDATE manhwa_episode
                   SET image_dir = %s,
                       image_cnt = %s,
                       saved_image_cnt = %s,
                       collected_at = NOW()
                 WHERE episode_code = %s
                """,
                (image_dir, image_cnt, len(images), episode_code),
            )
            sync_work_list(cursor, manhwa_id, mark_started=True)


# ---------------------------------------------------------------------
# 수집 대상 목록(manhwa_work_list) 진행 상태
# ---------------------------------------------------------------------
INSERT_MISSING_WORK_LIST_SQL = """
INSERT INTO manhwa_work_list (manhwa_id, title, sort_no, remark)
SELECT i.manhwa_id, i.title, 9999, 'manhwa_info 기준 보충'
  FROM manhwa_info AS i
 WHERE i.manhwa_id = %s
   AND NOT EXISTS (SELECT 1 FROM manhwa_work_list AS w WHERE w.manhwa_id = i.manhwa_id)
"""

SYNC_WORK_LIST_SQL = """
UPDATE manhwa_work_list AS w
  JOIN manhwa_info AS i ON i.manhwa_id = w.manhwa_id
   SET w.title = i.title,
       w.author = COALESCE(i.author, w.author),
       w.total_episode = i.total_episode,
       w.status = i.status,
       w.weekday = COALESCE(i.weekday, w.weekday),
       w.platform = COALESCE(i.platform, w.platform),
       w.source_url = COALESCE(i.source_url, w.source_url),
       w.cover_url = COALESCE(i.cover_url, w.cover_url),
       w.cover_path = COALESCE(i.cover_path, w.cover_path),
       w.category_names = (
           SELECT GROUP_CONCAT(c.category_name ORDER BY c.category_seq SEPARATOR ',')
             FROM manhwa_category AS c
            WHERE c.manhwa_id = i.manhwa_id AND c.use_yn = 'Y'
       ),
       w.last_episode_no = (
           SELECT MAX(e.episode_no) FROM manhwa_episode AS e WHERE e.manhwa_id = i.manhwa_id
       ),
       w.last_episode_title = (
           SELECT e.episode_title FROM manhwa_episode AS e
            WHERE e.manhwa_id = i.manhwa_id ORDER BY e.episode_no DESC LIMIT 1
       ),
       w.last_update_date = (
           SELECT MAX(e.episode_date) FROM manhwa_episode AS e WHERE e.manhwa_id = i.manhwa_id
       ),
       w.collect_episode_cnt = (
           SELECT COALESCE(SUM(e.has_images), 0) FROM manhwa_episode AS e WHERE e.manhwa_id = i.manhwa_id
       ),
       w.collect_image_cnt = (
           SELECT COALESCE(SUM(e.saved_image_cnt), 0) FROM manhwa_episode AS e WHERE e.manhwa_id = i.manhwa_id
       ),
       w.collect_ip = COALESCE(%s, w.collect_ip)
 WHERE w.manhwa_id = %s
"""

APPLY_COLLECT_STATUS_SQL = """
UPDATE manhwa_work_list
   SET collect_yn = CASE
           WHEN total_episode > 0 AND collect_episode_cnt >= total_episode THEN 'Y'
           WHEN collect_episode_cnt > 0 OR %s = 1 THEN 'I'
           ELSE 'N'
       END,
       collect_started_at = CASE
           WHEN collect_started_at IS NULL AND (collect_episode_cnt > 0 OR %s = 1) THEN NOW()
           ELSE collect_started_at
       END,
       collect_ended_at = CASE
           WHEN total_episode > 0 AND collect_episode_cnt >= total_episode
                THEN COALESCE(collect_ended_at, NOW())
           ELSE NULL
       END
 WHERE manhwa_id = %s
"""


def sync_work_list(cursor, manhwa_id: int, mark_started: bool = False) -> None:
    """manhwa_info/manhwa_episode 집계를 manhwa_work_list 에 반영하고 수집 상태(collect_yn)를 다시 계산합니다."""
    cursor.execute(INSERT_MISSING_WORK_LIST_SQL, (manhwa_id,))
    cursor.execute(SYNC_WORK_LIST_SQL, (get_local_ip() or None, manhwa_id))
    flag = 1 if mark_started else 0
    cursor.execute(APPLY_COLLECT_STATUS_SQL, (flag, flag, manhwa_id))


def fetch_work_list_status(manhwa_id: int) -> dict | None:
    with get_connection() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT manhwa_id, title, total_episode, collect_yn, collect_episode_cnt,
                       collect_image_cnt, collect_started_at, collect_ended_at
                  FROM manhwa_work_list
                 WHERE manhwa_id = %s
                """,
                (manhwa_id,),
            )
            return cursor.fetchone()
