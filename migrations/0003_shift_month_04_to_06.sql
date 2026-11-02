-- Shift schedules from April to June (month 04 -> 06) by adding 2 months.
-- This is a one-off data fix requested by user.
--
-- WARNING:
-- If you already have schedules in June for the same master on the target dates,
-- this update can violate UNIQUE(master_id, work_date).

UPDATE master_schedule
SET work_date = (work_date + INTERVAL '2 months')::date
WHERE EXTRACT(MONTH FROM work_date) = 4;

