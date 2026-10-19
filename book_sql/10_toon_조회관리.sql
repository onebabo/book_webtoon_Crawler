-- =====================================================================
-- book_webtoon_Crawler : 웹툰(toon) 테이블 조회/관리 SQL (수동 실행용)
-- =====================================================================

SET @toon_id = '847903';  -- 문자형 (예: 'u-bt-ok_adultery-8ddc2430')

-- 1. 웹툰 1건 기본정보 / 수집현황
SELECT * FROM toon_info      WHERE toon_id = @toon_id;
SELECT * FROM toon_work_list WHERE toon_id = @toon_id;
SELECT * FROM toon_category  WHERE toon_id = @toon_id;

-- 2. 회차별 이미지 수집 현황
SELECT episode_no, episode_code, episode_title, episode_date,
       image_cnt, saved_image_cnt, has_images, image_dir, collected_at
  FROM toon_episode
 WHERE toon_id = @toon_id
 ORDER BY episode_no;

-- 3. 미수집 / 일부실패 회차 (재실행 대상)
SELECT episode_no, episode_code, image_cnt, saved_image_cnt, org_url
  FROM toon_episode
 WHERE toon_id = @toon_id
   AND has_images = 0
 ORDER BY episode_no;

-- 4. 회차 이미지 목록
SELECT e.episode_no, i.page_no, i.file_path, i.file_size
  FROM toon_episode_image i
  JOIN toon_episode e ON e.episode_code = i.episode_code
 WHERE i.toon_id = @toon_id
 ORDER BY e.episode_no, i.page_no;

-- 5. 전체 웹툰 수집 진행도
SELECT w.toon_id, w.title, w.platform, w.status, w.total_episode,
       w.collect_episode_cnt,
       w.total_episode - w.collect_episode_cnt AS remain_episode,
       w.collect_image_cnt, w.collect_yn, w.collect_started_at, w.collect_ended_at, w.collect_ip
  FROM toon_work_list w
 WHERE w.used = 'Y'
 ORDER BY w.collect_yn, w.updated_at DESC;

-- 6. 수집현황 집계 보정 (toon_episode 기준으로 toon_work_list 재계산)
-- UPDATE toon_work_list w
--    SET w.collect_episode_cnt = (SELECT COALESCE(SUM(e.has_images), 0) FROM toon_episode e WHERE e.toon_id = w.toon_id),
--        w.collect_image_cnt   = (SELECT COALESCE(SUM(e.saved_image_cnt), 0) FROM toon_episode e WHERE e.toon_id = w.toon_id)
--  WHERE w.toon_id = @toon_id;

-- 7. 웹툰 삭제 (DB만 삭제, 로컬 이미지 D:\booktoon\{toon_id} 는 직접 삭제)
--    toon_episode_image 는 toon_episode FK CASCADE, toon_category 는 toon_info FK CASCADE 로 함께 삭제
-- DELETE FROM toon_episode    WHERE toon_id = @toon_id;
-- DELETE FROM toon_work_list  WHERE toon_id = @toon_id;
-- DELETE FROM toon_edit_holds WHERE toon_id = @toon_id;
-- DELETE FROM toon_info       WHERE toon_id = @toon_id;

-- 8. 테이블 전체 제거 (초기화 시에만, 순서 주의)
-- DROP TABLE IF EXISTS toon_episode_image;
-- DROP TABLE IF EXISTS toon_episode;
-- DROP TABLE IF EXISTS toon_category;
-- DROP TABLE IF EXISTS toon_edit_holds;
-- DROP TABLE IF EXISTS toon_work_list;
-- DROP TABLE IF EXISTS toon_info;
