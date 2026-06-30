#!/usr/bin/env python3
"""
end_of_day_sweep.py — LAIro v10 End-of-Day Result Writer

Runs via GitHub Actions at 6:00 AM UTC (2:00 AM ET) daily.
Completely independent of the app — fetches full lineups from MLB Stats API,
gets HR results from Baseball Savant, and writes every batter to Supabase.

Usage:
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_SERVICE_KEY=xxx python3 end_of_day_sweep.py
    python3 end_of_day_sweep.py --date 2026-06-29
"""

import os, sys, json, unicodedata
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars required")
    sys.exit(1)

SB_HEADERS = {
    'apikey': SUPABASE_SERVICE_KEY,
    'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'resolution=merge-duplicates,return=minimal',
}

MLB_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; LAIro-sweep/1.0)'}


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers=MLB_HEADERS)
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8', errors='replace'))
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  Retry {attempt+1}: {e}")


def strip_accents(s):
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def sb_upsert(rows):
    if not rows:
        return 0
    body = json.dumps(rows).encode('utf-8')
    req = Request(
        f"{SUPABASE_URL}/rest/v1/hr_logs?on_conflict=player_name,game,date",
        data=body, headers=SB_HEADERS, method='POST',
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return len(rows) if resp.status in (200, 201, 204) else 0
    except HTTPError as e:
        print(f"  Supabase error: {e.code} — {e.read().decode()[:200]}")
        return 0


def fetch_schedule(date_str):
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}&hydrate=linescore,lineups,team"
    return fetch_json(url)


def fetch_boxscore(game_pk):
    url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
    return fetch_json(url)


def fetch_savant_hrs(game_pk):
    """Returns dict of normalized_name -> {count, ev, angle, dist, grade}"""
    url = f"https://baseballsavant.mlb.com/gf?game_pk={game_pk}"
    try:
        data = fetch_json(url)
    except Exception as e:
        print(f"  Savant failed for {game_pk}: {e}")
        return {}

    ev_plays = data.get('exit_velocity') or []
    hr_plays = [p for p in ev_plays if p.get('result') == 'Home Run']

    hr_map = {}
    for p in hr_plays:
        name = p.get('batter_name', '')
        if not name:
            continue
        norm = strip_accents(name)
        if norm not in hr_map:
            hr_map[norm] = {'name': name, 'count': 0, 'ev': 0, 'angle': 0, 'dist': 0}
        hr_map[norm]['count'] += 1
        ev = float(p.get('launch_speed') or 0)
        if ev > hr_map[norm]['ev']:
            hr_map[norm]['ev'] = ev
            hr_map[norm]['angle'] = float(p.get('launch_angle') or 0)
            hr_map[norm]['dist'] = int(p.get('hit_distance') or 0)

    # Grade each HR
    for h in hr_map.values():
        ev, dist, angle = h['ev'], h['dist'], h['angle']
        sweet = 20 <= angle <= 35
        if ev >= 105 and dist >= 400 and sweet:
            h['grade'] = 'ELITE'
        elif ev >= 100 and dist >= 380 and sweet:
            h['grade'] = 'SOLID'
        elif ev >= 98 and dist >= 360:
            h['grade'] = 'DECENT'
        else:
            h['grade'] = 'CHEAP'

    return hr_map


def game_label(away_name, home_name):
    return f"{away_name} @ {home_name}"


def run_sweep(date_str):
    print(f"=== LAIro End-of-Day Sweep: {date_str} ===")

    schedule = fetch_schedule(date_str)
    dates = schedule.get('dates') or []
    if not dates:
        print("No games found.")
        return

    games = dates[0].get('games') or []
    final_games = [g for g in games if g.get('status', {}).get('abstractGameState') == 'Final']
    print(f"  Games: {len(games)} total, {len(final_games)} final")

    if not final_games:
        print("No final games — nothing to sweep.")
        return

    all_rows = []

    for game in final_games:
        pk = game['gamePk']
        away_team = game.get('teams', {}).get('away', {}).get('team', {}).get('teamName', 'Away')
        home_team = game.get('teams', {}).get('home', {}).get('team', {}).get('teamName', 'Home')
        label = game_label(away_team, home_team)

        print(f"\n  Game {pk}: {label}")

        # Fetch HR results from Savant
        hr_map = fetch_savant_hrs(pk)
        print(f"    HRs from Savant: {sum(h['count'] for h in hr_map.values())}")

        # Fetch full boxscore for all batters
        try:
            boxscore = fetch_boxscore(pk)
        except Exception as e:
            print(f"    Boxscore failed: {e}")
            continue

        teams = boxscore.get('teams', {})
        batters_written = set()

        for side in ('away', 'home'):
            team_data = teams.get(side, {})
            players = team_data.get('players', {})
            batting_order = team_data.get('battingOrder', [])

            # Use batting order if available, else all players who batted
            if batting_order:
                player_ids = [str(pid) for pid in batting_order]
            else:
                player_ids = [pid for pid in players.keys()]

            for pid in player_ids:
                player = players.get(str(pid)) or players.get(f'ID{pid}')
                if not player:
                    continue

                person = player.get('person', {})
                name = person.get('fullName', '')
                if not name or name in batters_written:
                    continue

                # Skip pitchers
                pos = player.get('position', {}).get('abbreviation', '')
                if pos == 'P':
                    continue

                batters_written.add(name)
                norm = strip_accents(name)

                hr_data = hr_map.get(norm)
                if hr_data:
                    result = '2HR' if hr_data['count'] >= 2 else 'HR'
                    ev = hr_data['ev'] if hr_data['ev'] > 0 else None
                    angle = hr_data['angle'] if hr_data['angle'] != 0 else None
                    dist = hr_data['dist'] if hr_data['dist'] > 0 else None
                    grade = hr_data.get('grade')
                else:
                    result = 'No HR'
                    ev = angle = dist = grade = None

                team_name = team_data.get('team', {}).get('teamName', '')
                bat_spot = player.get('battingOrder', '')
                bat_num = int(str(bat_spot)[0]) if bat_spot else None

                all_rows.append({
                    'date': date_str,
                    'player_name': name,
                    'game': label,
                    'team': team_name,
                    'result': result,
                    'hit_grade': grade,
                    'ev': ev,
                    'angle': angle,
                    'distance': dist,
                    'data_source': 'sweep',
                })

        print(f"    Batters: {len(batters_written)} | HRs matched: {sum(1 for r in all_rows if r['game'] == label and r['result'] != 'No HR')}")

    print(f"\nTotal rows to write: {len(all_rows)}")

    if not all_rows:
        print("Nothing to write.")
        return

    # Batch upsert
    batch_size = 200
    total_written = 0
    for i in range(0, len(all_rows), batch_size):
        batch = all_rows[i:i+batch_size]
        written = sb_upsert(batch)
        total_written += written
        print(f"  Batch {i//batch_size + 1}: {written}/{len(batch)}")

    print(f"\n=== Sweep complete: {total_written} rows written ===")


if __name__ == '__main__':
    if '--date' in sys.argv:
        idx = sys.argv.index('--date')
        target_date = sys.argv[idx + 1]
    else:
        et_now = datetime.now(timezone.utc) - timedelta(hours=4)
        target_date = (et_now - timedelta(days=1)).strftime('%Y-%m-%d')

    run_sweep(target_date)
