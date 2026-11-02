import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/hairsalon")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "1440"))


@dataclass
class BookingInfo:
    booking_id: int
    service_name: str
    master_name: str
    slot_date: str
    start_time: str
    status: str


class BookingStates(StatesGroup):
    choosing_service = State()
    choosing_master = State()
    choosing_day = State()
    choosing_slot = State()

class MasterPanelStates(StatesGroup):
    choosing_free_day = State()
    choosing_free_start = State()
    choosing_free_duration = State()

class AdminPanelStates(StatesGroup):
    choosing_master_for_free = State()
    choosing_master_for_services = State()

class AdminMastersStates(StatesGroup):
    adding_full_name = State()
    adding_specialization = State()


class HairSalonRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.conn = psycopg2.connect(self.database_url, cursor_factory=RealDictCursor)
        self.conn.autocommit = False

    def setup(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
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
            """
        )
        self.conn.commit()
        self._seed_demo_data()

    def _seed_demo_data(self) -> None:
        cur = self.conn.cursor()
        services = [
            ("Мужская стрижка", 45, 900),
            ("Женская стрижка", 60, 1500),
            ("Окрашивание", 120, 3500),
        ]
        cur.executemany(
            """
            INSERT INTO service(name, duration_min, price)
            VALUES (%s, %s, %s)
            ON CONFLICT(name) DO NOTHING
            """,
            services,
        )

        masters = [
            ("Иван Петров", "Мужские стрижки"),
            ("Мария Соколова", "Женские стрижки и окрашивание"),
        ]
        for full_name, specialization in masters:
            cur.execute(
                "SELECT master_id FROM master WHERE full_name = %s",
                (full_name,),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO master(full_name, specialization) VALUES (%s, %s)",
                    (full_name, specialization),
                )

        cur.execute("SELECT master_id FROM master ORDER BY master_id")
        master_ids = [r["master_id"] for r in cur.fetchall()]
        cur.execute("SELECT service_id FROM service ORDER BY service_id")
        service_ids = [r["service_id"] for r in cur.fetchall()]

        for m_id in master_ids:
            for s_id in service_ids:
                cur.execute(
                    """
                    INSERT INTO master_service(master_id, service_id)
                    VALUES (%s, %s)
                    ON CONFLICT(master_id, service_id) DO NOTHING
                    """,
                    (m_id, s_id),
                )

        for day_shift in range(7):
            work_day = date.today() + timedelta(days=day_shift)
            for master_id in master_ids:
                cur.execute(
                    """
                    INSERT INTO master_schedule(master_id, work_date, start_time, end_time)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(master_id, work_date) DO NOTHING
                    """,
                    (master_id, work_day.isoformat(), "10:00", "18:00"),
                )
                cur.execute(
                    "SELECT schedule_id FROM master_schedule WHERE master_id = %s AND work_date = %s",
                    (master_id, work_day.isoformat()),
                )
                schedule_id = cur.fetchone()["schedule_id"]
                self._ensure_slots(schedule_id, time(10, 0), time(18, 0), slot_minutes=60)
        self.conn.commit()

    def _ensure_slots(
        self, schedule_id: int, start_at: time, end_at: time, slot_minutes: int
    ) -> None:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM time_slot WHERE schedule_id = %s", (schedule_id,))
        if cur.fetchone()["cnt"] > 0:
            return

        start_dt = datetime.combine(date.today(), start_at)
        end_dt = datetime.combine(date.today(), end_at)
        while start_dt < end_dt:
            slot_end = start_dt + timedelta(minutes=slot_minutes)
            if slot_end > end_dt:
                break
            cur.execute(
                """
                INSERT INTO time_slot(schedule_id, start_time, end_time, is_booked)
                VALUES (%s, %s, %s, FALSE)
                """,
                (schedule_id, start_dt.time().strftime("%H:%M"), slot_end.time().strftime("%H:%M")),
            )
            start_dt = slot_end

    def register_client(self, telegram_id: int, name: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO client(telegram_id, name)
            VALUES (%s, %s)
            ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, name),
        )
        self.conn.commit()

    def get_services(self) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT service_id, name, duration_min, price FROM service ORDER BY name")
        return cur.fetchall()

    def get_master_services(self, master_id: int) -> set[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT service_id FROM master_service WHERE master_id = %s", (master_id,))
        return {int(r["service_id"]) for r in cur.fetchall()}

    def get_masters_for_service(self, service_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT m.master_id, m.full_name, m.specialization
            FROM master m
            JOIN master_service ms ON ms.master_id = m.master_id
            WHERE ms.service_id = %s
            ORDER BY m.full_name
            """,
            (service_id,),
        )
        return cur.fetchall()

    def get_available_days(self, master_id: int) -> list[str]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT ms.work_date
            FROM master_schedule ms
            JOIN time_slot ts ON ts.schedule_id = ms.schedule_id
            WHERE ms.master_id = %s
              AND ts.is_booked = FALSE
              AND (ms.work_date + ts.start_time) >= NOW()
            GROUP BY ms.work_date
            ORDER BY ms.work_date
            """,
            (master_id,),
        )
        rows = cur.fetchall()
        return [str(r["work_date"]) for r in rows]

    def get_available_slots(self, master_id: int, day: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT ts.slot_id, ts.start_time, ts.end_time
            FROM time_slot ts
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            WHERE ms.master_id = %s
              AND ms.work_date = %s
              AND ts.is_booked = FALSE
              AND (ms.work_date + ts.start_time) >= NOW()
            ORDER BY ts.start_time
            """,
            (master_id, day),
        )
        return cur.fetchall()

    def create_booking(
        self, telegram_id: int, service_id: int, master_id: int, slot_id: int
    ) -> int | None:
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT is_booked FROM time_slot WHERE slot_id = %s FOR UPDATE", (slot_id,))
            row = cur.fetchone()
            if row is None or row["is_booked"]:
                self.conn.rollback()
                return None

            cur.execute("UPDATE time_slot SET is_booked = TRUE WHERE slot_id = %s", (slot_id,))
            cur.execute(
                """
                INSERT INTO booking(client_telegram_id, service_id, master_id, slot_id, status)
                VALUES (%s, %s, %s, %s, 'confirmed')
                RETURNING booking_id
                """,
                (telegram_id, service_id, master_id, slot_id),
            )
            booking_id = cur.fetchone()["booking_id"]
            cur.execute(
                """
                INSERT INTO notification(booking_id, recipient_telegram_id, type)
                VALUES (%s, %s, 'booking_created')
                """,
                (booking_id, telegram_id),
            )
            self.conn.commit()
            return int(booking_id)
        except Exception:
            self.conn.rollback()
            raise

    def get_user_bookings(self, telegram_id: int, include_cancelled: bool = False) -> list[BookingInfo]:
        status_filter = "" if include_cancelled else "AND b.status NOT IN ('cancelled_client', 'cancelled_master')"
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT b.booking_id, s.name AS service_name, m.full_name AS master_name,
                   ms.work_date AS slot_date, ts.start_time::text AS start_time, b.status
            FROM booking b
            JOIN service s ON s.service_id = b.service_id
            JOIN master m ON m.master_id = b.master_id
            JOIN time_slot ts ON ts.slot_id = b.slot_id
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            WHERE b.client_telegram_id = %s
            {status_filter}
            ORDER BY ms.work_date, ts.start_time
            """,
            (telegram_id,),
        )
        rows = cur.fetchall()
        return [
            BookingInfo(
                booking_id=r["booking_id"],
                service_name=r["service_name"],
                master_name=r["master_name"],
                slot_date=str(r["slot_date"]),
                start_time=r["start_time"][:5],
                status=r["status"],
            )
            for r in rows
        ]

    def cancel_booking(self, booking_id: int, cancelled_by: str) -> bool:
        status = "cancelled_client" if cancelled_by == "client" else "cancelled_master"
        cur = self.conn.cursor()
        cur.execute(
            "SELECT slot_id, status FROM booking WHERE booking_id = %s",
            (booking_id,),
        )
        row = cur.fetchone()
        if row is None or row["status"] in ("cancelled_client", "cancelled_master"):
            return False

        cur.execute("UPDATE time_slot SET is_booked = FALSE WHERE slot_id = %s", (row["slot_id"],))
        cur.execute("UPDATE booking SET status = %s WHERE booking_id = %s", (status, booking_id))
        self.conn.commit()
        return True

    def add_service(self, name: str, duration_min: int, price: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO service(name, duration_min, price) VALUES (%s, %s, %s)",
            (name, duration_min, price),
        )
        self.conn.commit()

    def add_master(self, full_name: str, specialization: str) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO master(full_name, specialization) VALUES (%s, %s) RETURNING master_id",
            (full_name, specialization),
        )
        master_id = cur.fetchone()["master_id"]
        self.conn.commit()
        return int(master_id)

    def add_master_service(self, master_id: int, service_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO master_service(master_id, service_id)
            VALUES (%s, %s)
            ON CONFLICT(master_id, service_id) DO NOTHING
            """,
            (master_id, service_id),
        )
        self.conn.commit()

    def remove_master_service(self, master_id: int, service_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM master_service WHERE master_id = %s AND service_id = %s",
            (master_id, service_id),
        )
        deleted = cur.rowcount > 0
        self.conn.commit()
        return deleted

    def set_master_telegram(self, master_id: int, telegram_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE master SET telegram_id = %s WHERE master_id = %s", (telegram_id, master_id))
        self.conn.commit()

    def add_schedule(self, master_id: int, work_date: str, start: str, end: str, slot_minutes: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO master_schedule(master_id, work_date, start_time, end_time)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(master_id, work_date)
            DO UPDATE SET start_time = EXCLUDED.start_time, end_time = EXCLUDED.end_time
            RETURNING schedule_id
            """,
            (master_id, work_date, start, end),
        )
        schedule_id = cur.fetchone()["schedule_id"]
        cur.execute("DELETE FROM time_slot WHERE schedule_id = %s AND is_booked = FALSE", (schedule_id,))
        self._ensure_slots(
            schedule_id=schedule_id,
            start_at=datetime.strptime(start, "%H:%M").time(),
            end_at=datetime.strptime(end, "%H:%M").time(),
            slot_minutes=slot_minutes,
        )
        self.conn.commit()

    def admin_bookings_report(self) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT b.booking_id, c.name AS client_name, s.name AS service_name,
                   m.full_name AS master_name, ms.work_date, ts.start_time::text AS start_time, b.status
            FROM booking b
            JOIN client c ON c.telegram_id = b.client_telegram_id
            JOIN service s ON s.service_id = b.service_id
            JOIN master m ON m.master_id = b.master_id
            JOIN time_slot ts ON ts.slot_id = b.slot_id
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            ORDER BY ms.work_date DESC, ts.start_time DESC
            LIMIT 30
            """
        )
        return cur.fetchall()

    def master_schedule(self, telegram_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT master_id FROM master WHERE telegram_id = %s",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row is None:
            return []
        cur.execute(
            """
            SELECT s.name AS service_name, ms.work_date, ts.start_time::text AS start_time, b.status
            FROM booking b
            JOIN service s ON s.service_id = b.service_id
            JOIN time_slot ts ON ts.slot_id = b.slot_id
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            WHERE b.master_id = %s
            ORDER BY ms.work_date, ts.start_time
            LIMIT 30
            """,
            (row["master_id"],),
        )
        return cur.fetchall()

    def get_master_id_by_telegram(self, telegram_id: int) -> int | None:
        cur = self.conn.cursor()
        cur.execute("SELECT master_id FROM master WHERE telegram_id = %s", (telegram_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return int(row["master_id"])

    def get_masters(self) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT master_id, full_name, specialization FROM master ORDER BY full_name")
        return cur.fetchall()

    def admin_master_stats(self) -> list[dict]:
        """
        Statistics for admin:
        - planned_bookings_cnt: count of future confirmed bookings for each master
        - planned_avg_price: average service price of those bookings
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT
                m.master_id,
                m.full_name,
                COALESCE(
                    COUNT(b.booking_id) FILTER (
                        WHERE b.status = 'confirmed'
                          AND (ms.work_date + ts.start_time) >= NOW()
                    ),
                    0
                ) AS planned_bookings_cnt,
                AVG(s.price) FILTER (
                    WHERE b.status = 'confirmed'
                      AND (ms.work_date + ts.start_time) >= NOW()
                ) AS planned_avg_price
            FROM master m
            LEFT JOIN booking b ON b.master_id = m.master_id
            LEFT JOIN service s ON s.service_id = b.service_id
            LEFT JOIN time_slot ts ON ts.slot_id = b.slot_id
            LEFT JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            GROUP BY m.master_id, m.full_name
            ORDER BY m.full_name
            """
        )
        return cur.fetchall()

    def delete_master(self, master_id: int) -> bool:
        """
        Delete master only if there are no bookings referencing them.
        Returns True if deleted.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM booking WHERE master_id = %s LIMIT 1", (master_id,))
        if cur.fetchone() is not None:
            return False
        cur.execute("DELETE FROM master WHERE master_id = %s", (master_id,))
        deleted = cur.rowcount > 0
        self.conn.commit()
        return deleted

    def get_free_slots_for_day(self, master_id: int, work_date: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT ts.slot_id, ts.start_time::text AS start_time, ts.end_time::text AS end_time
            FROM time_slot ts
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            WHERE ms.master_id = %s
              AND ms.work_date = %s
              AND ts.is_booked = FALSE
              AND (ms.work_date + ts.end_time) >= NOW()
            ORDER BY ts.start_time
            """,
            (master_id, work_date),
        )
        return cur.fetchall()

    def cleanup_past_free_slots(self) -> int:
        """
        Remove past unbooked slots so they don't linger in DB.
        Returns number of deleted slots.
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            DELETE FROM time_slot ts
            USING master_schedule ms
            WHERE ts.schedule_id = ms.schedule_id
              AND ts.is_booked = FALSE
              AND (ms.work_date + ts.end_time) < NOW()
            """,
        )
        deleted = int(cur.rowcount or 0)
        self.conn.commit()
        return deleted

    def get_master_bookings(self, master_id: int, include_cancelled: bool = False) -> list[dict]:
        status_filter = "" if include_cancelled else "AND b.status NOT IN ('cancelled_client', 'cancelled_master')"
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT b.booking_id,
                   c.name AS client_name,
                   c.telegram_id AS client_telegram_id,
                   s.name AS service_name,
                   ms.work_date,
                   ts.start_time::text AS start_time,
                   ts.end_time::text AS end_time,
                   b.status
            FROM booking b
            JOIN client c ON c.telegram_id = b.client_telegram_id
            JOIN service s ON s.service_id = b.service_id
            JOIN time_slot ts ON ts.slot_id = b.slot_id
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            WHERE b.master_id = %s
            {status_filter}
            ORDER BY ms.work_date DESC, ts.start_time DESC
            LIMIT 50
            """,
            (master_id,),
        )
        return cur.fetchall()

    def cancel_booking_as_master(self, booking_id: int, master_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT booking_id FROM booking WHERE booking_id = %s AND master_id = %s",
            (booking_id, master_id),
        )
        if cur.fetchone() is None:
            return False
        return self.cancel_booking(booking_id, cancelled_by="master")

    def cancel_booking_as_admin(self, booking_id: int) -> bool:
        return self.cancel_booking(booking_id, cancelled_by="master")

    def add_free_slot(self, master_id: int, work_date: str, start: str, end: str) -> int:
        """
        Create a free (unbooked) time slot for given master and date.
        Returns created slot_id.
        """
        start_t = datetime.strptime(start, "%H:%M").time()
        end_t = datetime.strptime(end, "%H:%M").time()
        if end_t <= start_t:
            raise ValueError("end_time must be after start_time")

        cur = self.conn.cursor()
        cur.execute(
            "SELECT schedule_id, start_time, end_time FROM master_schedule WHERE master_id = %s AND work_date = %s",
            (master_id, work_date),
        )
        schedule = cur.fetchone()
        if schedule is None:
            cur.execute(
                """
                INSERT INTO master_schedule(master_id, work_date, start_time, end_time)
                VALUES (%s, %s, %s, %s)
                RETURNING schedule_id
                """,
                (master_id, work_date, start, end),
            )
            schedule_id = int(cur.fetchone()["schedule_id"])
        else:
            schedule_id = int(schedule["schedule_id"])
            schedule_start = schedule["start_time"]
            schedule_end = schedule["end_time"]
            new_start = min(schedule_start, start_t)
            new_end = max(schedule_end, end_t)
            if new_start != schedule_start or new_end != schedule_end:
                cur.execute(
                    """
                    UPDATE master_schedule
                    SET start_time = %s, end_time = %s
                    WHERE schedule_id = %s
                    """,
                    (new_start.strftime("%H:%M"), new_end.strftime("%H:%M"), schedule_id),
                )

        # prevent overlap with existing slots for this schedule (both booked and free)
        cur.execute(
            """
            SELECT 1
            FROM time_slot
            WHERE schedule_id = %s
              AND NOT (end_time <= %s OR start_time >= %s)
            LIMIT 1
            """,
            (schedule_id, start, end),
        )
        if cur.fetchone() is not None:
            self.conn.rollback()
            raise ValueError("overlaps existing slot")

        cur.execute(
            """
            INSERT INTO time_slot(schedule_id, start_time, end_time, is_booked)
            VALUES (%s, %s, %s, FALSE)
            RETURNING slot_id
            """,
            (schedule_id, start, end),
        )
        slot_id = int(cur.fetchone()["slot_id"])
        self.conn.commit()
        return slot_id

    def delete_free_slot(self, slot_id: int, master_id: int | None = None) -> bool:
        """
        Delete a free slot (only if not booked).
        If master_id is provided, slot must belong to that master.
        """
        cur = self.conn.cursor()
        if master_id is None:
            cur.execute(
                """
                SELECT ts.slot_id
                FROM time_slot ts
                WHERE ts.slot_id = %s AND ts.is_booked = FALSE
                """,
                (slot_id,),
            )
        else:
            cur.execute(
                """
                SELECT ts.slot_id
                FROM time_slot ts
                JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
                WHERE ts.slot_id = %s
                  AND ts.is_booked = FALSE
                  AND ms.master_id = %s
                """,
                (slot_id, master_id),
            )
        if cur.fetchone() is None:
            return False
        cur.execute("DELETE FROM time_slot WHERE slot_id = %s AND is_booked = FALSE", (slot_id,))
        self.conn.commit()
        return True

    def pending_reminders(self, minutes_before: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT b.booking_id, b.client_telegram_id, s.name AS service_name,
                   m.full_name AS master_name, ms.work_date, ts.start_time::text AS start_time
            FROM booking b
            JOIN service s ON s.service_id = b.service_id
            JOIN master m ON m.master_id = b.master_id
            JOIN time_slot ts ON ts.slot_id = b.slot_id
            JOIN master_schedule ms ON ms.schedule_id = ts.schedule_id
            WHERE b.status = 'confirmed'
              AND (ms.work_date + ts.start_time) BETWEEN NOW() AND NOW() + (%s || ' minutes')::interval
              AND NOT EXISTS (
                  SELECT 1
                  FROM notification n
                  WHERE n.booking_id = b.booking_id
                    AND n.type = %s
              )
            ORDER BY ms.work_date, ts.start_time
            """,
            (minutes_before, f"reminder_{minutes_before}"),
        )
        return cur.fetchall()

    def mark_reminder_sent(self, booking_id: int, recipient_telegram_id: int, minutes_before: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO notification(booking_id, recipient_telegram_id, type)
            VALUES (%s, %s, %s)
            """,
            (booking_id, recipient_telegram_id, f"reminder_{minutes_before}"),
        )
        self.conn.commit()


repo = HairSalonRepository(DATABASE_URL)
dp = Dispatcher()


def _reply_start_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Начать")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def _menu_keyboard(*, is_admin: bool, is_master: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Записаться", callback_data="menu:book")
    kb.button(text="Мои записи", callback_data="menu:my_bookings")
    kb.button(text="Отменить запись", callback_data="menu:cancel")
    if is_master or is_admin:
        kb.button(text="Мастер: мои записи", callback_data="menu:master_bookings")
        kb.button(text="Панель мастера", callback_data="menu:master_panel")
    if is_admin:
        kb.button(text="Админ отчёт", callback_data="menu:admin_report")
        kb.button(text="Панель администратора", callback_data="menu:admin_panel")
    kb.adjust(1)
    return kb.as_markup()


def _inline_buttons(options: Iterable[tuple[str, str]], width: int = 1) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, callback in options:
        kb.button(text=text, callback_data=callback)
    kb.adjust(width)
    return kb.as_markup()

def _main_menu_for(user_id: int) -> InlineKeyboardMarkup:
    return _menu_keyboard(
        is_admin=user_id in ADMIN_IDS,
        is_master=repo.get_master_id_by_telegram(user_id) is not None,
    )

def _back_to_menu_kb() -> InlineKeyboardMarkup:
    return _inline_buttons([("⬅️ В меню", "nav:menu")])

def _back_to_master_panel_kb() -> InlineKeyboardMarkup:
    return _inline_buttons([("⬅️ Назад", "nav:master_panel")])

def _back_to_admin_panel_kb() -> InlineKeyboardMarkup:
    return _inline_buttons([("⬅️ Назад", "nav:admin_panel")])

def _time_options(start_hour: int = 9, end_hour: int = 20, step_minutes: int = 30) -> list[str]:
    out: list[str] = []
    t = datetime.combine(date.today(), time(start_hour, 0))
    end = datetime.combine(date.today(), time(end_hour, 0))
    while t <= end:
        out.append(t.time().strftime("%H:%M"))
        t += timedelta(minutes=step_minutes)
    return out

def _duration_options() -> list[int]:
    return [30, 45, 60, 90, 120]

async def _render_free_day(message: Message, state: FSMContext, *, day: str) -> None:
    data = await state.get_data()
    master_id = int(data["target_master_id"])
    slots = repo.get_free_slots_for_day(master_id, day)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить слот", callback_data=f"free:add:{day}")
    for s in slots[:10]:
        kb.button(
            text=f"🗑 {s['start_time'][:5]}-{s['end_time'][:5]} (id {s['slot_id']})",
            callback_data=f"free:del:{s['slot_id']}:{day}",
        )
    kb.button(text="⬅️ Назад", callback_data="master:free_time")
    kb.adjust(1)
    text = "Свободные слоты на дату " + day
    if not slots:
        text += ":\n— нет слотов —"
    else:
        text += ":\n" + "\n".join(
            f"{s['start_time'][:5]}-{s['end_time'][:5]} (slot_id={s['slot_id']})" for s in slots[:10]
        )
    await message.answer(text, reply_markup=kb.as_markup())


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    repo.register_client(user.id, user.full_name)
    is_admin = user.id in ADMIN_IDS
    is_master = repo.get_master_id_by_telegram(user.id) is not None
    # Reply-клавиатуру и inline-меню нельзя отправить в одном сообщении — две ответные реплики.
    await message.answer(
        "Добро пожаловать в бот парикмахерской.\n\n"
        "Под полем ввода закреплена кнопка «Начать» — нажмите её в любой момент, чтобы снова открыть меню без команд.",
        reply_markup=_reply_start_keyboard(),
    )
    await message.answer(
        "Вы можете выбрать услугу, мастера и удобный слот, а затем управлять своими записями.",
        reply_markup=_menu_keyboard(is_admin=is_admin, is_master=is_master),
    )

@dp.message(F.text)
async def text_start_button(message: Message) -> None:
    """
    Allow new/any client to start dialog with a simple button/message "Начать".
    Telegram already shows native Start button; this handler covers the UX request
    where user sends/presses a "Начать" message.
    """
    text = (message.text or "").strip()
    if text.casefold() not in {"начать", "start"}:
        return
    await cmd_start(message)

@dp.callback_query(F.data == "nav:menu")
async def nav_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    await callback.message.answer("Главное меню:", reply_markup=_main_menu_for(user.id))
    await callback.answer()

@dp.callback_query(F.data == "nav:master_panel")
async def nav_master_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None and user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Мои записи", callback_data="master:bookings")
    kb.button(text="Свободное время", callback_data="master:free_time")
    kb.button(text="⬅️ В меню", callback_data="nav:menu")
    kb.adjust(1)
    await callback.message.answer("Панель мастера:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "nav:admin_panel")
async def nav_admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Отчёт по записям", callback_data="admin:report")
    kb.button(text="Статистика по мастерам", callback_data="admin:stats")
    kb.button(text="Записи мастера", callback_data="admin:pick_master_bookings")
    kb.button(text="Свободное время мастеров", callback_data="admin:pick_master_free")
    kb.button(text="Услуги мастеров", callback_data="admin:pick_master_services")
    kb.button(text="Мастера (добавить/удалить)", callback_data="admin:masters_manage")
    kb.button(text="⬅️ В меню", callback_data="nav:menu")
    kb.adjust(1)
    await callback.message.answer("Панель администратора:", reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "menu:book")
async def start_booking(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    services = repo.get_services()
    if not services:
        await callback.message.answer("Пока нет доступных услуг.")
        await callback.answer()
        return
    options = [
        (
            f"{row['name']} ({row['duration_min']} мин / {row['price']} ₽)",
            f"service:{row['service_id']}",
        )
        for row in services
    ]
    await state.set_state(BookingStates.choosing_service)
    await callback.message.answer("Выберите услугу:", reply_markup=_inline_buttons(options))
    await callback.answer()


@dp.callback_query(BookingStates.choosing_service, F.data.startswith("service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = int(callback.data.split(":")[1])
    await state.update_data(service_id=service_id)
    masters = repo.get_masters_for_service(service_id)
    if not masters:
        await callback.message.answer("Для услуги нет доступных мастеров.")
        await callback.answer()
        return
    options = [(f"{m['full_name']} ({m['specialization']})", f"master:{m['master_id']}") for m in masters]
    await state.set_state(BookingStates.choosing_master)
    await callback.message.answer("Выберите мастера:", reply_markup=_inline_buttons(options))
    await callback.answer()


@dp.callback_query(BookingStates.choosing_master, F.data.startswith("master:"))
async def choose_master(callback: CallbackQuery, state: FSMContext) -> None:
    master_id = int(callback.data.split(":")[1])
    await state.update_data(master_id=master_id)
    days = repo.get_available_days(master_id)
    if not days:
        await callback.message.answer("У мастера пока нет свободных дат.")
        await callback.answer()
        return
    options = [(d, f"day:{d}") for d in days[:10]]
    await state.set_state(BookingStates.choosing_day)
    await callback.message.answer("Выберите дату:", reply_markup=_inline_buttons(options, width=2))
    await callback.answer()


@dp.callback_query(BookingStates.choosing_day, F.data.startswith("day:"))
async def choose_day(callback: CallbackQuery, state: FSMContext) -> None:
    selected_day = callback.data.split(":", 1)[1]
    data = await state.get_data()
    master_id = int(data["master_id"])
    slots = repo.get_available_slots(master_id, selected_day)
    if not slots:
        await callback.message.answer("На эту дату свободных слотов нет.")
        await callback.answer()
        return
    await state.update_data(day=selected_day)
    options = [(f"{s['start_time']} - {s['end_time']}", f"slot:{s['slot_id']}") for s in slots]
    await state.set_state(BookingStates.choosing_slot)
    await callback.message.answer("Выберите время:", reply_markup=_inline_buttons(options, width=2))
    await callback.answer()


@dp.callback_query(BookingStates.choosing_slot, F.data.startswith("slot:"))
async def choose_slot(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    slot_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    booking_id = repo.create_booking(
        telegram_id=user.id,
        service_id=int(data["service_id"]),
        master_id=int(data["master_id"]),
        slot_id=slot_id,
    )
    await state.clear()
    if booking_id is None:
        await callback.message.answer("Слот уже занят. Попробуйте выбрать другое время.")
        await callback.answer()
        return
    await callback.message.answer(
        f"Запись создана. Номер брони: #{booking_id}\n"
        "Вы получите напоминание о записи за 24 часа.",
        reply_markup=_menu_keyboard(
            is_admin=user.id in ADMIN_IDS,
            is_master=repo.get_master_id_by_telegram(user.id) is not None,
        ),
    )
    await callback.answer("Готово")


@dp.callback_query(F.data == "menu:my_bookings")
@dp.message(Command("my_bookings"))
async def my_bookings(event: CallbackQuery | Message) -> None:
    user = event.from_user
    if user is None:
        return
    bookings = repo.get_user_bookings(user.id)
    if not bookings:
        text = "У вас пока нет активных записей."
    else:
        rows = []
        for b in bookings:
            status_part = f" | {b.status}" if b.status != "confirmed" else ""
            rows.append(
                f"#{b.booking_id}: {b.service_name} | {b.master_name} | {b.slot_date} {b.start_time}{status_part}"
            )
        text = "Ваши записи:\n" + "\n".join(rows)

    if isinstance(event, CallbackQuery):
        await event.message.answer(text)
        await event.answer()
    else:
        await event.answer(text)


@dp.callback_query(F.data == "menu:cancel")
async def cancel_menu(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    bookings = repo.get_user_bookings(user.id)
    if not bookings:
        await callback.message.answer("Нет активных записей для отмены.")
        await callback.answer()
        return
    options = [(f"#{b.booking_id} {b.slot_date} {b.start_time}", f"cancel:{b.booking_id}") for b in bookings]
    await callback.message.answer("Выберите запись для отмены:", reply_markup=_inline_buttons(options))
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":")[1])
    done = repo.cancel_booking(booking_id, cancelled_by="client")
    await callback.message.answer("Запись отменена." if done else "Не удалось отменить запись.")
    await callback.answer()


@dp.callback_query(F.data == "menu:admin_report")
async def admin_report(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    report = repo.admin_bookings_report()
    if not report:
        await callback.message.answer("Записей пока нет.")
        await callback.answer()
        return
    rows = [
        f"#{r['booking_id']} | {r['client_name']} | {r['service_name']} | {r['master_name']} | {r['work_date']} {r['start_time']} | {r['status']}"
        for r in report
    ]
    await callback.message.answer("Последние записи:\n" + "\n".join(rows))
    await callback.answer()

@dp.callback_query(F.data == "menu:master_bookings")
async def master_bookings_entrypoint(callback: CallbackQuery, state: FSMContext) -> None:
    # route into panel to avoid “командный” UX
    await nav_master_panel(callback, state)

@dp.callback_query(F.data == "menu:admin_report")
async def admin_report_entrypoint(callback: CallbackQuery, state: FSMContext) -> None:
    # keep existing report, but also allow panel navigation
    await admin_report(callback)

@dp.callback_query(F.data == "menu:admin_panel")
async def admin_panel_entrypoint(callback: CallbackQuery, state: FSMContext) -> None:
    await nav_admin_panel(callback, state)

@dp.callback_query(F.data == "menu:master_panel")
async def master_panel_entrypoint(callback: CallbackQuery, state: FSMContext) -> None:
    await nav_master_panel(callback, state)

@dp.callback_query(F.data == "menu:master_bookings")
async def master_bookings_menu(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return

    is_admin = user.id in ADMIN_IDS
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None and not is_admin:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    if master_id is None and is_admin:
        await callback.message.answer(
            "Вы админ, но не привязаны как мастер. "
            "Используйте /admin_master_bookings|master_id чтобы смотреть записи мастера."
        )
        await callback.answer()
        return

    rows = repo.get_master_bookings(int(master_id))
    if not rows:
        await callback.message.answer("У вас пока нет записей.")
        await callback.answer()
        return
    text = "\n".join(
        f"#{r['booking_id']} | {r['client_name']} | {r['service_name']} | "
        f"{r['work_date']} {r['start_time'][:5]} | {r['status']}"
        for r in rows
    )
    await callback.message.answer(
        "Ваши записи (последние 50):\n"
        + text
        + "\n\nОтменить запись: /master_cancel|booking_id"
    )
    await callback.answer()

@dp.callback_query(F.data == "master:bookings")
async def master_bookings_panel(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    is_admin = user.id in ADMIN_IDS
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None and not is_admin:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if master_id is None:
        await callback.message.answer("Вы не привязаны как мастер.", reply_markup=_back_to_admin_panel_kb())
        await callback.answer()
        return

    rows = repo.get_master_bookings(int(master_id))
    if not rows:
        await callback.message.answer("У вас пока нет записей.", reply_markup=_back_to_master_panel_kb())
        await callback.answer()
        return

    kb = InlineKeyboardBuilder()
    for r in rows[:10]:
        kb.button(
            text=f"Отменить #{r['booking_id']} • {r['work_date']} {r['start_time'][:5]}",
            callback_data=f"master:cancel:{r['booking_id']}",
        )
    kb.button(text="⬅️ Назад", callback_data="nav:master_panel")
    kb.adjust(1)
    text = "\n".join(
        f"#{r['booking_id']} | {r['client_name']} | {r['service_name']} | {r['work_date']} {r['start_time'][:5]} | {r['status']}"
        for r in rows[:10]
    )
    await callback.message.answer("Ваши записи:\n" + text, reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("master:cancel:"))
async def master_cancel_button(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    booking_id = int(callback.data.split(":")[2])
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None and user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if master_id is None and user.id in ADMIN_IDS:
        done = repo.cancel_booking_as_admin(booking_id)
    else:
        done = repo.cancel_booking_as_master(booking_id, int(master_id))
    await callback.message.answer("Запись отменена." if done else "Не удалось отменить запись.")
    await callback.answer()

@dp.callback_query(F.data == "master:free_time")
async def master_free_time(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_master_id=int(master_id))
    # show next 14 days
    days = [(date.today() + timedelta(days=i)).isoformat() for i in range(14)]
    opts = [(d, f"free:day:{d}") for d in days]
    kb = InlineKeyboardBuilder()
    for text, cb in opts:
        kb.button(text=text, callback_data=cb)
    kb.button(text="⬅️ Назад", callback_data="nav:master_panel")
    kb.adjust(2)
    await state.set_state(MasterPanelStates.choosing_free_day)
    await callback.message.answer("Выберите дату для управления свободным временем:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(MasterPanelStates.choosing_free_day, F.data.startswith("free:day:"))
async def free_day_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    day = callback.data.split(":", 2)[2]
    await _render_free_day(callback.message, state, day=day)
    await callback.answer()

@dp.callback_query(F.data.startswith("free:del:"))
async def free_delete(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    slot_id = int(parts[2])
    day = parts[3]
    is_admin = user.id in ADMIN_IDS
    data = await state.get_data()
    target_master_id = int(data.get("target_master_id") or 0)
    if is_admin:
        done = repo.delete_free_slot(slot_id, master_id=None)
    else:
        done = repo.delete_free_slot(slot_id, master_id=target_master_id)
    await callback.message.answer("Слот удалён." if done else "Не удалось удалить слот.")
    await _render_free_day(callback.message, state, day=day)
    await callback.answer()

@dp.callback_query(F.data.startswith("free:add:"))
async def free_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    day = callback.data.split(":", 2)[2]
    await state.update_data(free_day=day)
    times = _time_options()
    kb = InlineKeyboardBuilder()
    for t in times:
        kb.button(text=t, callback_data=f"free:start:{t}")
    kb.button(text="⬅️ Назад", callback_data=f"free:day:{day}")
    kb.adjust(4)
    await state.set_state(MasterPanelStates.choosing_free_start)
    await callback.message.answer("Выберите время начала:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(MasterPanelStates.choosing_free_start, F.data.startswith("free:start:"))
async def free_start_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    start = callback.data.split(":", 2)[2]
    await state.update_data(free_start=start)
    kb = InlineKeyboardBuilder()
    for d in _duration_options():
        kb.button(text=f"{d} мин", callback_data=f"free:dur:{d}")
    data = await state.get_data()
    day = data["free_day"]
    kb.button(text="⬅️ Назад", callback_data=f"free:add:{day}")
    kb.adjust(3)
    await state.set_state(MasterPanelStates.choosing_free_duration)
    await callback.message.answer("Выберите длительность слота:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(MasterPanelStates.choosing_free_duration, F.data.startswith("free:dur:"))
async def free_duration_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    minutes = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    master_id = int(data["target_master_id"])
    day = data["free_day"]
    start = data["free_start"]
    start_dt = datetime.combine(date.fromisoformat(day), datetime.strptime(start, "%H:%M").time())
    end_dt = start_dt + timedelta(minutes=minutes)
    end = end_dt.time().strftime("%H:%M")
    try:
        repo.add_free_slot(master_id, day, start, end)
    except ValueError as e:
        await callback.message.answer(f"Не удалось добавить слот: {e}", reply_markup=_back_to_master_panel_kb())
        await callback.answer()
        return
    await callback.message.answer(f"Слот добавлен: {day} {start}-{end}")
    # back to day view
    await state.set_state(MasterPanelStates.choosing_free_day)
    await _render_free_day(callback.message, state, day=day)
    await callback.answer()

@dp.callback_query(F.data == "admin:report")
async def admin_report_panel(callback: CallbackQuery) -> None:
    # reuse existing report
    await admin_report(callback)

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    rows = repo.admin_master_stats()
    if not rows:
        await callback.message.answer("Мастеров пока нет.", reply_markup=_back_to_admin_panel_kb())
        await callback.answer()
        return
    lines: list[str] = []
    for r in rows:
        cnt = int(r["planned_bookings_cnt"] or 0)
        avg = r["planned_avg_price"]
        avg_txt = "—" if avg is None else f"{int(round(float(avg)))} ₽"
        lines.append(f"{r['full_name']}: {cnt} запланировано, средняя стоимость {avg_txt}")
    await callback.message.answer(
        "Статистика по запланированным записям (confirmed, будущие):\n" + "\n".join(lines),
        reply_markup=_back_to_admin_panel_kb(),
    )
    await callback.answer()

def _admin_masters_manage_kb(masters: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить мастера", callback_data="admin:masters_add")
    for m in masters[:20]:
        kb.button(text=f"🗑 {m['full_name']}", callback_data=f"admin:masters_del:{m['master_id']}")
    kb.button(text="⬅️ Назад", callback_data="nav:admin_panel")
    kb.adjust(1)
    return kb.as_markup()

@dp.callback_query(F.data == "admin:masters_manage")
async def admin_masters_manage(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    masters = repo.get_masters()
    text = "Управление мастерами:\n"
    if masters:
        text += "\n".join(f"{m['master_id']}: {m['full_name']} ({m['specialization']})" for m in masters[:20])
    else:
        text += "— мастеров пока нет —"
    await callback.message.answer(text, reply_markup=_admin_masters_manage_kb(masters))
    await callback.answer()

@dp.callback_query(F.data == "admin:masters_add")
async def admin_masters_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminMastersStates.adding_full_name)
    await callback.message.answer(
        "Введите ФИО мастера одним сообщением.\n"
        "Отмена: кнопка «Назад».",
        reply_markup=_back_to_admin_panel_kb(),
    )
    await callback.answer()

@dp.message(AdminMastersStates.adding_full_name, F.text)
async def admin_masters_add_full_name(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        return
    full_name = (message.text or "").strip()
    if not full_name:
        await message.answer("ФИО не должно быть пустым. Попробуйте ещё раз.")
        return
    await state.update_data(new_master_full_name=full_name)
    await state.set_state(AdminMastersStates.adding_specialization)
    await message.answer("Введите специализацию мастера одним сообщением:")

@dp.message(AdminMastersStates.adding_specialization, F.text)
async def admin_masters_add_specialization(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        return
    specialization = (message.text or "").strip()
    if not specialization:
        await message.answer("Специализация не должна быть пустой. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    full_name = str(data.get("new_master_full_name") or "").strip()
    if not full_name:
        await state.clear()
        await message.answer("Сессия добавления сброшена. Откройте снова «Мастера (добавить/удалить)».")
        return
    master_id = repo.add_master(full_name, specialization)
    await state.clear()
    masters = repo.get_masters()
    await message.answer(
        f"Мастер добавлен: {full_name} (ID={master_id}).",
        reply_markup=_admin_masters_manage_kb(masters),
    )

@dp.callback_query(F.data.startswith("admin:masters_del:"))
async def admin_masters_delete_confirm(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    master_id = int(callback.data.split(":")[2])
    masters = repo.get_masters()
    name = next((m["full_name"] for m in masters if int(m["master_id"]) == master_id), f"ID={master_id}")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"admin:masters_del_ok:{master_id}")
    kb.button(text="❌ Отмена", callback_data="admin:masters_manage")
    kb.adjust(1)
    await callback.message.answer(f"Удалить мастера {name}?", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin:masters_del_ok:"))
async def admin_masters_delete_do(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    master_id = int(callback.data.split(":")[2])
    done = repo.delete_master(master_id)
    masters = repo.get_masters()
    if done:
        await callback.message.answer("Мастер удалён.", reply_markup=_admin_masters_manage_kb(masters))
    else:
        await callback.message.answer(
            "Не удалось удалить мастера.\n"
            "Причина: у мастера есть записи (или мастер не найден). Сначала отмените/удалите записи.",
            reply_markup=_admin_masters_manage_kb(masters),
        )
    await callback.answer()

@dp.callback_query(F.data == "admin:pick_master_bookings")
async def admin_pick_master_bookings(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    masters = repo.get_masters()
    kb = InlineKeyboardBuilder()
    for m in masters[:20]:
        kb.button(text=m["full_name"], callback_data=f"admin:bookings:{m['master_id']}")
    kb.button(text="⬅️ Назад", callback_data="nav:admin_panel")
    kb.adjust(1)
    await callback.message.answer("Выберите мастера:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin:bookings:"))
async def admin_master_bookings_panel(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    master_id = int(callback.data.split(":")[2])
    rows = repo.get_master_bookings(master_id, include_cancelled=True)
    if not rows:
        await callback.message.answer("Записей у мастера нет.", reply_markup=_back_to_admin_panel_kb())
        await callback.answer()
        return
    kb = InlineKeyboardBuilder()
    for r in rows[:10]:
        if r["status"] == "confirmed":
            kb.button(text=f"Отменить #{r['booking_id']} • {r['work_date']} {r['start_time'][:5]}", callback_data=f"admin:cancel:{r['booking_id']}")
    kb.button(text="⬅️ Назад", callback_data="admin:pick_master_bookings")
    kb.adjust(1)
    text = "\n".join(
        f"#{r['booking_id']} | {r['client_name']} | {r['service_name']} | {r['work_date']} {r['start_time'][:5]} | {r['status']}"
        for r in rows[:10]
    )
    await callback.message.answer("Записи мастера:\n" + text, reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin:cancel:"))
async def admin_cancel_button(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    booking_id = int(callback.data.split(":")[2])
    done = repo.cancel_booking_as_admin(booking_id)
    await callback.message.answer("Запись отменена." if done else "Не удалось отменить запись.")
    await callback.answer()

@dp.callback_query(F.data == "admin:pick_master_free")
async def admin_pick_master_free(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    masters = repo.get_masters()
    kb = InlineKeyboardBuilder()
    for m in masters[:20]:
        kb.button(text=m["full_name"], callback_data=f"admin:free:{m['master_id']}")
    kb.button(text="⬅️ Назад", callback_data="nav:admin_panel")
    kb.adjust(1)
    await callback.message.answer("Выберите мастера:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin:free:"))
async def admin_free_for_master(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    master_id = int(callback.data.split(":")[2])
    await state.clear()
    await state.update_data(target_master_id=master_id)
    # reuse master flow (same callbacks) but allow delete any slot
    days = [(date.today() + timedelta(days=i)).isoformat() for i in range(14)]
    kb = InlineKeyboardBuilder()
    for d in days:
        kb.button(text=d, callback_data=f"free:day:{d}")
    kb.button(text="⬅️ Назад", callback_data="admin:pick_master_free")
    kb.adjust(2)
    await state.set_state(MasterPanelStates.choosing_free_day)
    await callback.message.answer("Выберите дату:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "admin:pick_master_services")
async def admin_pick_master_services(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    masters = repo.get_masters()
    kb = InlineKeyboardBuilder()
    for m in masters[:20]:
        kb.button(text=m["full_name"], callback_data=f"admin:services:{m['master_id']}")
    kb.button(text="⬅️ Назад", callback_data="nav:admin_panel")
    kb.adjust(1)
    await callback.message.answer("Выберите мастера:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin:services:"))
async def admin_services_for_master(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    master_id = int(callback.data.split(":")[2])
    services = repo.get_services()
    assigned = repo.get_master_services(master_id)
    kb = InlineKeyboardBuilder()
    for s in services[:20]:
        mark = "✅" if int(s["service_id"]) in assigned else "➕"
        kb.button(text=f"{mark} {s['name']}", callback_data=f"admin:svc_toggle:{master_id}:{s['service_id']}")
    kb.button(text="⬅️ Назад", callback_data="admin:pick_master_services")
    kb.adjust(1)
    await callback.message.answer("Услуги мастера (нажмите, чтобы включить/выключить):", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin:svc_toggle:"))
async def admin_service_toggle(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    # callback_data: admin:svc_toggle:{master_id}:{service_id}
    _, _, master_id_str, service_id_str = callback.data.split(":")
    master_id = int(master_id_str)
    service_id = int(service_id_str)
    assigned = repo.get_master_services(master_id)
    if service_id in assigned:
        repo.remove_master_service(master_id, service_id)
    else:
        repo.add_master_service(master_id, service_id)
    # refresh (send updated menu without fake callbacks)
    services = repo.get_services()
    assigned2 = repo.get_master_services(master_id)
    kb = InlineKeyboardBuilder()
    for s in services[:20]:
        mark = "✅" if int(s["service_id"]) in assigned2 else "➕"
        kb.button(text=f"{mark} {s['name']}", callback_data=f"admin:svc_toggle:{master_id}:{s['service_id']}")
    kb.button(text="⬅️ Назад", callback_data="admin:pick_master_services")
    kb.adjust(1)
    await callback.message.answer(
        "Услуги мастера (нажмите, чтобы включить/выключить):",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@dp.message(Command("master_bookings"))
async def master_bookings_cmd(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None and user.id not in ADMIN_IDS:
        await message.answer("Команда доступна мастеру (или админу).")
        return
    if master_id is None:
        await message.answer("Вы не привязаны как мастер. Админу: /admin_master_bookings|master_id")
        return
    rows = repo.get_master_bookings(master_id)
    if not rows:
        await message.answer("У вас пока нет записей.")
        return
    text = "\n".join(
        f"#{r['booking_id']} | {r['client_name']} | {r['service_name']} | "
        f"{r['work_date']} {r['start_time'][:5]} | {r['status']}"
        for r in rows
    )
    await message.answer(
        "Ваши записи (последние 50):\n"
        + text
        + "\n\nОтменить запись: /master_cancel|booking_id"
    )


@dp.message(Command("master_cancel"))
async def master_cancel(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    parts = (message.text or "").split("|")
    if len(parts) != 2:
        await message.answer("Формат: /master_cancel|booking_id")
        return
    _, booking_id = [p.strip() for p in parts]
    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None and user.id not in ADMIN_IDS:
        await message.answer("Команда доступна мастеру (или админу).")
        return
    if master_id is None and user.id in ADMIN_IDS:
        done = repo.cancel_booking_as_admin(int(booking_id))
        await message.answer("Запись отменена." if done else "Не удалось отменить запись.")
        return
    done = repo.cancel_booking_as_master(int(booking_id), int(master_id))
    await message.answer("Запись отменена." if done else "Не удалось отменить запись (возможно, это не ваша запись).")


@dp.message(Command("add_free"))
async def add_free_slot(message: Message) -> None:
    """
    Master: /add_free|YYYY-MM-DD|HH:MM|HH:MM
    Admin:  /add_free|master_id|YYYY-MM-DD|HH:MM|HH:MM
    """
    user = message.from_user
    if user is None:
        return
    parts = (message.text or "").split("|")
    if len(parts) not in (4, 5):
        await message.answer(
            "Формат для мастера: /add_free|YYYY-MM-DD|HH:MM|HH:MM\n"
            "Формат для админа: /add_free|master_id|YYYY-MM-DD|HH:MM|HH:MM"
        )
        return

    is_admin = user.id in ADMIN_IDS
    if len(parts) == 4:
        master_id = repo.get_master_id_by_telegram(user.id)
        if master_id is None:
            await message.answer("Команда доступна мастеру (или админу с master_id).")
            return
        _, work_date, start, end = [p.strip() for p in parts]
        target_master_id = int(master_id)
    else:
        if not is_admin:
            await message.answer("Команда с master_id доступна только администратору.")
            return
        _, master_id_str, work_date, start, end = [p.strip() for p in parts]
        target_master_id = int(master_id_str)

    try:
        slot_id = repo.add_free_slot(target_master_id, work_date, start, end)
    except ValueError as e:
        await message.answer(f"Не удалось добавить слот: {e}")
        return
    await message.answer(f"Свободное время добавлено. slot_id={slot_id}\nУдалить: /del_free|{slot_id}")


@dp.message(Command("del_free"))
async def del_free_slot(message: Message) -> None:
    """
    Master: /del_free|slot_id  (only own)
    Admin:  /del_free|slot_id  (any)
    """
    user = message.from_user
    if user is None:
        return
    parts = (message.text or "").split("|")
    if len(parts) != 2:
        await message.answer("Формат: /del_free|slot_id")
        return
    _, slot_id_str = [p.strip() for p in parts]
    slot_id = int(slot_id_str)

    if user.id in ADMIN_IDS:
        done = repo.delete_free_slot(slot_id, master_id=None)
        await message.answer("Слот удалён." if done else "Не удалось удалить слот (возможно, он занят или не найден).")
        return

    master_id = repo.get_master_id_by_telegram(user.id)
    if master_id is None:
        await message.answer("Команда доступна только мастеру или администратору.")
        return
    done = repo.delete_free_slot(slot_id, master_id=int(master_id))
    await message.answer("Слот удалён." if done else "Не удалось удалить слот (возможно, он занят/не ваш/не найден).")


@dp.message(Command("admin_master_bookings"))
async def admin_master_bookings(message: Message) -> None:
    """
    Admin: /admin_master_bookings|master_id
    """
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 2:
        await message.answer("Формат: /admin_master_bookings|master_id")
        return
    _, master_id_str = [p.strip() for p in parts]
    master_id = int(master_id_str)
    rows = repo.get_master_bookings(master_id)
    if not rows:
        await message.answer("Записей у мастера нет.")
        return
    text = "\n".join(
        f"#{r['booking_id']} | {r['client_name']} | {r['service_name']} | "
        f"{r['work_date']} {r['start_time'][:5]} | {r['status']}"
        for r in rows
    )
    await message.answer("Записи мастера (последние 50):\n" + text + "\n\nОтменить: /master_cancel|booking_id")


@dp.message(Command("assign_service"))
async def assign_service(message: Message) -> None:
    """
    Admin: /assign_service|master_id|service_id
    """
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 3:
        await message.answer("Формат: /assign_service|master_id|service_id")
        return
    _, master_id_str, service_id_str = [p.strip() for p in parts]
    repo.add_master_service(int(master_id_str), int(service_id_str))
    await message.answer("Услуга назначена мастеру.")


@dp.message(Command("unassign_service"))
async def unassign_service(message: Message) -> None:
    """
    Admin: /unassign_service|master_id|service_id
    """
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 3:
        await message.answer("Формат: /unassign_service|master_id|service_id")
        return
    _, master_id_str, service_id_str = [p.strip() for p in parts]
    done = repo.remove_master_service(int(master_id_str), int(service_id_str))
    await message.answer("Услуга снята с мастера." if done else "Связка мастер-услуга не найдена.")


@dp.message(Command("list_services"))
async def list_services(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    services = repo.get_services()
    if not services:
        await message.answer("Услуг нет.")
        return
    text = "\n".join(
        f"{s['service_id']}: {s['name']} ({s['duration_min']} мин / {s['price']} ₽)" for s in services
    )
    await message.answer("Услуги:\n" + text)


@dp.message(Command("list_masters"))
async def list_masters(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    masters = repo.get_masters()
    if not masters:
        await message.answer("Мастеров нет.")
        return
    text = "\n".join(f"{m['master_id']}: {m['full_name']} ({m['specialization']})" for m in masters)
    await message.answer("Мастера:\n" + text)


@dp.message(Command("add_service"))
async def add_service(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 4:
        await message.answer(
            "Формат: /add_service|Название|Длительность_мин|Цена\n"
            "Пример: /add_service|Детская стрижка|40|800"
        )
        return
    _, name, duration_str, price_str = [p.strip() for p in parts]
    try:
        repo.add_service(name, int(duration_str), int(price_str))
    except ValueError:
        await message.answer("Длительность и цена должны быть числами.")
        return
    except errors.UniqueViolation:
        repo.conn.rollback()
        await message.answer("Не удалось добавить услугу: проверьте уникальность названия.")
        return
    await message.answer("Услуга добавлена.")


@dp.message(Command("add_master"))
async def add_master(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 3:
        await message.answer("Формат: /add_master|ФИО|Специализация")
        return
    _, full_name, specialization = [p.strip() for p in parts]
    master_id = repo.add_master(full_name, specialization)
    await message.answer(f"Мастер добавлен с ID={master_id}")


@dp.message(Command("link_master"))
async def link_master(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 3:
        await message.answer("Формат: /link_master|master_id|telegram_id")
        return
    _, master_id, telegram_id = [p.strip() for p in parts]
    repo.set_master_telegram(int(master_id), int(telegram_id))
    await message.answer("Мастер привязан к Telegram ID.")


@dp.message(Command("add_schedule"))
async def add_schedule(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split("|")
    if len(parts) != 6:
        await message.answer(
            "Формат: /add_schedule|master_id|YYYY-MM-DD|HH:MM|HH:MM|slot_minutes\n"
            "Пример: /add_schedule|1|2026-05-01|10:00|18:00|60"
        )
        return
    _, master_id, work_date, start_time, end_time, slot_minutes = [p.strip() for p in parts]
    repo.add_schedule(int(master_id), work_date, start_time, end_time, int(slot_minutes))
    await message.answer("График мастера обновлен.")


@dp.message(Command("master_schedule"))
async def master_schedule(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    rows = repo.master_schedule(user.id)
    if not rows:
        await message.answer(
            "Ваш мастер-профиль не привязан к Telegram ID.\n"
            "Добавьте ваш telegram_id в таблицу master для использования этой функции."
        )
        return
    text = "\n".join(
        f"{r['work_date']} {r['start_time'][:5]} | {r['service_name']} | {r['status']}" for r in rows
    )
    await message.answer("Ваше расписание:\n" + text)


async def _setup_bot_hints(bot: Bot) -> None:
    """
    Подсказки для новых пользователей до первого сообщения в чат:
    краткое описание в профиле бота и пункты в меню команд (кнопка «/» у поля ввода).
    Reply-клавиатуру «Начать» Telegram показывает только после ответа бота — это ограничение клиента.
    """
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начать работу — меню и кнопка «Начать»"),
                BotCommand(command="my_bookings", description="Мои записи"),
            ],
            language_code="ru",
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open menu"),
                BotCommand(command="my_bookings", description="My bookings"),
            ],
            language_code="en",
        )
        await bot.set_my_short_description(
            short_description=(
                "Запись в салон: нажмите START внизу чата или откройте меню команд (/) и выберите «Начать работу»."
            ),
            language_code="ru",
        )
        await bot.set_my_short_description(
            short_description="Book online: tap START below or open the command menu (/) and pick Start.",
            language_code="en",
        )
    except Exception:
        pass


async def reminder_worker(bot: Bot) -> None:
    last_cleanup_at: datetime | None = None
    while True:
        try:
            # hourly cleanup for past free slots
            now = datetime.now()
            if last_cleanup_at is None or (now - last_cleanup_at) >= timedelta(hours=1):
                with contextlib.suppress(Exception):
                    repo.cleanup_past_free_slots()
                last_cleanup_at = now

            reminders = repo.pending_reminders(REMINDER_MINUTES)
            for row in reminders:
                text = (
                    "Напоминание о визите.\n"
                    f"Запись #{row['booking_id']}: {row['service_name']} у мастера {row['master_name']}\n"
                    f"Дата и время: {row['work_date']} {row['start_time'][:5]}"
                )
                await bot.send_message(row["client_telegram_id"], text)
                repo.mark_reminder_sent(row["booking_id"], row["client_telegram_id"], REMINDER_MINUTES)
        except Exception:
            repo.conn.rollback()
        await asyncio.sleep(60)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не указан BOT_TOKEN в переменных окружения.")
    if not DATABASE_URL:
        raise RuntimeError("Не указан DATABASE_URL в переменных окружения.")
    bot = Bot(BOT_TOKEN)
    await _setup_bot_hints(bot)
    reminder_task = asyncio.create_task(reminder_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reminder_task


if __name__ == "__main__":
    asyncio.run(main())
