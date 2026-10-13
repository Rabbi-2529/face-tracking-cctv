-- Runs once, on first initialisation of an empty data directory.

-- Vector similarity search for face embeddings (ArcFace, 512-d).
CREATE EXTENSION IF NOT EXISTS vector;

-- Trigram indexes for fast employee name / code lookup.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Query statistics, so you can see which tracking queries get slow as the
-- detections table grows.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- A read-only role for the vision workers. They need exactly two things:
-- the enabled camera list and the active face gallery. Nothing else, and no
-- write access at all - vision workers publish to Redis, never to Postgres.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vision_ro') THEN
    -- Password is replaced on first boot by the app; this role cannot log in
    -- remotely until docker-compose sets VISION_DB_PASSWORD.
    CREATE ROLE vision_ro LOGIN PASSWORD 'change-me-via-env';
  END IF;
END
$$;

-- Grants are applied after migrations create the tables; see the
-- `grant_vision_ro` management command, run by the migrate service.
