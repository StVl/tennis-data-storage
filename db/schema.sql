-- =============================================================================
-- schema.sql — полная схема БД теннисного приложения (чистый Postgres 14+).
--
-- Поднимает с нуля ВСЕ таблицы, типы, view и статичные справочники (tours,
-- rounds). Данных не содержит — контент заливается scripts/migrate_data.py.
--
-- Применение:
--   psql "$DATABASE_URL" -f db/schema.sql
--   # или: python3 scripts/apply_schema.py (использует psycopg)
--
-- Скрипт идемпотентен: можно запускать повторно, существующие объекты
-- не пересоздаются и данные не теряются.
-- Документация схемы: docs/db_schema.md
-- =============================================================================

begin;

-- -----------------------------------------------------------------------------
-- Enum-типы (закрытые списки; расширение: ALTER TYPE ... ADD VALUE)
-- -----------------------------------------------------------------------------

do $$ begin
  create type surface_t as enum ('hard','clay','grass','carpet');
exception when duplicate_object then null; end $$;

do $$ begin
  create type hand_t as enum ('right','left');
exception when duplicate_object then null; end $$;

do $$ begin
  create type gender_t as enum ('m','f');
exception when duplicate_object then null; end $$;

do $$ begin
  create type discipline_t as enum ('singles','doubles','mixed');
exception when duplicate_object then null; end $$;

do $$ begin
  create type match_status_t as enum ('scheduled','live','completed','cancelled');
exception when duplicate_object then null; end $$;

do $$ begin
  create type match_outcome_t as enum ('normal','retirement','walkover','default');
exception when duplicate_object then null; end $$;

do $$ begin
  create type entry_status_t as enum ('main','qualifying','withdrawn','alternate');
exception when duplicate_object then null; end $$;

do $$ begin
  create type push_token_kind_t as enum ('apns','apns_live_activity','fcm');
exception when duplicate_object then null; end $$;

do $$ begin
  create type iap_environment_t as enum ('production','sandbox');
exception when duplicate_object then null; end $$;

-- -----------------------------------------------------------------------------
-- Служебное: триггер updated_at
-- -----------------------------------------------------------------------------

create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at := now();
  return new;
end $$ language plpgsql;

-- -----------------------------------------------------------------------------
-- Справочники
-- -----------------------------------------------------------------------------

create table if not exists tours (
  code      text primary key,
  name      text not null,
  metadata  jsonb not null default '{}'
);

create table if not exists countries (
  code        char(2) primary key,
  name        jsonb not null,           -- {"ru":"Италия","en":"Italy"}
  flag_emoji  text
);

create table if not exists play_styles (
  id          bigint generated always as identity primary key,
  slug        text not null unique,
  name        jsonb not null,            -- локализуемое: {"en":"...","ru":"..."}
  description jsonb not null default '{}'
);

create table if not exists rounds (
  code        text primary key,          -- 'R128'..'F', 'RR', 'R1'..'R4', 'Q1'..'Q3'
  label       jsonb not null,
  sort_order  smallint not null          -- финал старше всех: F=100
);

-- -----------------------------------------------------------------------------
-- Контент-ядро
-- -----------------------------------------------------------------------------

