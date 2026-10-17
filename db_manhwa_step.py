"""만화 1건 수집 파이프라인 (db_toon_step.py 웹툰 버전을 만화용으로 복사·수정).

만화 페이지(/manhwa/{id})는 웹툰과 화면 구조(hero-v2 / ep-row-v2 / vw-imgs)가 같고 주소·ID만 다릅니다.
ID 가 웹툰과 겹치므로(/webtoon/2 ≠ /manhwa/2) DB 테이블·저장 폴더·브라우저 프로필을 따로 씁니다.

1) 만화 목록 페이지(https://toki35.com/manhwa/{manhwa_id})에서 작품 정보·회차 목록·섬네일 추출
2) manhwa_info / manhwa_category / manhwa_episode / manhwa_work_list DB 갱신
3) (--createonly Y 가 아니면) 이미지 미수집 회차의 뷰어 페이지를 같은 브라우저로 순서대로 열어
   이미지를 로컬에 직접 저장: <기본경로>/{manhwa_id}/{episode_no}/001.jpg ...
   → manhwa_episode_image / manhwa_episode(image_cnt, saved_image_cnt) / manhwa_work_list 갱신

브라우저: `.toki_browser_manhwa` 프로필로 headless 1차 접근 → Cloudflare/타임아웃이면
일반 Chrome(CDP) 창을 띄워 수동 통과 후 이어서 진행합니다. 이미 저장된 회차는 건너뛰므로
중간에 끊겨도 같은 명령을 다시 실행하면 이어서 수집합니다.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from db import config_path, load_mysql_config
from db_manhwa import (
    fetch_episode_status,
    fetch_work_list_status,
    save_episode_images,
    save_manhwa_basic,
)
from toki_chrome_cdp import (
    clear_cdp_port,
    close_profile_chrome,
    connect_cdp_page,
    start_chrome_cdp,
    stop_chrome_proc,
)

DEFAULT_BASE_PATH = Path("D:/bookmanhwa")  # 기본 저장 루트 경로 (웹툰 D:/booktoon 과 ID 가 겹쳐 분리)
# 웹툰 수집(db_toon_step.py, .toki_browser)과 동시에 돌려도 서로 크롬을 닫지 않도록 프로필 분리
DEFAULT_USER_DATA_DIR = Path("D:/booktool/booktoon/.toki_browser_manhwa")
DEFAULT_URL = "https://toki35.com/manhwa/12751"  # 기본 만화 URL(미지정 시)
EPISODE_WAIT_SEC = 2.0  # 회차 간 대기(초)
IMAGE_RETRY = 2  # 이미지 1장 다운로드 재시도 횟수
# 뷰어에 섞인 가짜 이미지 확장자. 진짜 장면도 .js/.css/.woff/.json 등으로 위장해 오므로 그런 확장자는 넣지 않는다.
DECOY_IMAGE_EXTS = {"svg"}
SCROLL_STABLE_ROUNDS = 3  # 회차 목록 무한스크롤: 개수 변화 없는 연속 횟수
# 뷰어 자동 스크롤(img loading=lazy 강제 로딩). 실측(105장/115,000px): 1.0초=100/105장, 1.5초 이상=전부,
# 한 번에 ~7,000px 넘게 건너뛰면 사이 이미지가 로딩되지 않음 → 2초 기본, 한 스텝 최대 2,500px, 최대 3초.
VIEWER_SCROLL_MS = 4000  # 최하단까지 기본 스크롤 시간(ms)
VIEWER_SCROLL_MAX_MS = 15000  # 긴 회차(60장+)에서도 lazy 로딩이 따라오도록 상한 확대
VIEWER_SCROLL_STEP_MS = 50  # 스크롤 간격(ms)
VIEWER_SCROLL_MAX_JUMP = 1500  # 한 스텝 최대 이동(px) — 너무 크면 사이 장 누락
# 스크롤 중 응답을 캐시하지만, 미완료 장이 있으면 강제 로드로 보충하므로 여유 있게 대기
VIEWER_LOAD_WAIT_MS = 8000  # 스크롤 후 이미지 로딩 완료 대기 상한(ms)

MANHWA_ID_RE = re.compile(r"/manhwa/(\d+)(?:/|$)")
EPISODE_URL_RE = re.compile(r"/manhwa/(\d+)/([^/?#]+)/?$")  # 회차(뷰어) URL
EPISODE_NO_RE = re.compile(r"-(\d+)$")

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
STEALTH_INIT_JS = """
Object.defineProperty(navigator, "webdriver", { get: () => undefined });
"""
# Cloudflare Turnstile / 봇 확인 페이지 감지
CF_CHALLENGE_JS = """() => {
    const text = ((document.title || "") + " " + (document.body && document.body.innerText || "")).slice(0, 4000);
    if (/보안 확인|사람인지 확인|Verify you are human|Just a moment|Checking your browser|cf-browser-verification/i.test(text)) {
        return true;
    }
    return !!(
        document.querySelector(
            "#challenge-form, .cf-turnstile, iframe[src*='challenges.cloudflare'], "
            + "iframe[src*='turnstile'], [data-sitekey], #cf-challenge-running"
        )
    );
}"""

LIST_READY_SELECTOR = "h1.hero-v2-title"
EPISODE_ROW_SELECTOR = "li.ep-row-v2"
VIEWER_IMG_SELECTOR = "div.vw-imgs img.viewer-ratio-img"

# 회차 100개 초과 시 nav.episode-pager 로 페이징(?epage=N, 최신순). » 링크가 마지막 페이지.
EPISODE_PAGER_JS = """() => {
    let max = 1;
    document.querySelectorAll("nav.episode-pager a[href*='epage='], nav.pager a[href*='epage=']").forEach((a) => {
        const m = (a.getAttribute("href") || "").match(/[?&]epage=(\\d+)/);
        if (m) max = Math.max(max, parseInt(m[1], 10));
    });
    return max;
}"""

# 목록 페이지: section.hero-v2(작품 정보) + li.ep-row-v2(회차) + ld+json(Book)
EXTRACT_LIST_JS = """() => {
    const txt = (el) => (el ? (el.innerText || el.textContent || "") : "").replace(/\\s+/g, " ").trim();
    const q = (sel, root = document) => root.querySelector(sel);
    const qa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

    let ld = {};
    for (const node of qa('script[type="application/ld+json"]')) {
        try {
            const obj = JSON.parse(node.textContent || "{}");
            if (obj && obj["@type"] === "Book") { ld = obj; break; }
        } catch (e) {}
    }

    const stats = {};
    for (const st of qa(".hero-v2-stats .stat")) {
        stats[txt(q(".stat-key", st))] = txt(q(".stat-num", st)).replace(/[★\\s,]/g, "");
    }
    const descEl = q(".hero-v2-desc");
    const coverImg = q(".hero-v2-thumb img");

    const episodes = qa("li.ep-row-v2").map((li) => {
        const link = q("a.ep-row-v2-link", li) || q("a[href]", li);
        const val = (sel) => txt(q(sel + " .ep-stat-val", li));
        return {
            href: link ? link.href : "",
            number: li.getAttribute("data-episode-number") || txt(q(".ep-row-v2-no", li)),
            // 만화는 <strong>1화</strong><span class="sub">작품명…1화</span> 구조 → strong(회차 제목)만 사용
            title: txt(q(".ep-row-v2-title strong", li)) || txt(q(".ep-row-v2-title", li)),
            date: txt(q(".ep-row-v2-date", li)),
            like: val(".ep-stat-like"),
            comments: val(".ep-stat-comments"),
            rating: val(".ep-stat-rating"),
        };
    });

    return {
        title: txt(q("h1.hero-v2-title")) || ld.name || "",
        authors: qa(".hero-v2-author a").map(txt).filter(Boolean),
        ldAuthor: (ld.author && ld.author.name) || "",
        status: txt(q(".pill-status")).replace(/●/g, "").trim(),
        weekday: txt(q(".pill-day")).replace(/\\s+/g, ""),
        platform: txt(q(".pill-plat")) || (ld.publisher && ld.publisher.name) || "",
        tags: qa(".hero-v2-tags .hero-v2-tag").map((a) => txt(a).replace(/^#\\s*/, "")),
        ldGenre: Array.isArray(ld.genre) ? ld.genre : (ld.genre ? [ld.genre] : []),
        description: ld.description || (descEl ? (descEl.innerText || descEl.textContent || "").trim() : ""),
        stats,
        ldRating: ld.aggregateRating ? ld.aggregateRating.ratingValue : null,
        ldPages: ld.numberOfPages || null,
        coverUrl: (coverImg && (coverImg.currentSrc || coverImg.src)) || ld.image || "",
        episodes,
    };
}"""

# 뷰어 페이지: div.vw-imgs img.viewer-ratio-img (alt="page N"), 위장 확장자(.json/.woff2/.js)지만 실제 JPEG
EXTRACT_VIEWER_JS = """() => Array.from(document.querySelectorAll("div.vw-imgs img.viewer-ratio-img")).map((img) => ({
    src: img.getAttribute("src") || img.getAttribute("data-src") || img.currentSrc || "",
    alt: img.getAttribute("alt") || "",
}))"""

# 뷰어 최하단까지 일정 속도로 스크롤. 스크롤 중 늘어나는 높이를 매 스텝 다시 계산합니다.
VIEWER_SCROLL_JS = """async ([baseMs, maxMs, stepMs, maxJump]) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const height = () => document.documentElement.scrollHeight - window.innerHeight;
    const start = performance.now();
    const total = Math.max(1, height());
    const duration = Math.min(maxMs, Math.max(baseMs, total / maxJump * stepMs));
    const step = Math.max(16, Math.min(stepMs, duration * maxJump / total));
    while (true) {
        const elapsed = performance.now() - start;
        window.scrollTo(0, Math.min(1, elapsed / duration) * height());
        if (elapsed >= duration) break;
        await sleep(step);
    }
    window.scrollTo(0, height());
    return Math.round(performance.now() - start);
}"""
VIEWER_LOADED_JS = """() => Array.from(document.querySelectorAll("div.vw-imgs img.viewer-ratio-img"))
    .every((img) => img.complete && img.naturalWidth > 0)"""

BROWSER_FETCH_JS = """async (url) => {
    try {
        // 스크롤로 이미 받은 이미지는 디스크/메모리 캐시 우선(네트워크·CDN 충돌 회피)
        let response;
        try {
            response = await fetch(url, {
                credentials: "omit",
                referrerPolicy: "no-referrer",
                cache: "force-cache",
            });
            if (!response.ok) throw new Error("cache miss");
        } catch (e) {
            response = await fetch(url, { credentials: "omit", referrerPolicy: "no-referrer" });
        }
        if (!response.ok) return { ok: false, status: response.status, b64: "" };
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = "";
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        return { ok: true, status: response.status, b64: btoa(binary) };
    } catch (e) {
        return { ok: false, status: 0, b64: "", error: String(e) };
    }
}"""


class ChallengeError(RuntimeError):
    """headless 에서 Cloudflare/캡차에 막힘 → CDP(일반 Chrome)로 다시 시도해야 함."""


# ---------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------
def manhwa_id_from_url(url: str) -> str:
    match = MANHWA_ID_RE.search(urlparse(url).path)
    if not match:
        raise ValueError(f"만화 ID를 URL에서 찾을 수 없습니다: {url}")
    return match.group(1)


def split_episode_url(url: str) -> tuple[str, str | None]:
    """회차(뷰어) URL 이면 (목록 URL, 회차 코드), 목록 URL 이면 (url, None)."""
    parsed = urlparse(url)
    match = EPISODE_URL_RE.search(parsed.path)
    if not match:
        return url, None
    return parsed._replace(path=f"/manhwa/{match.group(1)}", query="", fragment="").geturl(), match.group(2)


def resolve_base_path(base_path: str | None) -> Path:
    if base_path and str(base_path).strip():
        return Path(base_path)
    return DEFAULT_BASE_PATH


def cover_file_path(base_path: Path, manhwa_id: str) -> Path:
    return base_path / "cover" / f"{manhwa_id}.jpg"


def episode_dir(base_path: Path, manhwa_id: str, episode_no: int) -> Path:
    return base_path / str(manhwa_id) / str(episode_no)


INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def info_file_path(base_path: Path, manhwa_id: str, title: str) -> Path:
    """<기본경로>/{manhwa_id}/{manhwa_id}_{제목}.txt (파일명에 못 쓰는 문자는 _ 로 치환)."""
    safe = INVALID_FILENAME_RE.sub("_", title or "").strip().rstrip(".") or "untitled"
    return base_path / str(manhwa_id) / f"{manhwa_id}_{safe[:80]}.txt"


def write_info_file(base_path: Path, manhwa_id: str, info: dict, categories: list[str], episodes: list[dict]) -> Path | None:
    """만화 원본 경로·섬네일·로컬 저장 위치 등을 텍스트로 남깁니다. 매 실행마다 최신 내용으로 덮어씁니다."""
    dest = info_file_path(base_path, manhwa_id, info.get("title") or "")
    lines = [
        f"경로 : {info.get('source_url') or ''}",
        f"제목 : {info.get('title') or ''}",
        f"작가 : {info.get('author') or ''}",
        f"상태 : {info.get('status') or ''}",
        f"요일 : {info.get('weekday') or ''}",
        f"플랫폼 : {info.get('platform') or ''}",
        f"태그 : {', '.join(categories or [])}",
        f"총 회차 : {info.get('total_episode') or 0}",
        f"평점 : {info.get('rating') if info.get('rating') is not None else ''}",
        f"북마크 : {info.get('bookmark_cnt') if info.get('bookmark_cnt') is not None else ''}",
        f"썸네일 : {info.get('cover_url') or ''}",
        f"썸네일 저장 : {info.get('cover_path') or cover_file_path(base_path, manhwa_id).as_posix()}",
        f"이미지 저장 : {(base_path / str(manhwa_id)).as_posix()}/<회차번호>/001.jpg ...",
        f"갱신일시 : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "[소개]",
        (info.get("description") or "").strip(),
        "",
        f"[회차 목록] {len(episodes)}건 (회차 | 등록일 | 제목 | 경로)",
    ]
    lines += [
        f"{ep['episode_no']} | {ep.get('episode_date') or ''} | {ep.get('episode_title') or ''} | {ep.get('org_url') or ''}"
        for ep in episodes
    ]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        for old in dest.parent.glob(f"{manhwa_id}_*.txt"):  # 제목이 바뀐 경우 이전 파일 정리
            if old != dest:
                old.unlink(missing_ok=True)
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    except OSError as e:
        print(f"정보 파일 저장에 실패했습니다(수집은 계속): {e}")
        return None
    print(f"정보 파일을 저장했습니다: {dest.as_posix()}")
    return dest


def to_int(value) -> int | None:
    text = re.sub(r"[^\d]", "", str(value or ""))
    return int(text) if text else None


def to_float(value) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def parse_episode_date(value: str) -> str | None:
    """'26.10.04' / '2026.10.04' / '2026-10-04' → '2026-10-04'. '1일 전' 등 상대표기는 None."""
    match = re.search(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", value or "")
    if not match:
        return None
    year, month, day = (int(x) for x in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def is_decoy_image_url(url: str) -> bool:
    """뷰어 이미지 목록에 섞인 가짜 이미지(svg, placeholder)인지.
    원본 사이트에서도 만화 장면이 아니므로 받지 않고 회차 장수(image_cnt)에서도 뺀다."""
    path = unquote(urlparse(url).path).lower()
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ext in DECOY_IMAGE_EXTS or "placeholder" in path


def image_ext(body: bytes | None) -> str | None:
    """JPEG/PNG/WEBP/GIF 매직바이트로 확장자를 판별합니다. HTML·에러페이지는 None."""
    if not body or len(body) < 12:
        return None
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
        return "gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return None


def parse_episode_spec(spec: str | None) -> set[int] | None:
    """'1,3,5-10' → {1,3,5,...,10}. 미지정이면 None(전체)."""
    if not spec or not str(spec).strip():
        return None
    result: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            result.update(range(min(start, end), max(start, end) + 1))
        else:
            result.add(int(part))
    return result


# ---------------------------------------------------------------------
# 브라우저 세션 (headless 1차 → CDP 일반 Chrome)
# ---------------------------------------------------------------------
def is_cf_challenge(page) -> bool:
    try:
        return bool(page.evaluate(CF_CHALLENGE_JS))
    except Exception:
        return False


def _is_closed_error(exc: BaseException) -> bool:
    return "has been closed" in str(exc).lower() or "target closed" in str(exc).lower()


def _same_url(current: str, url: str) -> bool:
    left, right = urlparse(current), urlparse(url)
    if not left.netloc or not right.netloc:
        return False
    return (
        left.netloc == right.netloc
        and left.path.rstrip("/") == right.path.rstrip("/")
        and left.query == right.query  # ?epage=2 같은 회차 페이지는 다른 주소
    )


def wait_selector_or_challenge(session, selector: str, timeout_ms: int, what: str) -> None:
    """selector 가 뜰 때까지 기다립니다. CF 확인이면 headless 는 ChallengeError, headed 는 수동 통과 대기."""
    headless = session.headless
    deadline = time.monotonic() + timeout_ms / 1000
    prompted = False
    recoveries = 0
    while time.monotonic() < deadline:
        page = session.ensure_page()
        try:
            if page.query_selector(selector):
                return
            if is_cf_challenge(page):
                if headless:
                    raise ChallengeError(f"{what}: Cloudflare 보안 확인(Turnstile)에 막혔습니다.")
                if not prompted:
                    print("Cloudflare 보안 확인이 감지되었습니다.")
                    print("Chrome 창에서 체크박스가 보이면 클릭하세요. 빙글빙글만 돌면 F5 후 다시 기다리세요.")
                    print("프로필 오류 창이 보이면 확인을 누른 뒤 이 창을 닫지 마세요.")
                    prompted = True
                page.wait_for_timeout(1000)
                continue
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            page.wait_for_selector(selector, timeout=min(2000, remaining_ms))
            return
        except PlaywrightTimeoutError:
            continue
        except ChallengeError:
            raise
        except Exception as e:
            text = str(e).lower()
            if "destroyed" in text or "navigation" in text:
                continue
            if not _is_closed_error(e):
                raise
            recoveries += 1
            session.page = None
            if recoveries > 3:
                raise ChallengeError(
                    "Chrome 창이 닫혔습니다. "
                    "'프로필을 여는 동안 문제가 발생했습니다' 창이 보이면 확인을 누른 뒤 다시 실행하세요."
                ) from e
            time.sleep(0.5)
    if headless:
        raise ChallengeError(f"{what}: 로딩 타임아웃")
    raise ChallengeError(
        f"{what}: 로딩 시간({timeout_ms // 1000}초) 안에 끝나지 않았습니다. "
        "Chrome에서 보안 확인 체크박스를 누르거나, 빙글빙글만 돌면 F5 후 다시 실행하세요."
    )


class BrowserSession:
    """headless(persistent context) 또는 CDP(일반 Chrome) 페이지 1개를 관리합니다."""

    def __init__(self, playwright, profile_dir: Path, headless: bool, start_url: str | None = None):
        self.playwright = playwright
        self.profile_dir = profile_dir
        self.headless = headless
        self.start_url = start_url
        self.context = None
        self.browser = None
        self.chrome_proc = None
        self.cdp_port = None
        self.page = None

    def __enter__(self):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self.headless:
            # 일반 Chrome 프로필과 분리합니다. 같은 폴더를 Playwright 가 고치면
            # 설정 서명이 깨져 '프로필 오류' 대화상자가 반복됩니다.
            headless_dir = self.profile_dir.with_name(self.profile_dir.name + "_headless")
            headless_dir.mkdir(parents=True, exist_ok=True)
            options = {
                "user_data_dir": str(headless_dir),
                "headless": True,
                "args": STEALTH_ARGS,
                "user_agent": USER_AGENT,
                "locale": "ko-KR",
                "viewport": {"width": 1280, "height": 1600},
                "extra_http_headers": {"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"},
            }
            try:
                self.context = self.playwright.chromium.launch_persistent_context(channel="chrome", **options)
            except Exception:
                self.context = self.playwright.chromium.launch_persistent_context(**options)
            self.context.add_init_script(STEALTH_INIT_JS)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        else:
            print("일반 Chrome(CDP)을 띄웁니다. Playwright 자동화 창이 아닙니다.")
            print(f"프로필: {self.profile_dir} (이 프로필로 연 크롬이 있으면 모두 종료 후 진행)")
            self.chrome_proc, self.cdp_port = start_chrome_cdp(self.start_url or "about:blank", self.profile_dir)
            self.browser, self.context, self.page = connect_cdp_page(self.playwright, self.cdp_port)
        return self

    def ensure_page(self):
        """닫힌 탭 대신 남아 있는 탭을 붙잡습니다. 브라우저가 없으면 ChallengeError."""
        page = self.page
        try:
            if page is not None and not page.is_closed():
                return page
        except Exception as e:
            if not _is_closed_error(e):
                raise
        context = self.context
        if context is None:
            raise ChallengeError(
                "Chrome 창이 닫혔습니다. "
                "'프로필을 여는 동안 문제가 발생했습니다' 창이 보이면 확인을 누른 뒤 다시 실행하세요."
            )
        try:
            for candidate in list(context.pages):
                if not candidate.is_closed():
                    self.page = candidate
                    return candidate
            self.page = context.new_page()
            return self.page
        except Exception as e:
            raise ChallengeError(
                "Chrome 창이 닫혔습니다. "
                "'프로필을 여는 동안 문제가 발생했습니다' 창이 보이면 확인을 누른 뒤 다시 실행하세요."
            ) from e

    def __exit__(self, *exc):
        for closer in (self.browser, self.context):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        stop_chrome_proc(self.chrome_proc, self.cdp_port)
        # chrome.exe 는 런처 PID 가 먼저 끝나는 경우가 있어, 프로필로 남은 창을 한 번 더 닫습니다.
        if not self.headless:
            close_profile_chrome(self.profile_dir)
        return False

    def goto(self, url: str, selector: str, timeout_ms: int, what: str) -> None:
        page = self.ensure_page()
        current = ""
        try:
            current = page.url or ""
        except Exception:
            current = ""
        # 이미 목적지에 있으면 다시 열지 않습니다. 새로고침은 Cloudflare 확인을 처음부터 되돌립니다.
        if not _same_url(current, url):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                pass
            except Exception as e:
                if not _is_closed_error(e):
                    raise
                page = self.ensure_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    pass
        wait_selector_or_challenge(self, selector, timeout_ms, what)


# ---------------------------------------------------------------------
# 목록 페이지
# ---------------------------------------------------------------------
def load_all_episode_rows(page, expected: int | None) -> None:
    """회차가 많아 무한스크롤/지연 로딩되는 경우를 대비해 개수가 안정될 때까지 스크롤합니다."""
    stable = 0
    last = -1
    for _ in range(200):
        count = page.locator(EPISODE_ROW_SELECTOR).count()
        if expected and count >= expected:
            return
        if count == last:
            stable += 1
            if stable >= SCROLL_STABLE_ROUNDS:
                return
        else:
            stable = 0
            last = count
        page.mouse.wheel(0, 6000)
        page.wait_for_timeout(500)


def episode_page_url(url: str, page_no: int) -> str:
    parsed = urlparse(url)
    return parsed._replace(query=f"epage={page_no}", fragment="").geturl()


def collect_paged_episodes(session: "BrowserSession", url: str, data: dict, timeout_ms: int) -> None:
    """2페이지 이후 회차 목록(?epage=N)을 돌며 data['episodes'] 에 이어 붙입니다. href 는 절대주소로 바꿔 둡니다."""
    page = session.page
    for item in data.get("episodes") or []:
        item["href"] = urljoin(page.url, item.get("href") or "")
    last_page = to_int(page.evaluate(EPISODE_PAGER_JS)) or 1
    page_no = 2
    while page_no <= last_page:
        page_url = episode_page_url(url, page_no)
        print(f"회차 목록 {page_no}/{last_page} 페이지: {page_url}")
        session.goto(page_url, LIST_READY_SELECTOR, timeout_ms, f"회차 목록 {page_no}페이지")
        page = session.page
        try:
            page.wait_for_selector(EPISODE_ROW_SELECTOR, timeout=15000)
        except PlaywrightTimeoutError:
            print(f"  - {page_no}페이지에서 회차를 찾지 못했습니다.")
        found = (page.evaluate(EXTRACT_LIST_JS) or {}).get("episodes") or []
        for item in found:
            item["href"] = urljoin(page.url, item.get("href") or "")
        data.setdefault("episodes", []).extend(found)
        last_page = max(last_page, to_int(page.evaluate(EPISODE_PAGER_JS)) or 1)  # 페이지 창이 밀리는 경우
        page_no += 1


def build_episode_rows(page_url: str, data: dict, manhwa_id: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for item in data.get("episodes") or []:
        href = urljoin(page_url, item.get("href") or "")
        code = urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]
        if not code or code == manhwa_id:
            continue
        number = to_int(item.get("number"))
        if number is None:
            match = EPISODE_NO_RE.search(code)
            number = int(match.group(1)) if match else None
        if number is None:
            continue
        title = item.get("title") or f"{number}화"
        rows[code] = {
            "episode_code": code,
            "episode_no": number,
            "episode_title": title,
            "episode_date": parse_episode_date(item.get("date") or ""),
            "org_url": href,
            "like_cnt": to_int(item.get("like")),
            "comment_cnt": to_int(item.get("comments")),
            "rating": to_float(item.get("rating")),
        }
    return sorted(rows.values(), key=lambda row: row["episode_no"])


def build_manhwa_info(page_url: str, data: dict, manhwa_id: str, episodes: list[dict]) -> tuple[dict, list[str]]:
    stats = data.get("stats") or {}
    total = to_int(stats.get("총 회차")) or to_int(data.get("ldPages")) or len(episodes)
    # 사이트 회차 번호가 건너뛴 경우(예: 44화 없이 43→45): 목록이 마지막 번호까지 다 읽혔는데도
    # 건수가 총 회차보다 적으면 빠진 번호는 원래 없는 회차이므로 목록 건수를 총 회차로 쓴다.
    if episodes and len(episodes) < total and max(e["episode_no"] for e in episodes) >= total:
        total = len(episodes)
    rating = to_float(data.get("ldRating")) or to_float(stats.get("평점"))
    authors = data.get("authors") or ([data["ldAuthor"]] if data.get("ldAuthor") else [])
    info = {
        "manhwa_id": int(manhwa_id),
        "title": data.get("title") or f"manhwa_{manhwa_id}",
        "author": ", ".join(authors) or None,
        "total_episode": total,
        "status": data.get("status") or "",
        "weekday": data.get("weekday") or None,
        "platform": data.get("platform") or None,
        "description": data.get("description") or None,
        "rating": round(rating, 2) if rating is not None else None,
        "bookmark_cnt": to_int(stats.get("북마크")),
        "source_url": f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}/manhwa/{manhwa_id}",
    }
    categories = data.get("tags") or data.get("ldGenre") or []
    return info, categories


def _browser_fetch_bytes(page, url: str) -> bytes | None:
    """페이지 컨텍스트 fetch(캐시 우선). 스크롤로 받은 이미지 재사용에 유리합니다."""
    try:
        result = page.evaluate(BROWSER_FETCH_JS, url)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(result, dict) and result.get("ok") and result.get("b64"):
        body = base64.b64decode(result["b64"])
        if image_ext(body):
            return body
    return None


def download_bytes(page, url: str, *, prefer_cache: bool = False) -> bytes | None:
    """이미지 바이트 수신. prefer_cache=True 이면 브라우저 캐시 fetch 를 먼저 시도합니다.

    스크롤 직후 APIRequest 를 바로 쓰면 CDN 연결이 겹쳐 ECONNRESET 이 자주 납니다.
    위장 확장자(.woff/.js/.css 등)는 APIRequest 가 실제 폰트·스크립트를 받을 수 있어
    마지막에 브라우저 컨텍스트 fetch 를 한 번 더 시도합니다.
    """
    if prefer_cache:
        body = _browser_fetch_bytes(page, url)
        if body:
            return body
    for attempt in range(1, IMAGE_RETRY + 1):
        try:
            response = page.request.get(url, timeout=60000)
            if response.ok:
                body = response.body()
                if image_ext(body):
                    return body
            else:
                print(f"  - 다운로드 실패(status={response.status}) {attempt}/{IMAGE_RETRY}")
        except Exception as e:  # noqa: BLE001
            print(f"  - 다운로드 오류 {attempt}/{IMAGE_RETRY}: {e}")
    body = _browser_fetch_bytes(page, url)
    if body:
        return body
    return None


def attach_image_response_capture(page) -> tuple[dict[str, bytes], Callable[[], None]]:
    """뷰어 스크롤 중 <img> 네트워크 응답을 URL→bytes 로 모아 재다운로드를 피합니다."""
    captured: dict[str, bytes] = {}

    def on_response(response) -> None:
        try:
            if response.status != 200:
                return
            # <img> 로 로드된 리소스만(위장 확장자 .woff/.js 포함)
            if response.request.resource_type != "image":
                return
            body = response.body()
            if not image_ext(body):
                return
            captured[response.url] = body
            req_url = response.request.url
            if req_url and req_url != response.url:
                captured[req_url] = body
        except Exception:  # noqa: BLE001  개별 응답 실패는 무시(이후 재요청)
            return

    page.on("response", on_response)

    def detach() -> None:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass

    return captured, detach


def save_cover(page, cover_url: str, dest: Path) -> tuple[str | None, str | None]:
    if not cover_url or cover_url.startswith("data:"):
        return None, None
    body = download_bytes(page, cover_url)
    if not body:
        print(f"섬네일 다운로드에 실패했습니다: {cover_url}")
        return cover_url, None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    except OSError as e:  # 파일이 열려 있거나 권한 문제여도 수집은 계속
        print(f"섬네일 파일 저장에 실패했습니다(수집은 계속): {e}")
        return cover_url, (dest.as_posix() if dest.is_file() else None)
    print(f"섬네일을 저장했습니다: {dest.as_posix()}")
    return cover_url, dest.as_posix()


# ---------------------------------------------------------------------
# 회차(뷰어) 이미지 저장
# ---------------------------------------------------------------------
def scroll_viewer_to_bottom(page) -> None:
    """lazy 이미지가 멈춘 스크롤 때문에 늦게 받아지지 않도록 최하단까지 자동 스크롤 후 로딩을 잠깐 기다립니다."""
    try:
        elapsed = page.evaluate(
            VIEWER_SCROLL_JS,
            [VIEWER_SCROLL_MS, VIEWER_SCROLL_MAX_MS, VIEWER_SCROLL_STEP_MS, VIEWER_SCROLL_MAX_JUMP],
        )
        page.wait_for_function(VIEWER_LOADED_JS, timeout=VIEWER_LOAD_WAIT_MS)
        print(f"  - 자동 스크롤 완료({elapsed / 1000:.1f}초), 이미지 로딩 완료")
    except PlaywrightTimeoutError:
        # 전부 complete 전에도 스크롤 중 수신분은 이미 캐시됨 → 짧게 끊고 저장 진행
        print(f"  - 자동 스크롤 완료, 로딩 대기 {VIEWER_LOAD_WAIT_MS / 1000:.1f}초 초과 → 수신분·캐시로 저장")
    except Exception as e:  # noqa: BLE001  스크롤 실패해도 다운로드는 진행
        if _is_closed_error(e):
            raise
        print(f"  - 자동 스크롤 실패(다운로드는 진행): {e}")


# 뷰어 DOM 이미지 중 실제 로드된 것만 골라 저장용 URL 목록을 만든다.
# (사이트 측에서 일부 img 요청이 영원히 pending 인 경우가 있어, 미로드 장은 image_cnt 에서 제외)
EXTRACT_VIEWER_LOADED_JS = """() => Array.from(document.querySelectorAll("div.vw-imgs img.viewer-ratio-img")).map((img) => ({
    src: img.currentSrc || img.getAttribute("src") || img.getAttribute("data-src") || "",
    alt: img.getAttribute("alt") || "",
    complete: !!img.complete,
    naturalWidth: img.naturalWidth || 0,
    naturalHeight: img.naturalHeight || 0,
}))"""

FORCE_LOAD_IMG_JS = """async (urls) => {
    // 동시 요청이 많으면 CDN 이 pending 으로 남는 경우가 있어 순차 로드
    const loadOne = (url) => new Promise((resolve) => {
        const img = new Image();
        let settled = false;
        const done = (ok) => {
            if (settled) return;
            settled = true;
            resolve(ok);
        };
        img.onload = () => done(img.naturalWidth > 0);
        img.onerror = () => done(false);
        img.src = url;
        setTimeout(() => done(img.complete && img.naturalWidth > 0), 4000);
    });
    let ok = 0;
    for (const url of urls) {
        if (await loadOne(url)) ok += 1;
    }
    return ok;
}"""

# 이미 뷰어에 그려진 img 를 canvas 로 뽑아 네트워크 재요청 없이 저장 (CORS taint 시 null)
CANVAS_EXPORT_JS = """async (src) => {
    const img = Array.from(document.querySelectorAll("div.vw-imgs img.viewer-ratio-img"))
        .find((el) => (el.currentSrc || el.src) === src || (el.getAttribute("src") || "") === src);
    if (!img || !img.naturalWidth) return null;
    try {
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0);
        const dataUrl = canvas.toDataURL("image/jpeg", 0.92);
        const m = /^data:image\\/\\w+;base64,(.+)$/.exec(dataUrl);
        return m ? m[1] : null;
    } catch (e) {
        return null;
    }
}"""


def force_capture_missing_images(page, urls: list[str], captured: dict[str, bytes]) -> int:
    """스크롤로 못 받은 URL을 <img> 로 순차 강제 로드해 response 캡처를 보충합니다.

    위장 확장자 URL은 APIRequest 가 JPEG가 아닌 실제 .js/.woff 를 받을 수 있어
    브라우저 이미지 로더 경로가 더 안정적입니다.
    """
    missing = [url for url in urls if url not in captured]
    if not missing:
        return 0
    try:
        ok = page.evaluate(FORCE_LOAD_IMG_JS, missing)
        page.wait_for_timeout(300)
        print(f"  - 미수신 {len(missing)}장 순차 강제 로드 시도 (onload {ok}장)")
    except Exception as e:  # noqa: BLE001
        if _is_closed_error(e):
            raise
        print(f"  - 미수신 이미지 강제 로드 실패(다운로드는 진행): {e}")
        return 0
    filled = sum(1 for url in missing if url in captured)
    if filled:
        print(f"  - 강제 로드로 캡처 추가 {filled}장")
    return filled


def canvas_export_image(page, url: str) -> bytes | None:
    """뷰어에 이미 로드된 이미지를 canvas→JPEG 로 추출합니다."""
    try:
        b64 = page.evaluate(CANVAS_EXPORT_JS, url)
    except Exception:  # noqa: BLE001
        return None
    if not b64:
        return None
    body = base64.b64decode(b64)
    return body if image_ext(body) else None


def collect_episode_images(
    session: BrowserSession,
    manhwa_id: str,
    episode: dict,
    base_path: Path,
    timeout_ms: int,
) -> tuple[int, list[dict]]:
    """회차 뷰어를 열어 이미지를 <base>/{manhwa_id}/{episode_no}/NNN.ext 로 저장합니다."""
    page = session.page
    # goto·스크롤로 브라우저가 받는 응답을 가로채 재다운로드(APIRequest) 부담·ECONNRESET 을 줄임
    captured, detach_capture = attach_image_response_capture(page)
    try:
        session.goto(episode["org_url"], VIEWER_IMG_SELECTOR, timeout_ms, f"{episode['episode_no']}화 뷰어")
        page.wait_for_timeout(800)
        scroll_viewer_to_bottom(page)
        items = page.evaluate(EXTRACT_VIEWER_JS) or []
        urls = [
            urljoin(page.url, item["src"])
            for item in items
            if item.get("src") and not item["src"].startswith("data:")
        ]
        decoys = [url for url in urls if is_decoy_image_url(url)]
        if decoys:
            print(f"  - 가짜 이미지 {len(decoys)}장 제외(svg·placeholder)")
            urls = [url for url in urls if not is_decoy_image_url(url)]
        if not urls:
            print("  - 뷰어 이미지 목록이 비어 있습니다.")
            return 0, []

        force_capture_missing_images(page, urls, captured)

        # 사이트에서 일부 img 가 영원히 pending(complete=false)인 경우가 있음 → 로드된 장만 수집 대상으로
        loaded_items = page.evaluate(EXTRACT_VIEWER_LOADED_JS) or []
        loaded_urls = []
        pending = 0
        for item in loaded_items:
            src = item.get("src") or ""
            if not src or src.startswith("data:"):
                continue
            full = urljoin(page.url, src)
            if is_decoy_image_url(full):
                continue
            if int(item.get("naturalWidth") or 0) > 0:
                loaded_urls.append(full)
            else:
                pending += 1
        if pending:
            print(f"  - 뷰어 미로드(pending/실패) {pending}장 → image_cnt 에서 제외 (로드 {len(loaded_urls)}장)")
        if loaded_urls:
            urls = loaded_urls
        else:
            print("  - 로드된 뷰어 이미지가 없어 전체 URL 목록으로 저장을 시도합니다.")

        target_dir = episode_dir(base_path, manhwa_id, episode["episode_no"])
        target_dir.mkdir(parents=True, exist_ok=True)
        width = max(3, len(str(len(urls))))
        saved: list[dict] = []
        cache_hits = 0
        refetched = 0
        canvas_hits = 0
        for page_no, url in enumerate(urls, start=1):
            body = captured.get(url)
            if body:
                cache_hits += 1
            else:
                body = canvas_export_image(page, url)
                if body:
                    canvas_hits += 1
                else:
                    body = download_bytes(page, url, prefer_cache=True)
                    if body:
                        refetched += 1
            if not body:
                print(f"  - [{page_no}/{len(urls)}] 저장 실패: {url}")
                continue
            dest = target_dir / f"{page_no:0{width}d}.{image_ext(body)}"
            dest.write_bytes(body)
            saved.append(
                {
                    "page_no": page_no,
                    "org_url": url,
                    "file_path": dest.as_posix(),
                    "file_size": len(body),
                }
            )
        print(
            f"  - 저장 경로: 스크롤수신 {cache_hits}장 / canvas {canvas_hits}장 / "
            f"재요청 {refetched}장 / 실패 {len(urls) - len(saved)}장"
        )
        return len(urls), saved
    finally:
        detach_capture()


def select_target_episodes(
    episodes: list[dict],
    status: dict[str, dict],
    base_path: Path,
    manhwa_id: str,
    only: set[int] | None,
    num: int | None,
    force: bool,
    only_code: str | None = None,
) -> list[dict]:
    targets = []
    for ep in episodes:
        if only is not None and ep["episode_no"] not in only:
            continue
        if only_code is not None and ep["episode_code"] != only_code:
            continue
        row = status.get(ep["episode_code"]) or {}
        done = row.get("has_images") and episode_dir(base_path, manhwa_id, ep["episode_no"]).is_dir()
        if done and not force:
            continue
        targets.append(ep)
    return targets[:num] if num else targets


def run_session(session: BrowserSession, args, manhwa_id: str, base_path: Path) -> dict:
    timeout_ms = int(args.timeout * 1000)
    session.goto(args.url, LIST_READY_SELECTOR, timeout_ms, "만화 목록")
    page = session.page
    try:
        page.wait_for_selector(EPISODE_ROW_SELECTOR, timeout=15000)
    except PlaywrightTimeoutError:
        print("회차 목록(li.ep-row-v2)을 찾지 못했습니다. 작품 정보만 저장합니다.")
    data = page.evaluate(EXTRACT_LIST_JS) or {}
    expected = to_int((data.get("stats") or {}).get("총 회차"))
    if expected and len(data.get("episodes") or []) < expected:
        load_all_episode_rows(page, expected)
        data = page.evaluate(EXTRACT_LIST_JS) or {}
    list_url = page.url
    collect_paged_episodes(session, list_url, data, timeout_ms)
    page = session.page

    episodes = build_episode_rows(list_url, data, manhwa_id)
    info, categories = build_manhwa_info(list_url, data, manhwa_id, episodes)
    print(f"[{manhwa_id}] {info['title']} / {info.get('author')} / {info['status'] or '-'} / "
          f"{info.get('platform') or '-'} / 총 {info['total_episode']}화 (목록 {len(episodes)}건)")
    if info["total_episode"] and len(episodes) < info["total_episode"]:
        print(f"주의: 목록에서 읽은 회차({len(episodes)})가 총 회차({info['total_episode']})보다 적습니다.")

    cover_url, cover_path = save_cover(page, data.get("coverUrl") or "", cover_file_path(base_path, manhwa_id))
    info["cover_url"] = cover_url
    info["cover_path"] = cover_path
    write_info_file(base_path, manhwa_id, info, categories, episodes)
    save_manhwa_basic(info, categories, episodes)
    print("DB 갱신: manhwa_info / manhwa_category / manhwa_episode / manhwa_work_list")

    if args.createonly:
        print("--createonly Y: 회차 이미지 수집은 생략합니다.")
        return {"targets": 0, "ok": 0, "fail": 0}

    status = fetch_episode_status(int(manhwa_id))
    targets = select_target_episodes(
        episodes, status, base_path, manhwa_id, parse_episode_spec(args.episode), args.num, args.force,
        args.episode_code,
    )
    if args.episode_code and not any(ep["episode_code"] == args.episode_code for ep in episodes):
        print(f"주의: 회차 코드 {args.episode_code} 를 목록에서 찾지 못했습니다.")
    print(f"이미지 수집 대상: {len(targets)}회차 (저장 경로: {(base_path / manhwa_id).as_posix()}/<회차>)")
    ok = fail = 0
    for index, ep in enumerate(targets, start=1):
        print(f"[{index}/{len(targets)}] {ep['episode_no']}화 {ep['episode_title']} - {ep['org_url']}")
        try:
            image_cnt, saved = collect_episode_images(session, manhwa_id, ep, base_path, timeout_ms)
        except ChallengeError:
            raise
        except PlaywrightTimeoutError as e:
            print(f"  - 실패: {e}")
            fail += 1
            continue
        image_dir = episode_dir(base_path, manhwa_id, ep["episode_no"]).as_posix()
        save_episode_images(int(manhwa_id), ep["episode_code"], image_dir, image_cnt, saved)
        if image_cnt and len(saved) >= image_cnt:
            ok += 1
            print(f"  - 저장 완료: {len(saved)}장 → {image_dir}")
        else:
            fail += 1
            print(f"  - 일부 실패: {len(saved)}/{image_cnt}장 → {image_dir}")
        if index < len(targets):
            time.sleep(args.wait)
    return {"targets": len(targets), "ok": ok, "fail": fail}


def close_toki_browsers(profile_dir: Path) -> int:
    """profile_dir(.toki_browser_manhwa)을 쓰는 Chrome 프로세스를 종료하고 종료한 개수를 반환합니다.

    book_web_Crawler/db_book_step_type_1.py 와 동일. 다른 프로필의 일반 Chrome 은 건드리지 않습니다.
    """
    closed = close_profile_chrome(profile_dir)
    if closed is None:
        print("열린 브라우저 확인 실패, 종료 생략")
        return 0
    clear_cdp_port(profile_dir)
    if closed:
        time.sleep(1)  # 프로필 잠금 해제 대기
    return closed


def run(args) -> int:
    try:
        load_mysql_config()  # 브라우저/보안확인 전에 DB 설정부터 확인
    except (FileNotFoundError, ValueError) as e:
        print(f"{e}\n→ book_web_Crawler\\config.ini 를 {config_path().parent} 에 복사한 뒤 다시 실행하세요.")
        return 2
    manhwa_id = manhwa_id_from_url(args.url)
    base_path = resolve_base_path(args.output)
    profile_dir = Path(args.profile) if args.profile else DEFAULT_USER_DATA_DIR
    closed = close_toki_browsers(profile_dir)  # 목록 브라우저 체크 이전에 기존 창 정리
    if closed:
        print(f"열려 있던 .toki_browser_manhwa Chrome {closed}개 종료: {profile_dir}")
    result = None
    with sync_playwright() as playwright:
        if not args.headed:
            try:
                print(f"1차 접근(.toki_browser_manhwa headless): {profile_dir}")
                with BrowserSession(playwright, profile_dir, headless=True) as session:
                    result = run_session(session, args, manhwa_id, base_path)
            except ChallengeError as e:
                print(f"headless 접근이 정상적이지 않습니다: {e}")
                print("일반 Chrome 창을 열어 보안 확인 후 이어서 진행합니다(저장된 회차는 건너뜀).")
        if result is None:
            try:
                with BrowserSession(playwright, profile_dir, headless=False, start_url=args.url) as session:
                    result = run_session(session, args, manhwa_id, base_path)
            except ChallengeError as e:
                print(e)
                return 1

    status = fetch_work_list_status(int(manhwa_id)) or {}
    print(
        f"완료: 대상 {result['targets']}회차 / 성공 {result['ok']} / 실패 {result['fail']} | "
        f"manhwa_work_list collect_yn={status.get('collect_yn')} "
        f"({status.get('collect_episode_cnt')}/{status.get('total_episode')}회차, "
        f"이미지 {status.get('collect_image_cnt')}장)"
    )
    return 0 if result["fail"] == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="만화 1건 정보/회차 이미지 수집 (toki manhwa)")
    parser.add_argument(
        "url", nargs="?", default=DEFAULT_URL,
        help="만화 목록 URL (예: https://toki35.com/manhwa/12751) 또는 회차 URL(…/manhwa/12751/132716: 그 회차만 수집)",
    )
    parser.add_argument("-o", "--output", default=str(DEFAULT_BASE_PATH), help="저장 루트 경로 (기본 D:/bookmanhwa)")
    parser.add_argument("--createonly", default="N", help="Y: 작품정보/회차목록/섬네일/DB까지만, 이미지 수집 생략")
    parser.add_argument("--num", type=int, default=None, help="이번 실행에서 수집할 최대 회차 수 (미지정: 전체)")
    parser.add_argument("--episode", default=None, help="특정 회차 번호만 수집 (예: 1,3,5-10)")
    parser.add_argument("--force", action="store_true", help="이미 수집된 회차도 다시 저장")
    parser.add_argument("--headed", action="store_true", help="처음부터 일반 Chrome(CDP) 창으로 진행")
    parser.add_argument("--profile", default=None, help=f"브라우저 프로필 경로 (기본 {DEFAULT_USER_DATA_DIR})")
    parser.add_argument("--timeout", type=float, default=90, help="페이지 로딩 대기(초)")
    parser.add_argument("--wait", type=float, default=EPISODE_WAIT_SEC, help="회차 간 대기(초)")
    args = parser.parse_args()
    args.createonly = str(args.createonly).strip().upper() == "Y"
    args.url, args.episode_code = split_episode_url(args.url)
    if args.episode_code:
        print(f"회차 URL → 목록 {args.url} 기준으로 회차 {args.episode_code} 만 수집합니다.")
    return args


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run(parse_args()))
