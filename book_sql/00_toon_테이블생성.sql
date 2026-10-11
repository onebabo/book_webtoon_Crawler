-- =====================================================================
-- book_webtoon_Crawler : 웹툰(toon) 전용 테이블 생성
--   소설(book_info / book_work_list / book_info_chapter ...)과 완전히 분리된 테이블 세트
--   기준 샘플 : https://toki34.com/webtoon/847903  (회차 URL: /webtoon/847903/nv-847903-1)
--   이미지 저장 : D:\booktoon\{toon_id}\{episode_no}\001.jpg ...
--   섬네일 저장 : D:\booktoon\cover\{toon_id}.jpg
--
--   ※ db.py 의 ensure_schema() 가 실행 시 이 파일을 그대로 읽어 실행합니다(DDL 단일 관리).
--     '--' 주석 줄을 제거한 뒤 ';' 기준으로 나누므로, 구문 안에 ';' 문자를 넣지 마세요.
-- =====================================================================

-- 1. 웹툰 기본 정보 (실제로 수집을 진행/완료한 웹툰)
CREATE TABLE IF NOT EXISTS toon_info (
    toon_id         BIGINT          NOT NULL COMMENT '웹툰 아이디 (사이트 원본 ID, 예: 847903)',
    title           VARCHAR(255)    NOT NULL COMMENT '제목',
    author          VARCHAR(255)    NULL COMMENT '작가 (여러 명이면 쉼표 구분)',
    total_episode   INT             NOT NULL DEFAULT 0 COMMENT '총 회차 수 (사이트 기준)',
    status          ENUM('연재','완결','중단') NOT NULL DEFAULT '연재' COMMENT '연재 상태',
    weekday         VARCHAR(20)     NULL COMMENT '연재 요일 (예: 월요일)',
    platform        VARCHAR(100)    NULL COMMENT '원 연재 플랫폼 (예: 네이버)',
    description     TEXT            NULL COMMENT '소개',
    rating          DECIMAL(5,2)    NULL COMMENT '평점 (10점 만점)',
    bookmark_cnt    INT             NULL COMMENT '북마크 수',
    source_url      VARCHAR(500)    NULL COMMENT '원본 URL (https://toki34.com/webtoon/{toon_id})',
    cover_url       VARCHAR(500)    NULL COMMENT '원본 섬네일 URL',
    cover_path      VARCHAR(500)    NULL COMMENT '로컬 섬네일 경로',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (toon_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='웹툰 기본 정보';

-- 2. 웹툰 태그/카테고리 (toon_info 1:N)
CREATE TABLE IF NOT EXISTS toon_category (
    category_seq    BIGINT          NOT NULL AUTO_INCREMENT COMMENT '내부 PK',
    toon_id         BIGINT          NOT NULL COMMENT '웹툰 아이디 (toon_info.toon_id)',
    category_name   VARCHAR(50)     NOT NULL COMMENT '태그/카테고리 이름 (예: 완결웹툰)',
    use_yn          CHAR(1)         NOT NULL DEFAULT 'Y' COMMENT '사용 여부 Y/N',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (category_seq),
    UNIQUE KEY uk_toon_category (toon_id, category_name),
    KEY idx_toon_id (toon_id),
    CONSTRAINT fk_toon_category_toon FOREIGN KEY (toon_id)
        REFERENCES toon_info (toon_id) ON DELETE CASCADE,
    CONSTRAINT chk_toon_category_use_yn CHECK (use_yn IN ('Y','N'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='웹툰 카테고리 정보';

-- 3. 웹툰 수집 대상 마스터 목록 (book_work_list 대응) - 수집 진행 상태 관리
CREATE TABLE IF NOT EXISTS toon_work_list (
    toon_id             BIGINT          NOT NULL COMMENT '웹툰 아이디 (사이트 원본 ID)',
    title               VARCHAR(255)    NOT NULL COMMENT '제목',
    author              VARCHAR(255)    NULL COMMENT '작가',
    total_episode       INT             NOT NULL DEFAULT 0 COMMENT '총 회차 수 (사이트 기준)',
    status              ENUM('연재','완결','중단') NOT NULL DEFAULT '연재' COMMENT '연재 상태',
    weekday             VARCHAR(20)     NULL COMMENT '연재 요일',
    platform            VARCHAR(100)    NULL COMMENT '원 연재 플랫폼',
    source_url          VARCHAR(500)    NULL COMMENT '원본 URL',
    cover_url           VARCHAR(500)    NULL COMMENT '원본 섬네일 URL',
    cover_path          VARCHAR(500)    NULL COMMENT '로컬 섬네일 경로',
    category_names      VARCHAR(500)    NULL COMMENT '태그/카테고리 목록 (쉼표 구분)',
    last_episode_no     INT             NULL COMMENT '최신 회차 번호',
    last_episode_title  VARCHAR(255)    NULL COMMENT '최신 회차 제목',
    last_update_date    DATE            NULL COMMENT '최신 회차 갱신일',
    sort_no             INT             NOT NULL DEFAULT 0 COMMENT '목록 노출 순번',
    used                CHAR(1)         NOT NULL DEFAULT 'Y' COMMENT '사용 여부 (Y:사용, N:사용중지)',
    collect_yn          CHAR(1)         NOT NULL DEFAULT 'N' COMMENT '수집 완료 여부 (N:미완료, Y:완료, I:진행중)',
    collect_episode_cnt INT             NOT NULL DEFAULT 0 COMMENT '이미지 수집 완료 회차 수',
    collect_image_cnt   INT             NOT NULL DEFAULT 0 COMMENT '저장된 이미지 총 수',
    collect_started_at  DATETIME        NULL COMMENT '수집 시작 일시',
    collect_ended_at    DATETIME        NULL COMMENT '수집 완료 일시',
    remark              VARCHAR(500)    NULL COMMENT '비고 / 실패 사유',
    collect_ip          VARCHAR(45)     NULL COMMENT '최종 수집 작업 PC IP',
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (toon_id),
    KEY idx_toon_collect_yn (collect_yn),
    KEY idx_toon_sort_no (sort_no),
    KEY idx_toon_used (used),
    CONSTRAINT chk_toon_work_collect_yn CHECK (collect_yn IN ('N','Y','I')),
    CONSTRAINT chk_toon_work_used CHECK (used IN ('Y','N'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='웹툰 수집 대상 목록';

-- 4. 웹툰 회차 (book_info_chapter 대응)
--    episode_code : 사이트 회차 코드 (예: nv-847903-33  → 플랫폼접두어-원본ID-회차)
--    has_images   : 이미지 수(image_cnt) 만큼 모두 저장되었으면 1
CREATE TABLE IF NOT EXISTS toon_episode (
    episode_seq     BIGINT          NOT NULL AUTO_INCREMENT COMMENT '내부 PK',
    toon_id         BIGINT          NOT NULL COMMENT '웹툰 아이디 (toon_info.toon_id)',
    episode_code    VARCHAR(100)    NOT NULL COMMENT '사이트 회차 코드 (예: nv-847903-33)',
    episode_no      INT             NOT NULL COMMENT '회차 번호 (저장 폴더명)',
    episode_type    VARCHAR(10)     NOT NULL DEFAULT 'MAIN' COMMENT '회차 구분 (MAIN:본편, SIDE:외전/후기 등)',
    episode_title   VARCHAR(255)    NULL COMMENT '회차 제목',
    episode_date    DATE            NULL COMMENT '회차 등록일',
    org_url         VARCHAR(500)    NULL COMMENT '원본 회차 URL',
    like_cnt        INT             NULL COMMENT '추천 수',
    comment_cnt     INT             NULL COMMENT '댓글 수',
    rating          DECIMAL(4,2)    NULL COMMENT '회차 평점',
    image_dir       VARCHAR(500)    NULL COMMENT '로컬 이미지 폴더 (D:/booktoon/{toon_id}/{episode_no})',
    image_cnt       INT             NOT NULL DEFAULT 0 COMMENT '뷰어 기준 이미지 수',
    saved_image_cnt INT             NOT NULL DEFAULT 0 COMMENT '로컬 저장 성공 이미지 수',
    has_images      TINYINT         GENERATED ALWAYS AS (
                        CASE WHEN image_cnt > 0 AND saved_image_cnt >= image_cnt THEN 1 ELSE 0 END
                    ) STORED COMMENT '이미지 수집 완료 여부 (1:완료)',
    collected_at    DATETIME        NULL COMMENT '이미지 수집 일시',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (episode_seq),
    UNIQUE KEY uk_toon_episode_code (episode_code),
    KEY idx_toon_episode_no (toon_id, episode_no),
    KEY idx_toon_has_images (toon_id, has_images)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='웹툰 회차 정보';

-- 5. 웹툰 회차 이미지 (회차 1:N)
--    org_url 은 사이트가 매번 새로 발급하는 위장 URL(.json/.woff2/.js 확장자, 실제는 JPEG)이라 참고용입니다.
CREATE TABLE IF NOT EXISTS toon_episode_image (
    image_seq       BIGINT          NOT NULL AUTO_INCREMENT COMMENT '내부 PK',
    toon_id         BIGINT          NOT NULL COMMENT '웹툰 아이디',
    episode_code    VARCHAR(100)    NOT NULL COMMENT '회차 코드 (toon_episode.episode_code)',
    page_no         INT             NOT NULL COMMENT '이미지 순번 (1부터)',
    org_url         VARCHAR(1000)   NULL COMMENT '원본 이미지 URL (수집 시점)',
    file_path       VARCHAR(500)    NULL COMMENT '로컬 저장 경로',
    file_size       INT             NULL COMMENT '파일 크기 (byte)',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (image_seq),
    UNIQUE KEY uk_toon_episode_page (episode_code, page_no),
    KEY idx_toon_image_toon (toon_id),
    CONSTRAINT fk_toon_image_episode FOREIGN KEY (episode_code)
        REFERENCES toon_episode (episode_code) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='웹툰 회차 이미지 정보';

-- 6. 웹툰 수집 보류 (book_edit_holds 대응)
CREATE TABLE IF NOT EXISTS toon_edit_holds (
    toon_id             BIGINT          NOT NULL COMMENT '웹툰 아이디',
    title               VARCHAR(255)    NOT NULL COMMENT '웹툰 제목',
    total_episode       INT             NOT NULL DEFAULT 0 COMMENT '총 회차수',
    collect_episode_cnt INT             NOT NULL DEFAULT 0 COMMENT '수집완료 회차수',
    remain_episode      INT             GENERATED ALWAYS AS (total_episode - collect_episode_cnt) STORED
                                        COMMENT '미수집 회차수 (총 회차수 - 수집완료 회차수)',
    hold_state          CHAR(1)         NOT NULL DEFAULT 'H' COMMENT '보류상태 (H:보류중, D:보류해제)',
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (toon_id),
    KEY idx_toon_hold_state (hold_state),
    CONSTRAINT chk_toon_hold_state CHECK (hold_state IN ('H','D'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='웹툰 보류 정보';
