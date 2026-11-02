INSERT INTO service(name, duration_min, price)
SELECT x.name, x.duration_min, x.price
FROM (
    VALUES
        ('Мужская стрижка', 45, 900),
        ('Женская стрижка', 60, 1500),
        ('Окрашивание', 120, 3500)
) AS x(name, duration_min, price)
WHERE NOT EXISTS (
    SELECT 1
    FROM service s
    WHERE s.name = x.name
);

INSERT INTO master(full_name, specialization)
SELECT 'Иван Петров', 'Мужские стрижки'
WHERE NOT EXISTS (SELECT 1 FROM master WHERE full_name = 'Иван Петров');

INSERT INTO master(full_name, specialization)
SELECT 'Мария Соколова', 'Женские стрижки и окрашивание'
WHERE NOT EXISTS (SELECT 1 FROM master WHERE full_name = 'Мария Соколова');

INSERT INTO master_service(master_id, service_id)
SELECT m.master_id, s.service_id
FROM master m
CROSS JOIN service s
WHERE NOT EXISTS (
    SELECT 1
    FROM master_service ms
    WHERE ms.master_id = m.master_id
      AND ms.service_id = s.service_id
);

INSERT INTO master_schedule(master_id, work_date, start_time, end_time)
SELECT m.master_id, (CURRENT_DATE + gs.d)::date, '10:00'::time, '18:00'::time
FROM master m
CROSS JOIN generate_series(0, 6) AS gs(d)
WHERE NOT EXISTS (
    SELECT 1
    FROM master_schedule sch
    WHERE sch.master_id = m.master_id
      AND sch.work_date = (CURRENT_DATE + gs.d)::date
);

DO $$
DECLARE
    slot_start_col TEXT;
    slot_end_col TEXT;
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'time_slot' AND column_name = 'start_time'
        ) THEN 'start_time'
        WHEN EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'time_slot' AND column_name = 'slot_start'
        ) THEN 'slot_start'
        ELSE NULL
    END INTO slot_start_col;

    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'time_slot' AND column_name = 'end_time'
        ) THEN 'end_time'
        WHEN EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'time_slot' AND column_name = 'slot_end'
        ) THEN 'slot_end'
        ELSE NULL
    END INTO slot_end_col;

    IF slot_start_col IS NULL OR slot_end_col IS NULL THEN
        RAISE EXCEPTION
            'time_slot должен содержать колонки start_time/end_time или slot_start/slot_end';
    END IF;

    EXECUTE format(
        $sql$
        INSERT INTO time_slot(schedule_id, %I, %I, is_booked)
        SELECT ms.schedule_id, t.start_time::time, (t.start_time + interval '1 hour')::time, FALSE
        FROM master_schedule ms
        CROSS JOIN LATERAL (
            SELECT generate_series(
                ms.work_date + ms.start_time,
                ms.work_date + ms.end_time - interval '1 hour',
                interval '1 hour'
            ) AS start_time
        ) AS t
        LEFT JOIN time_slot ts
            ON ts.schedule_id = ms.schedule_id
           AND ts.%I = t.start_time::time
        WHERE ts.slot_id IS NULL
        $sql$,
        slot_start_col,
        slot_end_col,
        slot_start_col
    );
END $$;
