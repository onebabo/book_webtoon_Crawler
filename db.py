import configparser
import socket
from contextlib import contextmanager
from pathlib import Path

import pymysql

CONFIG_NAME = "config.ini"  # DB 접속 설정 파일명
SCHEMA_SQL = Path(__file__).resolve().parent / "book_sql" / "00_toon_테이블생성.sql"  # 테이블 DDL
VALID_STATUS = ("연재", "완결", "중단")  # 허용 연재 상태 값


def config_path() -> Path:
    return Path(__file__).resolve().parent / CONFIG_NAME


def load_mysql_config(path: Path | None = None) -> dict:
    config_file = path or config_path()
    parser = configparser.ConfigParser(interpolation=None)
    read_files = parser.read(config_file, encoding="utf-8")
    if not read_files:
        raise FileNotFoundError(f"DB 설정 파일을 찾을 수 없습니다: {config_file}")
    if not parser.has_section("mysql"):
        raise ValueError(f"[mysql] 섹션이 없습니다: {config_file}")

    section = parser["mysql"]
    return {
        "host": section.get("host", "").strip(),
        "port": section.getint("port", 3306),
        "user": section.get("user", "").strip(),
        "password": section.get("password", ""),
        "database": section.get("database", "").strip(),
        "charset": section.get("charset", "utf8mb4").strip() or "utf8mb4",
    }


def normalize_status(value: str | None) -> str:
    text = (value or "").strip()
    if "완결" in text:
        return "완결"
    if "중단" in text or "휴재" in text:
        return "중단"
    return "연재"


def normalize_categories(tags) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        name = " ".join(str(tag).split()).strip().lstrip("#").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def episode_type_from_text(*values: str | None) -> str:
    hay = " ".join(value or "" for value in values)
    if any(word in hay for word in ("외전", "번외", "특전", "후기", "공지", "휴재")):
        return "SIDE"
    return "MAIN"


def schema_statements(path: Path = SCHEMA_SQL) -> list[str]:
    """book_sql/00_toon_테이블생성.sql 을 '--' 주석 제거 후 ';' 기준 구문 목록으로 반환합니다."""
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


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""


