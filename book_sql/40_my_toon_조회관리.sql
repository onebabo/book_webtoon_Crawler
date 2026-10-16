-- =====================================================================
-- book_webtoon_Crawler : 마이 북(my_toon) 테이블 조회/관리 SQL (수동 실행용)
--   등록 : python toon_create.py --TYPE 1|2 --name "..." --path "..."  (01_Claude_job/MYBOOK_CREATE.md)
-- =====================================================================

SET @my_toon_id = 20261016222916;

-- 1. 등록된 마이 북 목록 (회차/페이지 집계 + 복사 완료 여부)
SELECT i.my_toon_id, i.title, i.src_type, i.status,
       i.total_episode,
       COUNT(e.episode_seq)                                   AS episode_cnt,
       COALESCE(SUM(e.has_images), 0)                         AS done_episode_cnt,
       COUNT(e.episode_seq) - COALESCE(SUM(e.has_images), 0)  AS fail_episode_cnt,
       COALESCE(SUM(e.image_cnt), 0)                          AS page_cnt,
       COALESCE(SUM(e.saved_image_cnt), 0)                    AS saved_page_cnt,
       i.total_image_cnt,
       i.src_path, i.image_root, i.created_at, i.updated_at
  FROM my_toon_info i
  LEFT JOIN my_toon_episode e ON e.my_toon_id = i.my_toon_id
 GROUP BY i.my_toon_id
 ORDER BY i.title, i.my_toon_id;

-- 2. 마이 북 1건 기본정보
SELECT * FROM my_toon_info WHERE my_toon_id = @my_toon_id;

-- 3. 회차별 현황
SELECT episode_no, episode_code, episode_type, episode_title,
       image_cnt, saved_image_cnt, has_images, src_dir, image_dir, collected_at
  FROM my_toon_episode
 WHERE my_toon_id = @my_toon_id
 ORDER BY episode_no;

-- 4. 복사 미완료 / 일부실패 회차 (전체 마이 북, 재실행 대상)
SELECT i.my_toon_id, i.title, e.episode_no, e.episode_title, e.image_cnt, e.saved_image_cnt, e.src_dir
  FROM my_toon_episode e
  JOIN my_toon_info i ON i.my_toon_id = e.my_toon_id
 WHERE e.has_images = 0
 ORDER BY i.my_toon_id, e.episode_no;

-- 5. 회차 이미지 목록 (src_file 끝 #L / #R = TYPE 2 펼침면의 왼쪽/오른쪽)
SELECT e.episode_no, m.page_no, m.src_file, m.file_path, m.file_size
  FROM my_toon_episode_image m
  JOIN my_toon_episode e ON e.episode_code = m.episode_code
 WHERE m.my_toon_id = @my_toon_id
 ORDER BY e.episode_no, m.page_no;

-- 6. 원본 폴더로 이미 등록됐는지 확인 (같은 폴더 중복 등록 방지)
-- SELECT my_toon_id, title, src_type, created_at FROM my_toon_info WHERE src_path = 'D:/mynas/도시정벌 1부(01~12)';

-- 7. 작가 / 상태 / 소개 수동 입력 (toon_create.py 는 이 값을 건드리지 않음)
-- UPDATE my_toon_info SET author = '신형빈, Bin프로', status = '완결', description = NULL WHERE my_toon_id = @my_toon_id;

-- 8. 마이 북 삭제 (DB만 삭제, 로컬 이미지 D:\booktoon\{my_toon_id} 는 직접 삭제)
--    my_toon_episode 는 my_toon_info FK CASCADE, my_toon_episode_image 는 my_toon_episode FK CASCADE 로 함께 삭제
-- DELETE FROM my_toon_info WHERE my_toon_id = @my_toon_id;

-- 9. 테이블 전체 제거 (초기화 시에만, 순서 주의)
-- DROP TABLE IF EXISTS my_toon_episode_image;
-- DROP TABLE IF EXISTS my_toon_episode;
-- DROP TABLE IF EXISTS my_toon_info;
