-- Rollback for 0003_shift_month_04_to_06.sql
-- Shifts schedules in June back to April by subtracting 2 months.
--
-- WARNING:
-- This assumes June rows were created by the "up" migration.
-- If you have genuine June schedules, this rollback may affect them.

UPDATE master_schedule
SET work_date = (work_date - INTERVAL '2 months')::date
WHERE EXTRACT(MONTH FROM work_date) = 6;

