#!/usr/bin/env python3
"""Шапка карточки турнира: город, страна, описание, размер сетки, дата жеребьёвки.

Не трогает матчи и не меняет completed-розыгрыши. Upcoming/ongoing с уже
загруженными матчами помечает drawn — иначе карточка рисует «ждём жеребьёвку»,
пока сетка уже на корте.

  DATABASE_URL=postgres://... python3 scripts/seed_tournament_card_meta.py
  DATABASE_URL=postgres://... python3 scripts/seed_tournament_card_meta.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.types.json import Jsonb

# Город отдельно от country_code: клиент собирает чип `City · CC`.
TOURNAMENTS = {
    "atp_finals": {
        "location": "Turin",
        "country_code": "IT",
        "description": {
            "en": "The season finale: eight players, round-robin groups, one indoor hard-court title.",
            "ru": "Финал сезона: восемь игроков, группы и один титул на зале.",
        },
    },
    "barcelona": {
        "location": "Barcelona",
        "country_code": "ES",
        "description": {
            "en": "Clay-court ATP 500 at the Real Club de Tenis Barcelona.",
            "ru": "Грунтовый ATP 500 в Real Club de Tenis Barcelona.",
        },
    },
    "bastad": {
        "location": "Båstad",
        "country_code": "SE",
        "description": {
            "en": "Swedish Open on Baltic clay, a summer stop before the North-American swing.",
            "ru": "Swedish Open на грунте Балтики — летняя остановка перед североамериканской серией.",
        },
    },
    "canada": {
        "location": "Toronto",
        "country_code": "CA",
        "description": {
            "en": "National Bank Open, a Masters 1000 hard-court stop in Canada.",
            "ru": "National Bank Open — Masters 1000 на харде в Канаде.",
        },
    },
    "cincinnati": {
        "location": "Mason",
        "country_code": "US",
        "description": {
            "en": "Western & Southern Open: a Masters 1000 hard-court week outside Cincinnati.",
            "ru": "Western & Southern Open: Masters 1000 на харде под Цинциннати.",
        },
    },
    "eastbourne": {
        "location": "Eastbourne",
        "country_code": "GB",
        "description": {
            "en": "Grass-court tune-up on the south coast, the week before Wimbledon.",
            "ru": "Травяная подготовка на южном побережье, за неделю до Уимблдона.",
        },
    },
    "estoril": {
        "location": "Estoril",
        "country_code": "PT",
        "description": {
            "en": "Clay ATP 250 on the Portuguese coast.",
            "ru": "Грунтовый ATP 250 на побережье Португалии.",
        },
    },
    "gstaad": {
        "location": "Gstaad",
        "country_code": "CH",
        "description": {
            "en": "Swiss Open on alpine clay in the Bernese Oberland.",
            "ru": "Swiss Open на альпийском грунте в Бернском Оберланде.",
        },
    },
    "kitzbuhel": {
        "location": "Kitzbühel",
        "country_code": "AT",
        "description": {
            "en": "Generali Open on Austrian clay in the Tyrolean Alps.",
            "ru": "Generali Open на австрийском грунте в Тироле.",
        },
    },
    "libema": {
        "location": "'s-Hertogenbosch",
        "country_code": "NL",
        "description": {
            "en": "Libema Open, the first grass-court title after Roland Garros.",
            "ru": "Libema Open — первый травяной титул после Ролан Гаррос.",
        },
    },
    "los_cabos": {
        "location": "Los Cabos",
        "country_code": "MX",
        "description": {
            "en": "Hard-court ATP 250 on the Baja California peninsula.",
            "ru": "Хардовый ATP 250 на полуострове Калифорния.",
        },
    },
    "madrid": {
        "location": "Madrid",
        "country_code": "ES",
        "description": {
            "en": "Mutua Madrid Open: a Masters 1000 on high-altitude clay.",
            "ru": "Mutua Madrid Open — Masters 1000 на грунте на высоте.",
        },
    },
    "mallorca": {
        "location": "Santa Ponsa",
        "country_code": "ES",
        "description": {
            "en": "Grass-court ATP 250 in Mallorca, the week before Wimbledon.",
            "ru": "Травяной ATP 250 на Мальорке, за неделю до Уимблдона.",
        },
    },
    "monte_carlo": {
        "location": "Monte-Carlo",
        "country_code": "MC",
        "description": {
            "en": "Rolex Monte-Carlo Masters on the clay of the Côte d'Azur.",
            "ru": "Rolex Monte-Carlo Masters на грунте Лазурного берега.",
        },
    },
    "munich": {
        "location": "Munich",
        "country_code": "DE",
        "description": {
            "en": "BMW Open, a clay ATP 250 in Munich.",
            "ru": "BMW Open — грунтовый ATP 250 в Мюнхене.",
        },
    },
    "newport": {
        "location": "Newport",
        "country_code": "US",
        "description": {
            "en": "Hall of Fame Open on grass at the Newport Casino.",
            "ru": "Hall of Fame Open на траве в Newport Casino.",
        },
    },
    "paris_masters": {
        "location": "Paris",
        "country_code": "FR",
        "description": {
            "en": "Rolex Paris Masters: the last indoor Masters 1000 before Turin.",
            "ru": "Rolex Paris Masters — последний зальный Masters 1000 перед Турином.",
        },
    },
    "roland_garros": {
        "location": "Paris",
        "country_code": "FR",
        "description": {
            "en": "Roland Garros, the clay-court Grand Slam at Porte d'Auteuil.",
            "ru": "Ролан Гаррос — грунтовый турнир Большого шлема у Porte d'Auteuil.",
        },
    },
    "rome": {
        "location": "Rome",
        "country_code": "IT",
        "description": {
            "en": "Internazionali BNL d'Italia, a Masters 1000 on Roman clay.",
            "ru": "Internazionali BNL d'Italia — Masters 1000 на римском грунте.",
        },
    },
    "shanghai": {
        "location": "Shanghai",
        "country_code": "CN",
        "description": {
            "en": "Rolex Shanghai Masters, the Asian Masters 1000 on hard courts.",
            "ru": "Rolex Shanghai Masters — азиатский Masters 1000 на харде.",
        },
    },
    "stuttgart": {
        "location": "Stuttgart",
        "country_code": "DE",
        "description": {
            "en": "BOSS Open, the first ATP 500 on grass.",
            "ru": "BOSS Open — первый ATP 500 на траве.",
        },
    },
    "umag": {
        "location": "Umag",
        "country_code": "HR",
        "description": {
            "en": "Croatia Open on Adriatic clay.",
            "ru": "Croatia Open на адриатическом грунте.",
        },
    },
    "us_open": {
        "location": "New York",
        "country_code": "US",
        "description": {
            "en": "The US Open at the USTA Billie Jean King National Tennis Center. 128 players, one hard-court slam.",
            "ru": "US Open в Национальном теннисном центре имени Билли Джин Кинг. 128 игроков, один хардовый шлем.",
        },
    },
    "washington": {
        "location": "Washington",
        "country_code": "US",
        "description": {
            "en": "Mubadala Citi DC Open, the hard-court ATP 500 in Washington.",
            "ru": "Mubadala Citi DC Open — хардовый ATP 500 в Вашингтоне.",
        },
    },
    "wimbledon": {
        "location": "London",
        "country_code": "GB",
        "description": {
            "en": "The oldest championship in tennis, on the lawns of the All England Club. 128 players, one grass-court crown.",
            "ru": "Старейший чемпионат тенниса на газонах All England Club. 128 игроков, одна травяная корона.",
        },
    },
    "winston_salem": {
        "location": "Winston-Salem",
        "country_code": "US",
        "description": {
            "en": "Winston-Salem Open, the last ATP 250 before the US Open.",
            "ru": "Winston-Salem Open — последний ATP 250 перед US Open.",
        },
    },
}

COUNTRIES = {
    "AT": ("Austria", "Австрия", "🇦🇹"),
    "CA": ("Canada", "Канада", "🇨🇦"),
    "CH": ("Switzerland", "Швейцария", "🇨🇭"),
    "CN": ("China", "Китай", "🇨🇳"),
    "DE": ("Germany", "Германия", "🇩🇪"),
    "ES": ("Spain", "Испания", "🇪🇸"),
    "FR": ("France", "Франция", "🇫🇷"),
    "GB": ("United Kingdom", "Великобритания", "🇬🇧"),
    "HR": ("Croatia", "Хорватия", "🇭🇷"),
    "IT": ("Italy", "Италия", "🇮🇹"),
    "MC": ("Monaco", "Монако", "🇲🇨"),
    "MX": ("Mexico", "Мексика", "🇲🇽"),
    "NL": ("Netherlands", "Нидерланды", "🇳🇱"),
    "PT": ("Portugal", "Португалия", "🇵🇹"),
    "SE": ("Sweden", "Швеция", "🇸🇪"),
    "US": ("United States", "США", "🇺🇸"),
}

# Типичный размер основной сетки и дата жеребьёвки (четверг/пятница перед стартом).
# Не выдумываем сетку: только мета, которую карточка читает в шапке и empty-state.
EDITIONS_2026 = {
    "winston_salem_2026": {"draw_size": 48, "draw_date": "2026-08-21"},
    "us_open_2026": {"draw_size": 128, "draw_date": "2026-08-27"},
    "shanghai_2026": {"draw_size": 96, "draw_date": "2026-10-03"},
    "paris_masters_2026": {"draw_size": 56, "draw_date": "2026-10-31"},
    "atp_finals_2026": {"draw_size": 8, "draw_date": "2026-11-14"},
}


def apply(cur) -> None:
    for code, (en, ru, flag) in COUNTRIES.items():
        cur.execute(
            """insert into countries (code, name, flag_emoji)
               values (%s, %s, %s)
               on conflict (code) do update
                 set name = excluded.name, flag_emoji = excluded.flag_emoji""",
            (code, Jsonb({"en": en, "ru": ru}), flag),
        )

    for slug, meta in TOURNAMENTS.items():
        cur.execute(
            """update tournaments
               set location = %s, country_code = %s, description = %s
               where slug = %s""",
            (meta["location"], meta["country_code"], Jsonb(meta["description"]), slug),
        )

    for slug, meta in EDITIONS_2026.items():
        cur.execute(
            """update tournament_editions
               set draw_size = %s, draw_date = %s
               where slug = %s""",
            (meta["draw_size"], meta["draw_date"], slug),
        )

    # Сетка уже в matches — карточка не должна оставаться в awaiting_draw.
    cur.execute(
        """update tournament_editions te
           set draw_status = 'drawn'
           from v_tournament_editions v
           where v.id = te.id
             and v.status in ('upcoming', 'ongoing')
             and te.draw_status = 'awaiting_draw'
             and exists (select 1 from matches m where m.edition_id = te.id)"""
    )
    print(f"[ok] countries: {len(COUNTRIES)}")
    print(f"[ok] tournament brands: {len(TOURNAMENTS)}")
    print(f"[ok] 2026 edition meta: {len(EDITIONS_2026)}")
    print(f"[ok] marked drawn (upcoming/ongoing with matches): {cur.rowcount}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.db_url:
        sys.exit("нужен DATABASE_URL (env) или --db-url")

    with psycopg.connect(args.db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            apply(cur)
        if args.dry_run:
            conn.rollback()
            print("[dry-run] rolled back")
        else:
            conn.commit()
            print("[ok] committed")


if __name__ == "__main__":
    main()
