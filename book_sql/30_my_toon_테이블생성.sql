-- =====================================================================
-- book_webtoon_Crawler : 마이 북(my_toon) 전용 테이블 생성
--   현재 PC 에 보관 중인 웹툰 폴더/이미지를 분석해서 DB 화 (toon_create.py)
--   toon_info / toon_episode / toon_episode_image 구조를 따르되, 사이트 수집용 컬럼은 빼고
--   원본 폴더 정보(src_*) 를 추가했습니다.
--
--   my_toon_id : 신규 생성 시 현재 일시 YYYYMMDDHHMMSS (14자리 숫자 → BIGINT 범위 안, 예: 20261016093015)
--   이미지 저장 : D:\booktoon\{my_toon_id}\{episode_no}\001.jpg ...
--
--   ※ toon_create.py 가 실행 시 이 파일을 그대로 읽어 실행합니다(DDL 단일 관리).
--     '--' 주석 줄을 제거한 뒤 ';' 기준으로 나누므로, 구문 안에 ';' 문자를 넣지 마세요.
-- =====================================================================

-- 1. 마이 북 기본 정보 (toon_info 대응)
CREATE TABLE IF NOT EXISTS my_toon_info (
    my_toon_id      BIGINT          NOT NULL COMMENT '마이 북 아이디 (신규 생성 시 YYYYMMDDHHMMSS)',
    title           VARCHAR(255)    NOT NULL COMMENT '제목 (--name)',
    author          VARCHAR(255)    NULL COMMENT '작가 (여러 명이면 쉼표 구분)',
    total_episode   INT             NOT NULL DEFAULT 0 COMMENT '총 회차 수 (원본 회차 폴더 수)',
    status          ENUM('연재','완결','중단') NOT NULL DEFAULT '연재' COMMENT '연재 상태',
    platform        VARCHAR(100)    NULL COMMENT '원 연재 플랫폼',
    description     TEXT            NULL COMMENT '소개',
    cover_path      VARCHAR(500)    NULL COMMENT '로컬 섬네일 경로',
    src_type        INT             NOT NULL COMMENT '원본 폴더 구조 TYPE (MYBOOK_CREATE.md 참고)',
    src_path        VARCHAR(500)    NOT NULL COMMENT '원본 폴더 경로 (--path)',
    image_root      VARCHAR(500)    NULL COMMENT '복사 대상 루트 (D:/booktoon/{my_toon_id})',
    total_image_cnt INT             NOT NULL DEFAULT 0 COMMENT '복사된 이미지 총 수',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (my_toon_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='마이 북 기본 정보';

-- 2. 마이 북 회차 (toon_episode 대응)
--    episode_code : my-{my_toon_id}-{episode_no}
CREATE TABLE IF NOT EXISTS my_toon_episode (
    episode_seq     BIGINT          NOT NULL AUTO_INCREMENT COMMENT '내부 PK',
    my_toon_id      BIGINT          NOT NULL COMMENT '마이 북 아이디 (my_toon_info.my_toon_id)',
    episode_code    VARCHAR(100)    NOT NULL COMMENT '회차 코드 (my-{my_toon_id}-{episode_no})',
    episode_no      INT             NOT NULL COMMENT '회차 번호 (저장 폴더명)',
    episode_type    VARCHAR(10)     NOT NULL DEFAULT 'MAIN' COMMENT '회차 구분 (MAIN:본편, SIDE:외전/후기 등)',
    episode_title   VARCHAR(255)    NULL COMMENT '회차 제목 (원본 폴더명)',
    src_dir         VARCHAR(500)    NULL COMMENT '원본 회차 폴더',
    image_dir       VARCHAR(500)    NULL COMMENT '로컬 이미지 폴더 (D:/booktoon/{my_toon_id}/{episode_no})',
    image_cnt       INT             NOT NULL DEFAULT 0 COMMENT '원본 이미지 수',
    saved_image_cnt INT             NOT NULL DEFAULT 0 COMMENT '복사 성공 이미지 수',
    has_images      TINYINT         GENERATED ALWAYS AS (
                        CASE WHEN image_cnt > 0 AND saved_image_cnt >= image_cnt THEN 1 ELSE 0 END
                    ) STORED COMMENT '이미지 복사 완료 여부 (1:완료)',
    collected_at    DATETIME        NULL COMMENT '이미지 복사 일시',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (episode_seq),
    UNIQUE KEY uk_my_toon_episode_code (episode_code),
    UNIQUE KEY uk_my_toon_episode_no (my_toon_id, episode_no),
    KEY idx_my_toon_has_images (my_toon_id, has_images),
    CONSTRAINT fk_my_toon_episode_info FOREIGN KEY (my_toon_id)
        REFERENCES my_toon_info (my_toon_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='마이 북 회차 정보';

-- 3. 마이 북 회차 이미지 (toon_episode_image 대응, 회차 1:N)
--    page_no 는 원본 파일명 숫자 순서대로 1부터 다시 매깁니다(원본 번호 빠짐/000 시작 보정).
CREATE TABLE IF NOT EXISTS my_toon_episode_image (
    image_seq       BIGINT          NOT NULL AUTO_INCREMENT COMMENT '내부 PK',
    my_toon_id      BIGINT          NOT NULL COMMENT '마이 북 아이디',
    episode_code    VARCHAR(100)    NOT NULL COMMENT '회차 코드 (my_toon_episode.episode_code)',
    page_no         INT             NOT NULL COMMENT '이미지 순번 (1부터)',
    src_file        VARCHAR(500)    NULL COMMENT '원본 파일 경로',
    file_path       VARCHAR(500)    NULL COMMENT '로컬 저장 경로',
    file_size       INT             NULL COMMENT '파일 크기 (byte)',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (image_seq),
    UNIQUE KEY uk_my_toon_episode_page (episode_code, page_no),
    KEY idx_my_toon_image_toon (my_toon_id),
    CONSTRAINT fk_my_toon_image_episode FOREIGN KEY (episode_code)
        REFERENCES my_toon_episode (episode_code) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='마이 북 회차 이미지 정보';
