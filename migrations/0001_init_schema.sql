CREATE TABLE IF NOT EXISTS client (
    telegram_id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS service (
    service_id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    duration_min INTEGER NOT NULL CHECK (duration_min > 0),
    price INTEGER NOT NULL CHECK (price >= 0)
);

CREATE TABLE IF NOT EXISTS master (
    master_id BIGSERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    specialization TEXT NOT NULL,
    telegram_id BIGINT UNIQUE
);

CREATE TABLE IF NOT EXISTS master_service (
    master_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    PRIMARY KEY (master_id, service_id),
    FOREIGN KEY (master_id) REFERENCES master(master_id) ON DELETE CASCADE,
    FOREIGN KEY (service_id) REFERENCES service(service_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS master_schedule (
    schedule_id BIGSERIAL PRIMARY KEY,
    master_id INTEGER NOT NULL,
    work_date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    UNIQUE (master_id, work_date),
    FOREIGN KEY (master_id) REFERENCES master(master_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS time_slot (
    slot_id BIGSERIAL PRIMARY KEY,
    schedule_id INTEGER NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    is_booked BOOLEAN NOT NULL DEFAULT FALSE,
    FOREIGN KEY (schedule_id) REFERENCES master_schedule(schedule_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS booking (
    booking_id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_telegram_id BIGINT NOT NULL,
    service_id INTEGER NOT NULL,
    master_id INTEGER NOT NULL,
    slot_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('confirmed', 'completed', 'cancelled_client', 'cancelled_master')),
    FOREIGN KEY (client_telegram_id) REFERENCES client(telegram_id) ON DELETE CASCADE,
    FOREIGN KEY (service_id) REFERENCES service(service_id),
    FOREIGN KEY (master_id) REFERENCES master(master_id),
    FOREIGN KEY (slot_id) REFERENCES time_slot(slot_id)
);

CREATE TABLE IF NOT EXISTS notification (
    notification_id BIGSERIAL PRIMARY KEY,
    booking_id INTEGER NOT NULL,
    recipient_telegram_id BIGINT NOT NULL,
    type TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (booking_id) REFERENCES booking(booking_id) ON DELETE CASCADE
);
