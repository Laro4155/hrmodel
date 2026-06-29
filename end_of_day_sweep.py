#!/usr/bin/env python3
"""
end_of_day_sweep.py — LAIro v10 End-of-Day Result Writer

Runs via GitHub Actions at 2:00 AM ET daily.
For the previous day's slate:
  1. Fetches all completed games from MLB Stats API
  2. Gets HR results from Baseball Savant game feeds
  3. Reads what's already logged in Supabase hr_logs for that date
  4. Writes No HR for any player/game pair not already in the table
  5. Updates result to HR/2HR for anyone who hit one

This replaces the browser-based auto-sweep so results write even
if the app is closed.

Usage:
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_SERVICE_KEY=xxx python3 end_of_day_sweep.py
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python3 end_of_day_sweep.py --date 2026-06-28
"""

import os
import sys
import json
import unicodedata
import re
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── Config ────────────────────────────────────────────────────────────────────
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


# ── Helpers ───────────────────────────────────────────────────────────────────
def strip_accents(s):
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def fetch_json(url, headers=None, retries=3):
    h = dict(MLB_HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            req = Request(url, headers=h)
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8', errors='replace'))
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  Retry {attempt+1}: {e}")


def sb_get(path, params=''):
    url = f"{SUPABASE_URL}/rest/v1/{path}?{params}"
    req = Request(url, headers={
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
    })
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return 0
    body = json.dumps(rows).encode('utf-8')
    headers = dict(SB_HEADERS)
    headers['Prefer'] = f'resolution=merge-duplicates,return=minimal'
    req = Request(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        data=body,
        headers=headers,
        method='POST',
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return len(rows) if resp.status in (200, 201, 204) else 0
    except HTTPError as e:
        body_text = e.read().decode('utf-8', errors='replace')[:300]
        print(f"  Supabase upsert error: {e.code} — {body_text}")
        return 0


# ── MLB Schedule ──────────────────────────────────────────────────────────────
def fetch_schedule(date_str):
    """Get all games for a date. Returns list of game dicts."""
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}&hydrate=linescore,team"
    data = fetch_json(url)
    games = []
    for date_block in (data.get('dates') or []):
        for g in (date_block.get('games') or []):
            games.append(g)
    return games


def get_final_game_pks(games):
    """Return gamePks for completed games only."""
    return [
        str(g['gamePk'])
        for g in games
        if g.get('status', {}).get('abstractGameState') == 'Final'
    ]


# ── HR Results from Baseball Savant ──────────────────────────────────────────
def fetch_hrs_for_game(game_pk, game_date):
    """
    Fetches HR events from Baseball Savant game feed.
    Returns list of dicts: {name, game_pk, count, ev, angle, dist, parks, grade}
    """
    url = f"https://baseballsavant.mlb.com/gf?game_pk={game_pk}"
    try:
        data = fetch_json(url)
    except Exception as e:
        print(f"  Savant fetch failed for game {game_pk}: {e}")
        return []

    ev_plays = data.get('exit_velocity') or []
    hr_plays = [p for p in ev_plays if p.get('result') == 'Home Run']

    # Group by batter — handle multi-HR games, keep best EV
    hr_map = {}
    for p in hr_plays:
        name = p.get('batter_name', '')
        if not name:
            continue
        key = f"{name}|||{game_pk}"
        if key not in hr_map:
            hr_map[key] = {'name': name, 'game_pk': str(game_pk), 'count': 0,
                           'ev': 0, 'angle': 0, 'dist': 0, 'parks': 0}
        hr_map[key]['count'] += 1
        ev = float(p.get('launch_speed') or 0)
        if ev > hr_map[key]['ev']:
            hr_map[key]['ev'] = ev
            hr_map[key]['angle'] = float(p.get('launch_angle') or 0)
            hr_map[key]['dist'] = int(p.get('hit_distance') or 0)
            cm = p.get('contextMetrics') or {}
            hr_map[key]['parks'] = cm.get('homeRunBallparks', 0)

    results = []
    for h in hr_map.values():
        ev, dist, angle = h['ev'], h['dist'], h['angle']
        sweet = 20 <= angle <= 35
        if ev >= 105 and dist >= 400 and sweet:
            grade = 'ELITE'
        elif ev >= 100 and dist >= 380 and sweet:
            grade = 'SOLID'
        elif ev >= 98 and dist >= 360:
            grade = 'DECENT'
        else:
            grade = 'CHEAP'
        h['grade'] = grade
        results.append(h)

    return results


# ── Supabase: read existing logs for date ────────────────────────────────────
def get_existing_logs(date_str):
    """
    Returns dict keyed by (player_name_normalized, game) -> row
    for all existing hr_logs rows on this date.
    """
    rows = sb_get('hr_logs', f'select=id,player_name,game,result,date&date=eq.{date_str}')
    existing = {}
    for r in rows:
        key = (strip_accents(r['player_name']), r.get('game', ''))
        existing[key] = r
    return existing


# ── Build game label from MLB schedule ───────────────────────────────────────
def game_label(game):
    """'AwayTeam @ HomeTeam' — matches app's game field format."""
    away = game.get('teams', {}).get('away', {}).get('team', {}).get('teamName', '')
    home = game.get('teams', {}).get('home', {}).get('team', {}).get('teamName', '')
    return f"{away} @ {home}"


# ── Main sweep ────────────────────────────────────────────────────────────────
def run_sweep(date_str):
    print(f"=== LAIro End-of-Day Sweep: {date_str} ===")

    # 1. Get schedule
    print("Fetching schedule...")
    games = fetch_schedule(date_str)
    print(f"  Found {len(games)} games")

    final_pks = get_final_game_pks(games)
    print(f"  Final games: {len(final_pks)}")

    if not final_pks:
        print("No final games found — nothing to sweep.")
        return

    # Build gamePk -> game info map
    game_map = {str(g['gamePk']): g for g in games}

    # 2. Fetch HR results from Savant
    print("Fetching HR results from Baseball Savant...")
    all_hrs = []
    for pk in final_pks:
        hrs = fetch_hrs_for_game(pk, date_str)
        for h in hrs:
            h['game_label'] = game_label(game_map.get(pk, {}))
        all_hrs.extend(hrs)
        print(f"  Game {pk}: {len(hrs)} HR(s)")

    # Build HR lookup: normalized_name -> HR data
    hr_lookup = {}
    for h in all_hrs:
        norm = strip_accents(h['name'])
        hr_lookup[norm] = h
        # Also try last+first for accented name variants
        parts = norm.split()
        if len(parts) >= 2:
            alt = f"{parts[-1]} {' '.join(parts[:-1])}"
            hr_lookup[alt] = h

    print(f"  Total HRs: {len(all_hrs)}")

    # 3. Get existing Supabase logs for this date
    print("Reading existing hr_logs from Supabase...")
    existing = get_existing_logs(date_str)
    print(f"  Existing rows: {len(existing)}")

    # 4. For each existing row, update result if it was a HR and isn't already logged
    updates = []
    for (norm_name, game), row in existing.items():
        if row['result'] in ('HR', '2HR'):
            continue  # already has a result

        # Check if this player hit a HR today
        hr = hr_lookup.get(norm_name)
        if hr:
            result = '2HR' if hr['count'] >= 2 else 'HR'
            updates.append({
                'id': row['id'],
                'result': result,
                'ev': hr['ev'] if hr['ev'] > 0 else None,
                'angle': hr['angle'] if hr['angle'] != 0 else None,
                'distance': hr['dist'] if hr['dist'] > 0 else None,
                'hit_grade': hr['grade'],
                'data_source': 'live',
            })
        else:
            updates.append({
                'id': row['id'],
                'result': 'No HR',
                'data_source': 'live',
            })

    print(f"  Rows to update: {len(updates)} ({sum(1 for u in updates if u['result'] != 'No HR')} HRs)")

    # Upsert by id
    if updates:
        written = sb_upsert('hr_logs', updates, 'id')
        print(f"  Written: {written}")
    else:
        print("  Nothing to update.")

    print("=== Sweep complete ===")


if __name__ == '__main__':
    # Default: yesterday ET
    if '--date' in sys.argv:
        idx = sys.argv.index('--date')
        target_date = sys.argv[idx + 1]
    else:
        # Use ET (UTC-4 during DST, UTC-5 in standard)
        # GitHub Actions runs at 2am ET = 6am or 7am UTC
        # Just use UTC date minus 1 day to get yesterday in ET
        et_now = datetime.now(timezone.utc) - timedelta(hours=4)
        target_date = (et_now - timedelta(days=1)).strftime('%Y-%m-%d')

    run_sweep(target_date)
