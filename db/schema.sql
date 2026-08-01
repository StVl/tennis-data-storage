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