create table if not exists players (
  id                 bigint generated always as identity primary key,
  slug               text not null unique,
  first_name         text,
  last_name          text,
  display_name       text not null,
  gender             gender_t not null default 'm',
  photo_url          text,
  birth_date         date,
  hand               hand_t,
  height_cm          smallint,
  country_code       char(2) references countries(code),
  birth_country_code char(2) references countries(code),
  play_style_id      bigint references play_styles(id),
  traits             jsonb not null default '{}',   -- {"ru":["стабильность",...]}
  pro_tip            jsonb not null default '{}',
  links              jsonb not null default '{}',
  attributes         jsonb not null default '{}',   -- точка расширения
  is_tracked         boolean not null default false, -- true = ростер приложения
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

do $$ begin
  create trigger players_updated_at before update on players
    for each row execute function set_updated_at();
exception when duplicate_object then null; end $$;

create table if not exists ranking_snapshots (
  id            bigint generated always as identity primary key,
  player_id     bigint not null references players(id) on delete cascade,
  tour_code     text not null references tours(code),
  snapshot_date date not null,
  rank          integer not null,
  points        integer,     -- очки рейтинга (52 недели)
  race_points   integer,     -- Race (очки текущего сезона)
  unique (player_id, tour_code, snapshot_date)
);

create index if not exists ranking_snapshots_latest_idx
  on ranking_snapshots (player_id, tour_code, snapshot_date desc);

create table if not exists tournaments (
  id              bigint generated always as identity primary key,
  slug            text not null unique,
  name            text not null,
  tour_code       text not null references tours(code) default 'atp',
  description     jsonb not null default '{}',
  location        text,
  country_code    char(2) references countries(code),
  logo_url        text,
  default_surface surface_t,
  conditions      jsonb not null default '{}',  -- {"altitude_m":..,"rain_risk":..}
  metadata        jsonb not null default '{}'
);

create table if not exists tournament_editions (
  id             bigint generated always as identity primary key,
  tournament_id  bigint not null references tournaments(id) on delete cascade,
  year           smallint not null,
  slug           text not null unique,
  start_date     date not null,
  end_date       date not null,
  surface        surface_t not null,
  discipline     discipline_t not null default 'singles',
  draw_size      smallint,
  prize_money    numeric(12,0),
  prize_currency char(3) default 'USD',
  champion_id    bigint references players(id),
  runner_up_id   bigint references players(id),
  metadata       jsonb not null default '{}',
  unique (tournament_id, year, discipline),
  check (end_date >= start_date)
);

create table if not exists tournament_entries (
  edition_id  bigint not null references tournament_editions(id) on delete cascade,
  player_id   bigint not null references players(id) on delete cascade,
  seed        smallint,
  status      entry_status_t not null default 'main',
  primary key (edition_id, player_id)
);

create index if not exists tournament_entries_player_idx
  on tournament_entries (player_id);

create table if not exists matches (
  id           bigint generated always as identity primary key,
  edition_id   bigint not null references tournament_editions(id) on delete cascade,
  round_code   text not null references rounds(code),
  discipline   discipline_t not null default 'singles',
  scheduled_at timestamptz,
  court        text,
  status       match_status_t not null default 'scheduled',
  winner_side  smallint check (winner_side in (1,2)),
  outcome      match_outcome_t,
  live_state   jsonb not null default '{}',
  bracket_pos  smallint,
  import_key   text unique,   -- legacy-id для идемпотентной миграции
  metadata     jsonb not null default '{}',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  -- у нормально завершённого матча обязан быть победитель; для walkover
  -- в исторических данных победитель бывает неизвестен — допускаем
  check (status <> 'completed' or winner_side is not null or outcome <> 'normal')
);

do $$ begin
  create trigger matches_updated_at before update on matches
    for each row execute function set_updated_at();
exception when duplicate_object then null; end $$;

create index if not exists matches_edition_idx  on matches (edition_id, round_code);
create index if not exists matches_schedule_idx on matches (scheduled_at)
  where status in ('scheduled','live');
create index if not exists matches_live_idx     on matches (id)
  where status = 'live';

create table if not exists match_participants (
  match_id  bigint not null references matches(id) on delete cascade,
  side      smallint not null check (side in (1,2)),
  slot      smallint not null default 1 check (slot in (1,2)),
  player_id bigint not null references players(id),
  primary key (match_id, side, slot)
);

create index if not exists match_participants_player_idx
  on match_participants (player_id, match_id);

create table if not exists match_sets (
  match_id              bigint not null references matches(id) on delete cascade,
  set_no                smallint not null check (set_no between 1 and 5),
  side1_games           smallint not null,
  side2_games           smallint not null,
  tiebreak_loser_points smallint,   -- '7-6(4)' -> 4; null = тай-брейка не было/неизвестно
  primary key (match_id, set_no)
);

-- -----------------------------------------------------------------------------
-- Пользовательский блок (чистый Postgres, без Supabase auth.users:
-- аноним создаётся строкой в profiles, привязка email/Apple — позже
-- заполнением полей той же строки)
-- -----------------------------------------------------------------------------

create table if not exists profiles (
  user_id    uuid primary key default gen_random_uuid(),
  email      text unique,       -- опциональная привязка
  apple_sub  text unique,       -- subject из Sign in with Apple
  device_id  text,              -- identifierForVendor при регистрации (диагностика)
  settings   jsonb not null default '{}',
  created_at timestamptz not null default now()
);
alter table profiles add column if not exists device_id text;

-- Opaque bearer-токены анонимных пользователей. Храним только sha256(token);
-- сам токен живёт в Keychain устройства. Несколько токенов на пользователя —
-- задел под несколько устройств после привязки email/Apple.
create table if not exists auth_tokens (
  token_hash   bytea primary key,   -- sha256 от токена
  user_id      uuid not null references profiles(user_id) on delete cascade,
  created_at   timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);
create index if not exists auth_tokens_user_idx on auth_tokens (user_id);

create table if not exists follows (
  user_id    uuid not null references profiles(user_id) on delete cascade,
  player_id  bigint not null references players(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, player_id)
);

create index if not exists follows_player_idx on follows (player_id);

create table if not exists push_tokens (
  id           bigint generated always as identity primary key,
  user_id      uuid not null references profiles(user_id) on delete cascade,
  device_id    text not null,
  kind         push_token_kind_t not null,
  token        text not null,
  environment  iap_environment_t not null default 'production',
  context      jsonb not null default '{}',
  created_at   timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  unique (token, kind)
);

create index if not exists push_tokens_user_idx on push_tokens (user_id, kind);

create table if not exists iap_purchases (
  id                      bigint generated always as identity primary key,
  user_id                 uuid not null references profiles(user_id) on delete cascade,
  product_id              text not null,
  original_transaction_id text not null,
  transaction_id          text not null unique,
  purchased_at            timestamptz not null,
  expires_at              timestamptz,
  price_amount            numeric(10,2),
  price_currency          char(3),
  environment             iap_environment_t not null default 'production',
  is_revoked              boolean not null default false,
  raw_transaction         jsonb not null default '{}',
  created_at              timestamptz not null default now(),
  unique (original_transaction_id, transaction_id)
);

create index if not exists iap_user_active_idx
  on iap_purchases (user_id, expires_at desc);

-- -----------------------------------------------------------------------------
-- Live-статусы: ingest внешнего источника (tennis-backend)
--
-- Пишет и читает эти таблицы только сервис tennis-backend. В контент-ядре он
-- меняет ровно один столбец — matches.status — и всегда под guard'ом по
-- текущему значению; счёт, победитель и сетка остаются за migrate_data.py.
-- Приватное состояние ingest'а живёт здесь, а не колонками в matches, именно
-- поэтому: чужое состояние в общем контракте — это гарантированный дрейф.
--
-- ВСЕ FK на контент-ядро объявлены on delete cascade. Это обязательно: без
-- каскада служебная таблица начинает блокировать чужие удаления — пайплайн не
-- смог бы удалить матч или розыгрыш (удаление розыгрыша каскадится в matches).
-- -----------------------------------------------------------------------------

-- Сопоставление id внешних источников с нашими сущностями. Связь N:1: у одной
-- сущности бывает несколько внешних ключей (источник несёт расщеплённые
-- личности игроков, а туры и разряды одного турнира — это разные id).
-- entity_id СОЗНАТЕЛЬНО без FK: висячая строка маппинга восстановима и
-- перевыводима из слага, а FK, ломающий импорт пайплайна, — нет.
create table if not exists external_ids (
  source       text   not null,          -- 'livetennisapi'
  entity_type  text   not null,          -- 'player' | 'edition' | 'match'
  external_key text   not null,
  entity_id    bigint not null,
  confirmed_at timestamptz,              -- null = машинная догадка, ждёт ревью
  primary key (source, entity_type, external_key)
);
create index if not exists external_ids_entity_idx
  on external_ids (source, entity_type, entity_id);

-- Одна строка на цикл любого из джобов ingest'а. Единственное окно в то, почему
-- карточка появилась или не появилась, поэтому строка пишется и на пропуске.
create table if not exists live_ingest_runs (
  id                      bigserial   primary key,
  -- 'live' | 'live-schedule' | 'live-push'. Без этого поля нельзя ни посчитать
  -- квоту (запросы всех джобов тратят один лимит), ни посчитать пропуски
  -- (прогон обновления расписания отсутствием матча не является).
  job                     text        not null,
  source                  text        not null,
  started_at              timestamptz not null,
  finished_at             timestamptz,
  rows_parsed             int,
  rows_in_scope           int,
  rows_matched            int,
  rows_dropped_unresolved int,
  -- инкрементируется по ходу цикла, а не на закрытии: редеплой посреди цикла
  -- иначе теряет уже потраченные запросы, а квота — связывающее ограничение
  requests_made           int         not null default 0,
  mode                    text,       -- active | watching | stale_safe | asleep
  skipped_reason          text,
  error                   text
);
create index if not exists live_ingest_runs_job_idx
  on live_ingest_runs (job, started_at desc);
create index if not exists live_ingest_runs_day_idx
  on live_ingest_runs (started_at);

-- Append-only журнал того, что сказал источник. Статус матча выводится из этой
-- истории, а не пишется напрямую из ответа опроса.
create table if not exists live_observations (
  id           bigserial   primary key,
  -- прогон, а не только время: прогоны строго упорядочены, поэтому «три
  -- пропуска подряд» — точное утверждение, а не эвристика по таймстемпам
  run_id       bigint      not null references live_ingest_runs(id) on delete cascade,
  match_id     bigint      not null references matches(id) on delete cascade,
  source       text        not null,
  state        text        not null,     -- 'on_court' | 'finished' | 'suspended'
  event_status text,                     -- сырое значение источника: его enum дрейфует
  observed_at  timestamptz not null
);
create index if not exists live_observations_match_idx
  on live_observations (match_id, observed_at desc);

-- Какие матчи ingest держит live прямо сейчас. Полностью выводима из журнала,
-- то есть материализация, а не второй источник истины.
create table if not exists live_flags (
  match_id         bigint         primary key references matches(id) on delete cascade,
  source           text           not null,   -- 'livetennisapi' | 'dev'
  external_key     text,
  state            text           not null,   -- 'on_court' | 'suspended'
  prior_status     match_status_t not null,   -- что восстановить на выходе
  flipped_at       timestamptz    not null,
  -- последний прогон, в котором матч был в борте; null = ручной флип
  last_seen_run_id bigint         references live_ingest_runs(id)
);
create index if not exists live_flags_flipped_idx on live_flags (flipped_at);

-- Расписание наших игроков, как его отдаёт источник. Кэш, а не истина: окно
-- опроса выводится отсюда, а не из v_tournament_editions — в календаре 25
-- розыгрышей против 60+ событий тура, и гейт по нему уводил бы поллер
-- в тишину на месяцы, причём молча.
create table if not exists live_schedule (
  source         text        not null,
  external_key   text        not null,   -- id матча у источника
  tournament_key text,                   -- id турнира у источника
  scheduled_at   timestamptz,            -- null = порядок игры не опубликован
  player_keys    text[]      not null,
  round_code     text,
  tournament     text,
  refreshed_at   timestamptz not null,
  primary key (source, external_key)
);
create index if not exists live_schedule_scheduled_idx on live_schedule (scheduled_at);

-- Очередь ревью, а не лог ошибок: сюда попадает только то, где хотя бы один
-- игрок наш. payload — уже очищенная проекция без полей счёта.
create table if not exists live_unmatched (
  id          bigserial   primary key,
  source      text        not null,
  payload     jsonb       not null,
  reason      text        not null,
  observed_at timestamptz not null,
  constraint live_unmatched_reason_check check (reason in (
    'no_match_row', 'ambiguous', 'one_side_unresolved',
    'edition_unmapped', 'round_unmapped'
  ))
);
create index if not exists live_unmatched_observed_idx on live_unmatched (observed_at desc);

-- Outbox переходов. Не pg_notify: Railway редеплоится на каждый push, LISTEN
-- в этот момент умирает, и отправленное в зазор исчезает без следа.
create table if not exists live_events (
  id          bigserial   primary key,
  match_id    bigint      not null references matches(id) on delete cascade,
  event       text        not null,
  payload     jsonb       not null default '{}',
  reason      text,
  created_at  timestamptz not null,
  claimed_at  timestamptz,              -- когда пробовали в последний раз
  consumed_at timestamptz,
  attempts    int         not null default 0,
  last_error  text,
  constraint live_events_event_check check (event in (
    'live', 'finished', 'suspended', 'resumed'
  ))
);
create index if not exists live_events_pending_idx
  on live_events (created_at) where consumed_at is null;

-- Негативный кэш ленивого резолвера: без него неизвестный id игрока
-- запрашивался бы каждый цикл вечно, а часть таких id у источника
-- отсутствует в принципе.
create table if not exists live_resolve_attempts (
  source        text        not null,
  external_key  text        not null,
  last_tried_at timestamptz not null,
  attempts      int         not null default 1,
  primary key (source, external_key)
);

-- Сессии Live Activity: одна карточка на пользователя и матч.
create table if not exists live_activity_sessions (
  id           bigserial   primary key,
  user_id      uuid        not null references profiles(user_id) on delete cascade,
  match_id     bigint      not null references matches(id) on delete cascade,
  update_token text,                     -- токен уже запущенной активности
  phase        text        not null,
  started_at   timestamptz not null,
  ended_at     timestamptz,
  constraint live_activity_sessions_phase_check
    check (phase in ('starting', 'active', 'ended'))
);
-- Единственное, что не даёт отправить второй push-to-start на тот же матч:
-- слот занимается ДО отправки и освобождается, если отправка не удалась.
create unique index if not exists live_activity_sessions_open_idx
  on live_activity_sessions (user_id, match_id) where ended_at is null;
create index if not exists live_activity_sessions_match_idx
  on live_activity_sessions (match_id) where ended_at is null;

-- -----------------------------------------------------------------------------
-- View: всё вычислимое не хранится
-- -----------------------------------------------------------------------------

-- Текущий рейтинг + дельта к предыдущему снапшоту (положительная = поднялся)
create or replace view v_current_rankings as
select player_id, tour_code, snapshot_date, rank, points, race_points,
       prev_rank - rank as delta_vs_prev
from (
  select rs.*,
         lead(rank) over (partition by player_id, tour_code
                          order by snapshot_date desc) as prev_rank,
         row_number() over (partition by player_id, tour_code
                            order by snapshot_date desc) as rn
  from ranking_snapshots rs
) t
where rn = 1;

-- Розыгрыши со статусом, вычисленным из дат
create or replace view v_tournament_editions as
select te.*,
       case when current_date < te.start_date then 'upcoming'
            when current_date > te.end_date   then 'completed'
            else 'ongoing' end as status
from tournament_editions te;

-- Матч «глазами игрока»: воспроизводит старую JSON-модель
-- (result won/lost, счёт с инверсией по стороне)
create or replace view v_player_matches as
select m.id as match_id,
       mp.player_id,
       mp.side,
       opp.player_id as opponent_id,          -- null = TBD
       m.edition_id, m.round_code, m.scheduled_at,
       m.status, m.outcome, m.court,
       case when m.winner_side is null then null
            when m.winner_side = mp.side then 'won' else 'lost' end as result,
       (select string_agg(
                 case when mp.side = 1
                      then s.side1_games || '-' || s.side2_games
                      else s.side2_games || '-' || s.side1_games end
                 || coalesce('(' || s.tiebreak_loser_points || ')', ''),
                 ', ' order by s.set_no)
        from match_sets s where s.match_id = m.id) as score_text
from matches m
join match_participants mp on mp.match_id = m.id
left join match_participants opp
       on opp.match_id = m.id and opp.side <> mp.side and opp.slot = mp.slot;

-- Действующие права доступа (подписки)
create or replace view v_active_entitlements as
select user_id, product_id, max(expires_at) as expires_at
from iap_purchases
where not is_revoked and (expires_at is null or expires_at > now())
group by user_id, product_id;

-- -----------------------------------------------------------------------------
-- Статичные справочные данные (часть схемы: без них FK не работают)
-- -----------------------------------------------------------------------------

insert into tours (code, name) values ('atp', 'ATP Tour')
on conflict (code) do nothing;

insert into rounds (code, label, sort_order) values
  ('Q1',   '{"ru":"1-й круг квалификации","en":"Qualifying Round 1"}', 10),
  ('Q2',   '{"ru":"2-й круг квалификации","en":"Qualifying Round 2"}', 11),
  ('Q3',   '{"ru":"3-й круг квалификации","en":"Qualifying Round 3"}', 12),
  ('R128', '{"ru":"1-й круг","en":"Round of 128"}',                    20),
  ('R64',  '{"ru":"2-й круг","en":"Round of 64"}',                     30),
  ('R32',  '{"ru":"3-й круг","en":"Round of 32"}',                     40),
  ('R16',  '{"ru":"4-й круг","en":"Round of 16"}',                     50),
  ('R1',   '{"ru":"1-й круг","en":"Round 1"}',                         21),
  ('R2',   '{"ru":"2-й круг","en":"Round 2"}',                         31),
  ('R3',   '{"ru":"3-й круг","en":"Round 3"}',                         41),
  ('R4',   '{"ru":"4-й круг","en":"Round 4"}',                         51),
  ('RR',   '{"ru":"Групповой этап","en":"Round Robin"}',               55),
  ('QF',   '{"ru":"1/4 финала","en":"Quarterfinal"}',                  80),
  ('SF',   '{"ru":"1/2 финала","en":"Semifinal"}',                     90),
  ('F',    '{"ru":"Финал","en":"Final"}',                             100)
on conflict (code) do nothing;

commit;