# ---------------------------------------------------------------------
# 웹툰 기본 정보 / 카테고리 / 회차 목록
# ---------------------------------------------------------------------
def upsert_toon_info(conn, info: dict) -> None:
    status_value = normalize_status(info.get("status"))
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO toon_info (
                toon_id, title, author, total_episode, status, weekday, platform,
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
                info["toon_id"],
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


def upsert_toon_categories(conn, toon_id: int, category_names: list[str]) -> None:
    names = normalize_categories(category_names)
    with conn.cursor() as cursor:
        if names:
            cursor.executemany(
                """
                INSERT INTO toon_category (toon_id, category_name, use_yn)
                VALUES (%s, %s, 'Y')
                ON DUPLICATE KEY UPDATE use_yn = 'Y'
                """,
                [(toon_id, name) for name in names],
            )
            placeholders = ", ".join(["%s"] * len(names))
            cursor.execute(
                f"""
                UPDATE toon_category SET use_yn = 'N'
                 WHERE toon_id = %s AND category_name NOT IN ({placeholders})
                """,
                (toon_id, *names),
            )
        else:
            cursor.execute("UPDATE toon_category SET use_yn = 'N' WHERE toon_id = %s", (toon_id,))


def upsert_toon_episode_rows(conn, toon_id: int, episodes: list[dict]) -> None:
    """목록 페이지 기준 회차 메타정보만 갱신합니다. 이미지 수집 컬럼은 건드리지 않습니다."""
    sql = """
        INSERT INTO toon_episode (
            toon_id, episode_code, episode_no, episode_type, episode_title,
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
            toon_id,
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


def save_toon_basic(info: dict, category_names: list[str], episodes: list[dict]) -> None:
    """toon_info + toon_category + toon_episode(목록 메타) + toon_work_list 를 한 번에 갱신합니다."""
    with get_connection() as conn:
        upsert_toon_info(conn, info)
        upsert_toon_categories(conn, info["toon_id"], category_names)
        upsert_toon_episode_rows(conn, info["toon_id"], episodes)
        with conn.cursor() as cursor:
            sync_work_list(cursor, info["toon_id"], mark_started=False)


def fetch_episode_status(toon_id: int) -> dict[str, dict]:
    """episode_code -> {episode_no, image_cnt, saved_image_cnt, has_images, image_dir}."""
    sql = """
        SELECT episode_code, episode_no, image_cnt, saved_image_cnt, has_images, image_dir
          FROM toon_episode
         WHERE toon_id = %s
    """
    result: dict[str, dict] = {}
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (toon_id,))
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
    toon_id: int,
    episode_code: str,
    image_dir: str,
    image_cnt: int,
    images: list[dict],
) -> None:
    """회차 1건의 이미지 수집 결과를 반영합니다(해당 회차 이미지 행은 새로 교체)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM toon_episode_image WHERE episode_code = %s", (episode_code,))
            if images:
                cursor.executemany(
                    """
                    INSERT INTO toon_episode_image (
                        toon_id, episode_code, page_no, org_url, file_path, file_size
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            toon_id,
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
                UPDATE toon_episode
                   SET image_dir = %s,
                       image_cnt = %s,
                       saved_image_cnt = %s,
                       collected_at = NOW()
                 WHERE episode_code = %s
                """,
                (image_dir, image_cnt, len(images), episode_code),
            )
            sync_work_list(cursor, toon_id, mark_started=True)


# ---------------------------------------------------------------------
# 수집 대상 목록(toon_work_list) 진행 상태
# ---------------------------------------------------------------------
INSERT_MISSING_WORK_LIST_SQL = """
INSERT INTO toon_work_list (toon_id, title, sort_no, remark)
SELECT i.toon_id, i.title, 9999, 'toon_info 기준 보충'
  FROM toon_info AS i
 WHERE i.toon_id = %s
   AND NOT EXISTS (SELECT 1 FROM toon_work_list AS w WHERE w.toon_id = i.toon_id)
"""

SYNC_WORK_LIST_SQL = """
UPDATE toon_work_list AS w
  JOIN toon_info AS i ON i.toon_id = w.toon_id
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
             FROM toon_category AS c
            WHERE c.toon_id = i.toon_id AND c.use_yn = 'Y'
       ),
       w.last_episode_no = (
           SELECT MAX(e.episode_no) FROM toon_episode AS e WHERE e.toon_id = i.toon_id
       ),
       w.last_episode_title = (
           SELECT e.episode_title FROM toon_episode AS e
            WHERE e.toon_id = i.toon_id ORDER BY e.episode_no DESC LIMIT 1
       ),
       w.last_update_date = (
           SELECT MAX(e.episode_date) FROM toon_episode AS e WHERE e.toon_id = i.toon_id
       ),
       w.collect_episode_cnt = (
           SELECT COALESCE(SUM(e.has_images), 0) FROM toon_episode AS e WHERE e.toon_id = i.toon_id
       ),
       w.collect_image_cnt = (
           SELECT COALESCE(SUM(e.saved_image_cnt), 0) FROM toon_episode AS e WHERE e.toon_id = i.toon_id
       ),
       w.collect_ip = COALESCE(%s, w.collect_ip)
 WHERE w.toon_id = %s
"""

APPLY_COLLECT_STATUS_SQL = """
UPDATE toon_work_list
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
 WHERE toon_id = %s
"""


def sync_work_list(cursor, toon_id: int, mark_started: bool = False) -> None:
    """toon_info/toon_episode 집계를 toon_work_list 에 반영하고 수집 상태(collect_yn)를 다시 계산합니다."""
    cursor.execute(INSERT_MISSING_WORK_LIST_SQL, (toon_id,))
    cursor.execute(SYNC_WORK_LIST_SQL, (get_local_ip() or None, toon_id))
    flag = 1 if mark_started else 0
    cursor.execute(APPLY_COLLECT_STATUS_SQL, (flag, flag, toon_id))


def fetch_work_list_status(toon_id: int) -> dict | None:
    with get_connection() as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT toon_id, title, total_episode, collect_yn, collect_episode_cnt,
                       collect_image_cnt, collect_started_at, collect_ended_at
                  FROM toon_work_list
                 WHERE toon_id = %s
                """,
                (toon_id,),
            )
            return cursor.fetchone()
