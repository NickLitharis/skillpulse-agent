-- Synthetic local/CI smoke fixture. This is not Hacker News data or evaluation evidence.
\set ON_ERROR_STOP on
BEGIN;
INSERT INTO threads (id, title, month, created_at)
VALUES (-900001, 'Synthetic deployment smoke fixture', date_trunc('month', now()), now())
ON CONFLICT (id) DO NOTHING;
INSERT INTO posts (id, thread_id, month, time, text_raw, text_clean, is_deleted, is_dead)
VALUES (-900002, -900001, date_trunc('month', now()), now(), '', '', false, false)
ON CONFLICT (id) DO NOTHING;
INSERT INTO skills (id, name, category, aliases)
VALUES ('python', 'Python', 'language', '[]') ON CONFLICT (id) DO NOTHING;
INSERT INTO post_extractions
(post_id, extractor, taxonomy_version, company, role_family, remote, countries, unknown_terms, is_fallback)
VALUES (-900002, 'baseline', :'taxonomy_version', 'Synthetic smoke company', 'backend', true, '[]', '[]', false)
ON CONFLICT (post_id, extractor, taxonomy_version) DO NOTHING;
INSERT INTO post_skills (post_id, skill_id, required, extractor, taxonomy_version)
VALUES (-900002, 'python', true, 'baseline', :'taxonomy_version')
ON CONFLICT (post_id, skill_id, extractor, taxonomy_version) DO NOTHING;
REFRESH MATERIALIZED VIEW skill_trend_mv;
REFRESH MATERIALIZED VIEW skill_segment_mv;
COMMIT;
