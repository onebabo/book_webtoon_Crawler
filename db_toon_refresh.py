"""보유 웹툰·만화 일괄 정보갱신 — 완결 여부 · 전체/수집 회차

1) 완결 판정: 완결 목록을 끝까지 넘기며 작품 ID 를 모은 뒤, 보유 작품이 목록에 있으면 '완결',
   없으면 '연재' 로 {toon|manhwa}_work_list.end_state 에 저장합니다.
     웹툰: {도메인}/end?page=N   (링크 /webtoon/{ID})
     만화: {도메인}/manhwa-end?page=N   (링크 /manhwa/{ID})
   ※ {도메인}/end/{ID} 개별 주소는 완결 작품도 404 라 쓰지 않습니다.
   ※ 빠르게 연속 요청하면 Cloudflare 가 IP 를 차단(403)합니다. 그래서 페이지 사이를 --delay(기본 3초) 만큼 쉬고,
     403 이면 바로 멈춘 뒤 받은 페이지까지 캐시(_refresh_cache/{kind}_end.json)에 남겨 다음 실행에서 이어 받습니다.
     처음부터 다시: --rescan
2) 회차 정보: 작품마다 `db_toon_step.py "{도메인}/webtoon/{ID}" --refresh` (만화: db_manhwa_step.py)
   를 순서대로 실행해 상태·총 회차·수집 현황을 다시 가져옵니다. (이미지 수집은 하지 않음)
   2026-11: 정보 갱신이 원본 IP 대역 차단(NGINX_CIDR_REALIP)을 반복해 일으켜 --refresh 로 바꿨습니다 —
   일반 Chrome 으로 목록 첫 화면 1번만 열고 스크롤·페이지 넘김·섬네일 요청을 하지 않습니다. 작품 사이 --gap(기본 60초),
   차단 화면(종료 코드 5)·로딩 실패(4)·시간 초과면 남은 작품(웹툰·만화 모두)을 건너뛰고 멈춥니다.

- 강제완결(end_state='강제완결', 웹 상세에서 관리자가 지정) 작품은 대상에서 뺍니다.
- 수집 상태(수집완료/수집필요/보류중)는 웹이 총 회차·수집 회차·보류(H)로 계산하므로 따로 저장하지 않습니다.
  우선순위: 수집완료(전체=수집) > 보류중({kind}_edit_holds H) > 수집필요(전체>수집)

예)
  python db_toon_refresh.py                     # 웹툰 + 만화 전체 (완결 판정 + 회차 정보)
  python db_toon_refresh.py --kind toon --end-only
  python db_toon_refresh.py --kind manhwa --id 47
  python db_toon_refresh.py --dry-run

※ 보류(2026-10-31): 1) 완결 판정은 요청이 많아 Cloudflare IP 대역 차단을 일으켜, 개편 전까지 --end-scan 을 줄 때만 실행합니다.
   기본 실행은 2) 회차 정보 갱신만 합니다.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from db import get_connection

DEFAULT_DOMAIN = "https://toki35.com"
END_STATE_SERIAL = "연재"
END_STATE_END = "완결"
END_STATE_FORCE = "강제완결"
PENDING_TITLE_PREFIX = "[등록대기]"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PROJECT_ROOT = Path(__file__).resolve().parent

# kind 별 테이블·완결 목록·크롤러 (웹 book_web src/lib/toon-kind.ts 와 같은 값)
KINDS = {
    "toon": {
        "label": "웹툰",
        "table": "toon",
        "id_col": "toon_id",
        "segment": "webtoon",
        "end_path": "/end",
        "script": "db_toon_step.py",
        "link_re": re.compile(r'href="(?:https?://[^/"]+)?/webtoon/([A-Za-z0-9_-]+)"'),
    },
    "manhwa": {
        "label": "만화",
        "table": "manhwa",
        "id_col": "manhwa_id",
        "segment": "manhwa",
        "end_path": "/manhwa-end",
        "script": "db_manhwa_step.py",
        "link_re": re.compile(r'href="(?:https?://[^/"]+)?/manhwa/(\d+)"'),
    },
}


def ensure_end_state_columns(cursor, table: str) -> None:
    """웹(book_web src/lib/work-state.ts)과 같은 정의 — 먼저 실행한 쪽이 만든다."""
    for column, definition in (
        ("end_state", "VARCHAR(10) NULL DEFAULT NULL COMMENT '완결 상태: 연재/완결/강제완결 (NULL=정보갱신 전)'"),
        ("end_checked_at", "DATETIME NULL DEFAULT NULL COMMENT '정보갱신 완결 확인 시각'"),
    ):
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
            """,
            (table, column),
        )
        if not cursor.fetchone()[0]:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def fetch_targets(kind: str, ids: list[str] | None) -> list[dict]:
    """보유(used=Y) 작품 중 강제완결·등록대기 제외."""
    cfg = KINDS[kind]
    work = f"{cfg['table']}_work_list"
    id_col = cfg["id_col"]
    sql = f"""
        SELECT {id_col}, title
          FROM {work}
         WHERE used = 'Y'
           AND (end_state IS NULL OR end_state <> %s)
           AND title NOT LIKE %s
    """
    params: list = [END_STATE_FORCE, f"{PENDING_TITLE_PREFIX}%"]
    if ids:
        sql += f" AND {id_col} IN ({', '.join(['%s'] * len(ids))})"
        params.extend(ids)
    sql += f" ORDER BY {id_col}"
    with get_connection() as conn:
        with conn.cursor() as cursor:
            ensure_end_state_columns(cursor, work)
            cursor.execute(sql, params)
            return [{"id": str(row[0]), "title": row[1]} for row in cursor.fetchall()]


