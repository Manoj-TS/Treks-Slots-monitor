-- Lift any stored open-slot cadence up to the 120s floor.
--
-- The floor is enforced in code from this release on (config.OPEN_INTERVAL_MIN,
-- applied in storage.clamp_cadence on both read and write), but a value written
-- before the floor existed is already sitting in this table, and the read path
-- alone would leave the row itself wrong and confusing to anyone reading psql.
-- This corrects the stored row once, so the database agrees with the code.
--
-- 120 is written literally rather than read from the environment: this is a
-- one-time historical correction, not a re-statement of the policy. If the
-- floor is ever deliberately changed, that is a new migration.

UPDATE app_settings
   SET value = to_jsonb(120),
       updated_at = now()
 WHERE key = 'cadence'
   AND jsonb_typeof(value) = 'number'
   AND (value #>> '{}')::numeric < 120;
