# HairSalonTeleBot

Telegram-бот для записи клиентов в парикмахерскую. Курсовой проект (НИУ ВШЭ, «Бизнес-информатика», 2 курс).

Бот заменяет запись по телефону: клиент сам выбирает услугу, мастера, дату и свободное время, получает напоминание о визите и может отменить запись. Мастер видит своё расписание, администратор управляет услугами, мастерами и графиками работы.

## Роли и функции

**Клиент**
- регистрация по Telegram ID при `/start`;
- пошаговая запись: услуга → мастер → дата → свободный слот;
- просмотр и отмена своих записей;
- автоматическое напоминание о визите (по умолчанию за 24 часа).

**Мастер**
- просмотр своего расписания и записей;
- добавление и удаление свободного времени;
- отмена своих записей.

**Администратор**
- управление услугами и мастерами, назначение услуг мастерам;
- настройка графиков работы и длительности слотов;
- отчёт по записям, отмена любой записи.

## Модель данных

PostgreSQL, 8 таблиц. Схема описана в миграции [`0001_init_schema.sql`](migrations/0001_init_schema.sql).

```mermaid
erDiagram
    CLIENT ||--o{ BOOKING : "делает"
    SERVICE ||--o{ BOOKING : "заказана в"
    MASTER ||--o{ BOOKING : "выполняет"
    MASTER ||--o{ MASTER_SERVICE : "оказывает"
    SERVICE ||--o{ MASTER_SERVICE : "оказывается"
    MASTER ||--o{ MASTER_SCHEDULE : "работает по"
    MASTER_SCHEDULE ||--o{ TIME_SLOT : "делится на"
    TIME_SLOT ||--o| BOOKING : "занят"
    BOOKING ||--o{ NOTIFICATION : "порождает"

    CLIENT {
        bigint telegram_id PK
        text name
        text phone
        timestamptz created_at
    }
    SERVICE {
        bigserial service_id PK
        text name UK
        int duration_min
        int price
    }
    MASTER {
        bigserial master_id PK
        text full_name
        text specialization
        bigint telegram_id UK
    }
    MASTER_SERVICE {
        int master_id PK, FK
        int service_id PK, FK
    }
    MASTER_SCHEDULE {
        bigserial schedule_id PK
        int master_id FK
        date work_date
        time start_time
        time end_time
    }
    TIME_SLOT {
        bigserial slot_id PK
        int schedule_id FK
        time start_time
        time end_time
        bool is_booked
    }
    BOOKING {
        bigserial booking_id PK
        bigint client_telegram_id FK
        int service_id FK
        int master_id FK
        int slot_id FK, UK
        text status
        timestamptz created_at
    }
    NOTIFICATION {
        bigserial notification_id PK
        int booking_id FK
        bigint recipient_telegram_id
        text type
        timestamptz sent_at
    }
```

Ключевые решения:
- связь «многие ко многим» между мастерами и услугами вынесена в таблицу `master_service`;
- рабочий день мастера (`master_schedule`) делится на слоты (`time_slot`), а уникальность `slot_id` в `booking` не даёт записать двух клиентов на одно время;
- статус записи ограничен проверкой: `confirmed`, `completed`, `cancelled_client`, `cancelled_master`;
- отправленные напоминания фиксируются в `notification`, чтобы не дублировать их.

## Запуск

1. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
2. Задайте переменные окружения:
   - `BOT_TOKEN` — токен Telegram-бота (получить у [@BotFather](https://t.me/BotFather));
   - `DATABASE_URL` — строка подключения к PostgreSQL, например `postgresql://user:pass@localhost:5432/hairsalon`;
   - `ADMIN_IDS` — Telegram ID администраторов через запятую, например `12345,67890`;
   - `REMINDER_MINUTES` — за сколько минут до визита отправлять напоминание (необязательно, по умолчанию `1440`).
3. Примените миграции и запустите бота:
   ```bash
   python migrate.py
   python main.py
   ```

## Команды

| Команда | Кто | Что делает |
|---|---|---|
| `/start` | все | главное меню |
| `/my_bookings` | клиент | мои записи |
| `/master_schedule` | мастер | расписание мастера |
| `/master_bookings` | мастер, админ | записи мастера |
| `/master_cancel\|booking_id` | мастер, админ | отмена записи (мастер — своей, админ — любой) |
| `/add_free\|YYYY-MM-DD\|HH:MM\|HH:MM` | мастер | добавить свободное время |
| `/add_free\|master_id\|YYYY-MM-DD\|HH:MM\|HH:MM` | админ | добавить свободное время мастеру |
| `/del_free\|slot_id` | мастер, админ | удалить свободный слот |
| `/add_service\|Название\|Длительность_мин\|Цена` | админ | добавить услугу |
| `/add_master\|ФИО\|Специализация` | админ | добавить мастера |
| `/assign_service\|master_id\|service_id` | админ | назначить услугу мастеру |
| `/unassign_service\|master_id\|service_id` | админ | снять услугу с мастера |
| `/list_services` | админ | список услуг с `service_id` |
| `/list_masters` | админ | список мастеров с `master_id` |
| `/link_master\|master_id\|telegram_id` | админ | привязать мастера к Telegram |
| `/add_schedule\|master_id\|YYYY-MM-DD\|HH:MM\|HH:MM\|slot_minutes` | админ | настроить график работы |

## Миграции

- Миграции лежат в папке [`migrations`](migrations): схема, тестовые данные и их откат (`*.down.sql`).
- Применить новые миграции: `python migrate.py`
- Перейти к версии: `python migrate.py --to 0001_init_schema`
- Посмотреть план без изменений: `python migrate.py --to 0001_init_schema --dry-run`
- Применённые версии хранятся в таблице `schema_migrations`.

## Стек

Python · aiogram 3 · PostgreSQL · psycopg2