def fetch_page(url: str, timeout: float) -> tuple[int, str]:
    """(HTTP 상태, 본문) — 이 저장소 venv 에 requests 가 없어 표준 라이브러리로 받는다."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def cache_path(kind: str) -> Path:
    return PROJECT_ROOT / "_refresh_cache" / f"{kind}_end.json"


def load_scan_cache(kind: str, domain: str, rescan: bool) -> dict:
    """이전 실행에서 받은 완결 목록 페이지 — 같은 도메인이고 --rescan 이 아닐 때만 이어 받는다."""
    path = cache_path(kind)
    if not rescan and path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
            if cache.get("domain") == domain:
                return cache
        except (OSError, ValueError):
            pass
    return {"domain": domain, "pages_done": 0, "finished": False, "ids": []}


def save_scan_cache(kind: str, cache: dict) -> None:
    path = cache_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def scan_completed_ids(
    kind: str,
    domain: str,
    wanted: set[str],
    max_pages: int,
    delay: float,
    timeout: float,
    rescan: bool,
) -> tuple[set[str], bool]:
    """완결 목록 페이지를 넘기며 작품 ID 를 모은다 (캐시에서 이어 받기).

    (완결 목록에 있는 보유 작품 ID, 끝까지 확인했는지) 를 돌려준다. 끝까지 못 봤으면 목록에 없다고 연재로
    판정하면 안 되므로 호출하는 쪽에서 찾은 작품만 완결로 반영한다. 보유 작품을 모두 찾으면 그 자리에서 멈춘다.
    """
    cfg = KINDS[kind]
    cache = load_scan_cache(kind, domain, rescan)
    seen: set[str] = set(cache["ids"])
    if cache["finished"]:
        print(f"캐시: 완결 목록 {cache['pages_done']}페이지 전체 확인됨 ({cache.get('updated_at')}). 새로 받으려면 --rescan")
        return seen & wanted, True
    if cache["pages_done"]:
        print(f"캐시: 완결 목록 {cache['pages_done']}페이지까지 받음 — {cache['pages_done'] + 1}페이지부터 이어 받습니다.")

    try:
        for page in range(cache["pages_done"] + 1, max_pages + 1):
            if page > cache["pages_done"] + 1:
                time.sleep(delay)
            url = f"{domain}{cfg['end_path']}?page={page}"
            try:
                status, text = fetch_page(url, timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"완결 목록 {page}페이지 요청 실패: {exc}")
                return seen & wanted, False
            if status != 200:
                blocked = " (Cloudflare 차단 — 잠시 뒤 다시 실행하면 이어 받습니다)" if status == 403 else ""
                print(f"완결 목록 {page}페이지 응답 {status}{blocked}")
                return seen & wanted, False
            new_ids = set(cfg["link_re"].findall(text)) - seen
            if not new_ids:
                cache["finished"] = True
                print(f"완결 목록 {page - 1}페이지까지 확인했습니다 (완결 {cfg['label']} {len(seen)}개).")
                return seen & wanted, True
            seen |= new_ids
            cache["pages_done"] = page
            found = seen & wanted
            if page % 10 == 0:
                print(f"  … {page}페이지 (완결 {len(seen)}개 중 보유 {len(found)}개)")
            if wanted and found >= wanted:
                print(f"보유 {cfg['label']}이(가) 모두 완결 목록에 있어 {page}페이지에서 멈춥니다.")
                return found, True
        print(f"--max-pages {max_pages} 에 도달했습니다 — 끝까지 확인하지 못했습니다.")
        return seen & wanted, False
    finally:
        cache["ids"] = sorted(seen)
        save_scan_cache(kind, cache)


def save_end_states(kind: str, rows: list[tuple[str, str]], dry_run: bool) -> int:
    """(end_state, id) 목록 저장 — 그 사이 강제완결로 바뀐 작품은 건드리지 않는다."""
    if dry_run or not rows:
        return 0
    cfg = KINDS[kind]
    with get_connection() as conn:
        with conn.cursor() as cursor:
            return cursor.executemany(
                f"""
                UPDATE {cfg['table']}_work_list
                   SET end_state = %s, end_checked_at = NOW()
                 WHERE {cfg['id_col']} = %s AND (end_state IS NULL OR end_state <> '강제완결')
                """,
                rows,
            ) or 0


# db_toon_step.py / db_manhwa_step.py: 4 로딩 실패 · 5 차단 화면 — 계속 접속하면 차단이 길어지므로 전체 중단
STOP_EXIT_CODES = {
    4: "목록 화면을 읽지 못했습니다(보안 확인·로딩 실패)",
    5: "원본 사이트가 이 IP 를 차단했습니다(Access denied)",
}

def refresh_episodes(
    kind: str,
    targets: list[dict],
    domain: str,
    headed: bool,
    job_timeout: float,
    dry_run: bool,
    gap: float = 60.0,
) -> tuple[int, list[str], bool]:
    """작품마다 db_toon_step.py / db_manhwa_step.py --refresh 실행 → (성공 수, 실패 ID 목록, 차단 등으로 멈춤).
    headed 는 예전 옵션 호환용(무시)."""
    cfg = KINDS[kind]
    ok = 0
    failed: list[str] = []
    for index, target in enumerate(targets, start=1):
        url = f"{domain}/{cfg['segment']}/{target['id']}"
        # 2026-11: 작품마다 쉬지 않고 사이트에 접속하면 차단된다 — 작품 사이 gap 초(±30%) 대기
        if index > 1 and not dry_run and gap > 0:
            time.sleep(gap * random.uniform(0.7, 1.3))
        # --refresh 는 항상 일반 Chrome(바로 재수집과 같은 방식) — headless·위장 UA 로 접속하지 않는다
        cmd = [sys.executable, cfg["script"], url, "--refresh"]
        print(f"\n[{cfg['label']} {index}/{len(targets)}] {target['id']} {target['title']}")
        if dry_run:
            print("  (dry-run) " + " ".join(cmd))
            continue
        try:
            result = subprocess.run(cmd, cwd=PROJECT_ROOT, timeout=job_timeout)
        except subprocess.TimeoutExpired:
            # 보안 확인·차단으로 화면이 안 뜨는 경우가 많다 — 계속 접속하면 차단이 길어지므로 중단
            print(f"  시간 초과({job_timeout:.0f}초). 남은 작품은 건너뛰고 멈춥니다.")
            failed.extend(t["id"] for t in targets[index - 1:])
            return ok, failed, True
        if result.returncode == 0:
            ok += 1
        elif result.returncode in STOP_EXIT_CODES:
            print(f"  {STOP_EXIT_CODES[result.returncode]}. 남은 작품은 건너뛰고 멈춥니다.")
            failed.extend(t["id"] for t in targets[index - 1:])
            return ok, failed, True
        else:
            print(f"  종료 코드 {result.returncode}")
            failed.append(target["id"])
    return ok, failed, False


def print_collect_summary(kind: str, ids: list[str]) -> None:
    """갱신 후 수집 상태 요약 (웹과 같은 계산: 수집완료 > 보류중 > 수집필요)"""
    if not ids:
        return
    cfg = KINDS[kind]
    t, id_col = cfg["table"], cfg["id_col"]
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT CASE
                         WHEN w.total_episode > 0 AND w.collect_episode_cnt >= w.total_episode THEN '수집완료'
                         WHEN h.hold_state = 'H' THEN '보류중'
                         ELSE '수집필요'
                       END AS state,
                       COUNT(*)
                  FROM {t}_work_list AS w
                  LEFT JOIN {t}_edit_holds AS h ON h.{id_col} = w.{id_col}
                 WHERE w.{id_col} IN ({', '.join(['%s'] * len(ids))})
                 GROUP BY state
                """,
                ids,
            )
            summary = ", ".join(f"{state} {count}" for state, count in cursor.fetchall())
    print(f"{cfg['label']} 수집 상태: {summary}")


