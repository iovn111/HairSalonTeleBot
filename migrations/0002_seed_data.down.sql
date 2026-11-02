DELETE FROM master_service ms
USING master m, service s
WHERE ms.master_id = m.master_id
  AND ms.service_id = s.service_id
  AND m.full_name IN ('Иван Петров', 'Мария Соколова')
  AND s.name IN ('Мужская стрижка', 'Женская стрижка', 'Окрашивание');

DELETE FROM time_slot ts
USING master_schedule ms, master m
WHERE ts.schedule_id = ms.schedule_id
  AND ms.master_id = m.master_id
  AND m.full_name IN ('Иван Петров', 'Мария Соколова')
  AND ts.is_booked = FALSE;

DELETE FROM master_schedule ms
USING master m
WHERE ms.master_id = m.master_id
  AND m.full_name IN ('Иван Петров', 'Мария Соколова')
  AND NOT EXISTS (
      SELECT 1
      FROM time_slot ts
      JOIN booking b ON b.slot_id = ts.slot_id
      WHERE ts.schedule_id = ms.schedule_id
  );

DELETE FROM master m
WHERE m.full_name IN ('Иван Петров', 'Мария Соколова')
  AND NOT EXISTS (
      SELECT 1 FROM booking b WHERE b.master_id = m.master_id
  );

DELETE FROM service s
WHERE s.name IN ('Мужская стрижка', 'Женская стрижка', 'Окрашивание')
  AND NOT EXISTS (
      SELECT 1 FROM booking b WHERE b.service_id = s.service_id
  );
