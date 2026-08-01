# API теннисного микросервиса — дизайн (уровень CRUD)

Бэкенд для [tennis-tracker](https://github.com/StVl/tennis-tracker) (iOS-приложение + виджет).
База: Postgres на Railway, схема — [db/schema.md](db_schema.md).
Задача документа: какие запросы за что отвечают и на какие таблицы/view ложатся.

## 0. Принципы

- REST, JSON, префикс версии `/v1`. Идентификаторы в URL — **слаги** (`sinner`,
  `wimbledon_2026`), surrogate id наружу не светим.
- Три зоны доступа:
  - **public read** — контент (игроки, матчи, турниры): без авторизации, кэшируемо (`Cache-Control`, ETag);
  - **user** — всё под `/users/me/*`: Bearer-токен пользователя;
  - **ingest** — запись контента пайплайном обновления данных: сервисный ключ (`X-Service-Key`), приложению недоступно.
- Схема «CRUD-ресурсы + 2 композитных read-эндпоинта»: экраны приложения и виджет
  получают готовую выдачу одним запросом (это замена нынешнего config.json),
  а CRUD-ресурсы — строительные блоки и админка.
- Пагинация: `?limit=&offset=`, по умолчанию limit=50. Время — ISO 8601 с таймзоной.
- Ошибки: `{ "error": { "code": "not_found", "message": "..." } }` + соответствующий HTTP-статус.

---

## 1. Auth / пользователь

Анонимная модель: приложение при первом запуске создаёт пользователя и получает
долгоживущий токен. Позже к тому же пользователю привязывается email/Apple.

| Метод | Путь | Что делает | Таблицы |
|---|---|---|---|
| `POST` | `/v1/users` | создать анонимного пользователя; тело `{device_id}`; ответ `{user_id, token}` | `profiles` insert |
| `GET` | `/v1/users/me` | профиль + счётчики (подписок, статус подписки) | `profiles`, `v_active_entitlements` |
| `PATCH` | `/v1/users/me` | привязать `email` / `apple_sub`, обновить `settings` | `profiles` update |
| `DELETE` | `/v1/users/me` | удаление аккаунта (App Store требование) | `profiles` delete (cascade всё) |

Токен: подписанный JWT с `user_id` (или opaque-токен в отдельной таблице сессий —
решим при реализации; на дизайн API не влияет).

## 2. Подписки (follows)

| Метод | Путь | Что делает | Таблицы |
|---|---|---|---|
| `GET` | `/v1/users/me/follows` | список слагов подписанных игроков | `follows` |
| `PUT` | `/v1/users/me/follows/{player_slug}` | подписаться (идемпотентно) | `follows` upsert |
| `DELETE` | `/v1/users/me/follows/{player_slug}` | отписаться (идемпотентно) | `follows` delete |
| `PUT` | `/v1/users/me/follows` | заменить весь список разом: `{player_slugs: [...]}` — под онбординг, где выбирают сразу несколько | `follows` delete+insert |

## 3. Пуш-токены и покупки

| Метод | Путь | Что делает | Таблицы |
|---|---|---|---|
| `PUT` | `/v1/users/me/push-tokens` | upsert токена: `{device_id, kind, token, context}`; kind: `apns` / `apns_live_activity` | `push_tokens` upsert по `(token, kind)` |
| `DELETE` | `/v1/users/me/push-tokens/{token}` | отозвать токен (logout/отключение) | `push_tokens` delete |
| `POST` | `/v1/users/me/purchases` | тело `{signed_transaction}` (JWS из StoreKit 2); сервер верифицирует у Apple, пишет строку; идемпотентно по `transaction_id` — этим же обрабатывается restore | `iap_purchases` upsert |
| `GET` | `/v1/users/me/entitlements` | действующие права: `[{product_id, expires_at}]` | `v_active_entitlements` |

## 4. Контент: игроки

| Метод | Путь | Что делает | Таблицы/view |
|---|---|---|---|
| `GET` | `/v1/players` | список ростера (`?tracked=true` по умолчанию); поля карточки сетки: slug, имя, фото, рейтинг, тренд, стиль. `?search=` по имени | `players` ⋈ `v_current_rankings` ⋈ `play_styles` |
| `GET` | `/v1/players/{slug}` | полный профиль: биография (рука, рост, страна, дата рождения), стиль с описанием, traits, pro_tip, links, рейтинг + дельта + очки | то же + `countries` |
| `GET` | `/v1/players/{slug}/matches` | история и расписание: `?status=completed&limit=3` → «Last matches»; `?status=scheduled,live` → «Upcoming». Каждый элемент — «глазами игрока»: оппонент, result, score | `v_player_matches` ⋈ `matches` ⋈ `tournament_editions` |
| `GET` | `/v1/players/{slug}/tournaments` | турниры игрока: `?status=upcoming,ongoing` → «Next Tournament» | `tournament_entries` ⋈ `v_tournament_editions` |
| `GET` | `/v1/players/{slug}/ranking-history` | снапшоты рейтинга (график на будущее) | `ranking_snapshots` |
| `GET` | `/v1/players/{a}/h2h/{b}` | личные встречи: `{wins, losses, matches: [...]}` — поле `headToHeadBeforeMatch` из README вычисляется этим запросом | `match_participants` self-join по завершённым матчам |

## 5. Контент: турниры и рейтинг

| Метод | Путь | Что делает | Таблицы/view |
|---|---|---|---|
| `GET` | `/v1/tournaments` | розыгрыши: `?status=upcoming|ongoing|completed` (три списка из требований), `?year=2026` | `v_tournament_editions` ⋈ `tournaments` |
| `GET` | `/v1/tournaments/{edition_slug}` | карточка розыгрыша: описание, локация, лого, покрытие, условия, даты, призовой, чемпион/финалист, заявленные игроки | + `tournament_entries` ⋈ `players` |
| `GET` | `/v1/tournaments/{edition_slug}/draw` | сетка: матчи, сгруппированные по раундам в порядке `rounds.sort_order`, внутри — по `bracket_pos` | `matches` ⋈ `match_participants` ⋈ `match_sets` ⋈ `rounds` |
| `GET` | `/v1/tournaments/{tournament_slug}/history` | прошлые розыгрыши: год → чемпион/финалист | `tournament_editions` по `tournament_id` |
| `GET` | `/v1/rankings` | текущий топ: `?limit=100`, `?tour=atp` | `v_current_rankings` ⋈ `players` |
| `GET` | `/v1/play-styles` | справочник стилей | `play_styles` |

## 6. Контент: матчи

| Метод | Путь | Что делает | Таблицы/view |
|---|---|---|---|
| `GET` | `/v1/matches` | фильтры: `?date=2026-08-01` / `?date=today`, `?status=live`, `?player=slug`, `?edition=slug`. Сортировки: `?sort=start_at` (default) / `?sort=best_rank` — минимальный текущий рейтинг участников, это сортировка колонки TODAY в виджете | `matches` ⋈ `match_participants` ⋈ `v_current_rankings` |
| `GET` | `/v1/matches/{id}` | матч целиком: участники, сеты, live_state, корт, раунд, турнир | + `match_sets` |

Форма матча в ответе — нейтральная (не «глазами игрока»):

```json
{
  "id": 501, "edition": "wimbledon_2026", "round": "F",
  "scheduled_at": "2026-07-12T15:00:00+01:00", "court": "Centre Court",
  "status": "completed", "surface": "grass",
  "sides": [
    {"side": 1, "players": [{"slug": "sinner", "name": "Jannik Sinner", "rank": 1}]},
    {"side": 2, "players": [{"slug": "zverev", "name": "Alexander Zverev", "rank": 2}]}
  ],
  "winner_side": 1, "outcome": "normal",
  "sets": [[6,7,7],[7,6,2],[6,3,null],[6,4,null]],
  "score_text": "6-7(7), 7-6(2), 6-3, 6-4",
  "live": null
}
```

## 7. Композитные read-эндпоинты (замена config.json)

Два экрана, которые приложение и виджет должны получать одним запросом.

> Реализация: до появления серверных follows подписки передаются параметром
> `?player_ids=sinner,alcaraz` (клиент берёт их из App Group), поэтому пути —
> `/v1/home` и `/v1/widget` без `users/me`. Когда появится пользовательский блок,
> добавятся авторизованные варианты `/v1/users/me/*` поверх той же логики.
> Детальный экран игрока собирается одним запросом через
> `GET /v1/players/{slug}?include=last_matches,next_match,next_tournament`.

### `GET /v1/home` — главный экран

Отдаёт обе секции сразу:

```json
{
  "your_season": [
    {
      "player": {"slug": "sinner", "name": "...", "photo_url": "...",
                 "rank": 1, "rank_delta": 0, "play_style": "Aggressive Baseliner"},
      "next_match": { ...форма матча... },          // null если нет
      "next_tournament": { ...edition... }           // показывается если нет матча
    }
  ],
  "all_players": [ {"slug": "...", "name": "...", "photo_url": "...", "followed": false} ]
}
```

Ложится на: `follows` → `players` ⋈ `v_current_rankings`; «ближайший матч» — первый
`scheduled/live` из `match_participants` ⋈ `matches` по `scheduled_at`; «ближайший
турнир» — первый upcoming из `tournament_entries` ⋈ `v_tournament_editions`.
(Это ровно пересчёт `playerCards` из старого `build_config.py`, но на лету.)

### `GET /v1/widget` — таймлайн виджета

Вся логика четырёх состояний виджета — на сервере, клиент только рисует:

```json
{
  "state": "rows" | "split" | "no_follows" | "no_matches",
  "rows": [
    {"type": "match", "player": {...}, "opponent": {...},
     "tournament_name": "US Open", "surface": "hard",
     "start_at": "...", "is_today": false},
    {"type": "tournament", "player": {...},
     "tournament_name": "...", "surface": "...", "dates": {...}}
  ],
  "today_column": [
    {"p1_last_name": "Sinner", "p2_last_name": "Alcaraz", "start_at": "..."}
  ]
}
```

Правила (из README виджета): до 3 строк по ближайшей дате; `split` — когда никто из
подписок не играет сегодня, но сегодня есть матчи неподписанных (до 5, сортировка
по лучшему рейтингу участника — `sort=best_rank` из §6); `?tz=` обязателен, «сегодня»
считается в таймзоне пользователя.

## 8. Ingest — запись контента пайплайном (сервисный ключ)

Замена нынешних правок шардов. Всё идемпотентно (upsert по слагу/ключу),
`playerCards`-подобные агрегаты не пишутся — они вычисляются на чтении.

| Метод | Путь | Что делает | Таблицы |
|---|---|---|---|
| `PUT` | `/v1/ingest/matches/{import_key}` | upsert матча целиком: расписание, участники, статус | `matches`, `match_participants` |
| `PATCH` | `/v1/ingest/matches/{import_key}/live` | горячий путь (раз в час и чаще): `{status: "live", live_state: {...}}` | `matches` update |
| `PATCH` | `/v1/ingest/matches/{import_key}/result` | завершение: `{winner_side, outcome, sets: [[6,4,null],...]}` | `matches`, `match_sets` |
| `PUT` | `/v1/ingest/tournaments/{edition_slug}` | upsert турнира+розыгрыша+заявок | `tournaments`, `tournament_editions`, `tournament_entries` |
| `PUT` | `/v1/ingest/players/{slug}` | upsert профиля игрока | `players` |
| `POST` | `/v1/ingest/rankings` | недельный снапшот пачкой: `{snapshot_date, rows: [{player, rank, points, race_points}]}` | `ranking_snapshots` |

## 9. Соответствие экранам приложения (проверка полноты)

| Экран / фича из README | Запросы |
|---|---|
| Онбординг «Choose players» | `GET /v1/players` + `PUT /v1/users/me/follows` |
| Главный экран (обе секции) | `GET /v1/users/me/home` |
| Карточка игрока | `GET /v1/players/{slug}` + `/matches?status=completed&limit=3` + `/matches?status=scheduled&limit=1` + `/tournaments?status=upcoming` (или одним embed-параметром `?include=last_matches,next_match,next_tournament`) |
| Follow/unfollow в toolbar | `PUT`/`DELETE /v1/users/me/follows/{slug}` |
| Виджет (все 4 состояния) | `GET /v1/users/me/widget?tz=Europe/Belgrade` |
| Колонка TODAY | внутри widget-эндпоинта (`GET /v1/matches?date=today&sort=best_rank` — тот же запрос отдельно) |
| `headToHeadBeforeMatch` | `GET /v1/players/{a}/h2h/{b}` (вычисляется из `matches`) |
| `seasonPointsVsLastYear` | из `ranking_snapshots`, когда накопится история за год; до тех пор поле `null` |
| `defendingPoints` | в БД пока нет данных → поле в ответе `null`; когда появится источник — колонка в `tournament_entries` (очки к защите привязаны к паре игрок+турнир) |
| Live activity пуши | серверный воркер: `matches where status='live'` × `follows` × `push_tokens(kind='apns_live_activity')` — вне HTTP API |

## 10. Что сознательно отложено

- GraphQL/gRPC — REST достаточно, клиент один.
- Реалтайм-сокеты для live-счёта — виджет обновляется раз в час, приложению хватит поллинга `GET /v1/matches?status=live`; live activity идут пушами.
- Rate limiting, метрики, OpenAPI-спека — на этапе реализации.
- Админ-UI — ingest-эндпоинтов достаточно, правки руками — SQL.