def refresh_kind(kind: str, args: argparse.Namespace) -> bool:
    """True 면 차단 등으로 멈춤 — 같은 사이트라 다음 kind 도 실행하지 않는다."""
    cfg = KINDS[kind]
    targets = fetch_targets(kind, args.ids)
    print(f"\n=== {cfg['label']} 정보갱신 대상: {len(targets)}편 (강제완결·등록대기 제외), 도메인 {args.domain}")
    if not targets:
        return

    if not args.skip_end:
        wanted = {t["id"] for t in targets}
        found, complete = scan_completed_ids(
            kind, args.domain, wanted, args.max_pages, args.delay, args.timeout, args.rescan
        )
        if complete:
            rows = [(END_STATE_END if t["id"] in found else END_STATE_SERIAL, t["id"]) for t in targets]
        else:
            print("완결 목록을 끝까지 확인하지 못해 찾은 작품만 완결로 반영합니다.")
            rows = [(END_STATE_END, toon_id) for toon_id in sorted(found)]
        saved = save_end_states(kind, rows, args.dry_run)
        ends = sum(1 for state, _ in rows if state == END_STATE_END)
        print(
            f"{cfg['label']} 완결 판정: 완결 {ends}편, 연재 {len(rows) - ends}편"
            + (" (dry-run, 저장 안 함)" if args.dry_run else f" — {saved}건 저장")
        )

    stopped = False
    if not args.end_only:
        ok, failed, stopped = refresh_episodes(
            kind, targets, args.domain, args.headed, args.job_timeout, args.dry_run, args.gap
        )
        if not args.dry_run:
            print(f"\n{cfg['label']} 회차 정보 갱신: 성공 {ok}편, 실패 {len(failed)}편")
            if failed:
                print("실패 ID: " + " ".join(failed))

    if not args.dry_run:
        print_collect_summary(kind, [t["id"] for t in targets])
    return stopped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="보유 웹툰·만화 일괄 정보갱신 — 완결 목록(/end, /manhwa-end)으로 완결 판정 후 작품별 회차 정보를 다시 가져옵니다. 강제완결 작품은 제외합니다."
    )
    parser.add_argument("--kind", choices=["toon", "manhwa", "all"], default="all", help="대상 (기본 all = 웹툰 + 만화)")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help=f"원본 사이트 도메인 (기본 {DEFAULT_DOMAIN})")
    parser.add_argument("--id", dest="ids", nargs="+", help="지정 작품만 (--kind toon 또는 manhwa 와 함께)")
    parser.add_argument("--end-only", action="store_true", help="완결 판정만 (회차 정보 갱신 생략)")
    # 보류(2026-10-31): 완결 목록 점검이 사이트에 요청을 많이 보내 Cloudflare 가 IP 대역을 차단(NGINX_CIDR_REALIP)한다.
    # 점검 방식을 개편하기 전까지 기본은 완결 판정을 하지 않고, --end-scan 을 줄 때만 실행한다.
    parser.add_argument("--end-scan", action="store_true", help="(보류 중) 완결 목록 점검으로 완결 판정 — 요청이 많아 차단 위험")
    parser.add_argument("--headed", action="store_true", help="(무시 — 정보 갱신은 항상 일반 Chrome)")
    parser.add_argument("--max-pages", type=int, default=1000, help="완결 목록 최대 페이지 (기본 1000)")
    parser.add_argument("--delay", type=float, default=3.0, help="완결 목록 페이지 간 대기(초, 기본 3 — 짧으면 Cloudflare 차단)")
    parser.add_argument("--rescan", action="store_true", help="완결 목록 캐시를 버리고 1페이지부터 다시 받기")
    parser.add_argument("--timeout", type=float, default=30, help="완결 목록 페이지 요청 제한(초)")
    parser.add_argument("--job-timeout", type=float, default=1800, help="작품 1건 회차 정보 갱신 제한(초)")
    parser.add_argument("--gap", type=float, default=60.0, help="작품 사이 대기(초, ±30%%, 기본 60 — 짧으면 사이트 차단)")
    parser.add_argument("--dry-run", action="store_true", help="DB 저장·크롤러 실행 없이 결과만 출력")
    args = parser.parse_args()
    args.skip_end = not args.end_scan
    if args.end_only and args.skip_end:
        parser.error("--end-only 는 --end-scan 과 함께 쓰세요 (완결 판정은 보류 중).")
    args.domain = args.domain.strip().rstrip("/")
    if args.ids and args.kind == "all":
        parser.error("--id 는 --kind toon 또는 --kind manhwa 와 함께 쓰세요.")
    return args


def main() -> int:
    args = parse_args()
    started = datetime.now()
    for index, kind in enumerate(["toon", "manhwa"] if args.kind == "all" else [args.kind]):
        # 웹툰 → 만화도 같은 원본 사이트 — 사이에 gap 만큼 쉰다
        if index and not args.dry_run and not args.end_only and args.gap > 0:
            time.sleep(args.gap * random.uniform(0.7, 1.3))
        if refresh_kind(kind, args):
            print("원본 사이트 차단·로딩 실패로 멈췄습니다. 원본이 브라우저에서 정상으로 열릴 때까지 다시 실행하지 마세요.")
            break
    print(f"\n완료 ({(datetime.now() - started).seconds}초)")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
