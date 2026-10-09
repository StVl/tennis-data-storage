import json

# Load upcoming
with open('data/matches_upcoming.json', 'r') as f:
    upcoming = json.load(f)

# Load past
with open('data/matches_past.json', 'r') as f:
    past = json.load(f)

# Matches to complete (move from upcoming to past)
completed_ids = {
    'machac_shanghai_r2_2026': {
        'result': 'lost',
        'score': '4-6, 6-7(4)'
    },
    'bublik_shanghai_r2_2026': {
        'result': 'won',
        'score': '6-4, 7-6(4)'
    },
    'musetti_shanghai_r2_2026': {
        'result': 'lost',
        'score': '6-7(7), 3-6, 6-7(1)'
    }
}

# Matches to fix dates (Oct 9 -> Oct 10)
date_fixes = {
    'svajda_shanghai_r2_2026': '2026-10-10T12:00:00+08:00',
    'tien_shanghai_r2_2026': '2026-10-10T12:00:00+08:00',
    'baez_shanghai_r2_2026': '2026-10-10T12:00:00+08:00',
    'vacherot_shanghai_r2_2026': '2026-10-10T12:00:00+08:00',
    'tsitsipas_shanghai_r2_2026': '2026-10-10T12:00:00+08:00',
    'darderi_shanghai_r2_2026': '2026-10-10T12:00:00+08:00',
    'bergs_shanghai_r2_2026': '2026-10-10T19:30:00+08:00',
    'buse_shanghai_r2_2026': '2026-10-10T19:30:00+08:00',
}

new_upcoming = []
newly_completed = []

for match in upcoming['matches']:
    mid = match['id']
    if mid in completed_ids:
        completed = {
            'id': match['id'],
            'playerId': match['playerId'],
            'opponentId': match['opponentId'],
            'tournament': match['tournament'],
            'stage': match['stage'],
            'startAt': match['startAt'],
            'surface': match['surface'],
            'status': 'completed',
            'result': completed_ids[mid]['result'],
            'score': completed_ids[mid]['score']
        }
        newly_completed.append(completed)
        print(f"Moving to past: {mid} -> {completed_ids[mid]['result']} {completed_ids[mid]['score']}")
    else:
        if mid in date_fixes:
            match['startAt'] = date_fixes[mid]
            print(f"Date fix: {mid} -> {date_fixes[mid]}")
        new_upcoming.append(match)

upcoming['matches'] = new_upcoming
past['matches'] = newly_completed + past['matches']

with open('data/matches_upcoming.json', 'w') as f:
    json.dump(upcoming, f, ensure_ascii=False, indent=2)

with open('data/matches_past.json', 'w') as f:
    json.dump(past, f, ensure_ascii=False, indent=2)

print(f"\nDone. Upcoming: {len(new_upcoming)} matches. Past: {len(past['matches'])} matches.")
