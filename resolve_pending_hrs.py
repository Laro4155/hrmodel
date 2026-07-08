#!/usr/bin/env python3
"""
resolve_pending_hrs.py — LAIro v10 Overnight Pending Resolver

The moment a play locks into True Top 10 or Auto-Lock in index.html, it writes
a 'Pending' row to hr_logs with its captured odds/prob/context. This script
finds every Pending row, checks whether that date's games are all Final, and
resolves each one to a final HR/2HR/No HR result with full hit detail
(grade, exit velo, distance, angle) via Baseball Savant.

This runs entirely server-side on a schedule — it does NOT depend on the
browser that locked the play ever being reopened. That's the point: previously,
closing out True Top 10 / Auto-Lock plays required opening the app again and
either clicking "Close Out Day" or waiting for the client-side auto-closeout
on next page load. This script means that work happens even if the computer
is off all night.

Only resolves a date once ALL of that date's MLB games are Final — if any
game is still in progress when this runs, that entire date is skipped and
retried on the next run (should self-resolve within a day or two).

Usage:
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_SERVICE_KEY=xxx python3 resolve_pending_hrs.py
"""

import os
import sys
import json
import unicodedata
import urllib.parse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required")
    sys.exit(1)

HEADERS = {
    'apikey': SUPABASE_SERVICE_KEY,
    'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
    'Content-Type': 'application/json',
}
MLB_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; LAIro-resolve/1.0)'}


def fetch_json(url, headers=None, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers or {})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8', errors='replace'))
        except Exception as e:
            print(f"  Attempt {attempt+1} failed for {url}: {e}")
            if attempt == retries - 1:
                raise
    return None


def strip_accents(s):
    nfkd = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def get_pending_rows():
    url = f"{SUPABASE_URL}/rest/v1/hr_logs?result=eq.Pending&select=player_name,game,date,team,home_away"
    rows = fetch_json(url, headers=HEADERS)
    return rows or []


def all_games_final(date_str):
    """Returns (all_final: bool, final_game_pks: list). If no games scheduled, treat as final (nothing to wait for)."""
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}"
    d = fetch_json(url, headers=MLB_HEADERS)
    dates = d.get('dates') or []
    if not dates:
        return True, []
    games = dates[0].get('games') or []
    if not games:
        return True, []
    statuses = [g.get('status', {}).get('abstractGameState') for g in games]
    final = all(s == 'Final' for s in statuses)
    game_pks = [g['gamePk'] for g in games if g.get('status', {}).get('abstractGameState') == 'Final']
    return final, game_pks


def fetch_hr_map_for_games(game_pks):
    """Same grading logic as the client's fetchTodayHRs() for consistency."""
    hr_map = {}
    for pk in game_pks:
        try:
            d = fetch_json(f"https://baseballsavant.mlb.com/gf?game_pk={pk}", headers=MLB_HEADERS)
        except Exception as e:
            print(f"  Savant fetch failed for game {pk}: {e}")
            continue
        ev_plays = d.get('exit_velocity') or []
        hr_plays = [p for p in ev_plays if p.get('result') == 'Home Run']
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


def find_match(name, hr_map):
    """Exact normalized match first, then last+first-name fallback — mirrors client-side findHRMatch()."""
    pstripped = strip_accents(name)
    if pstripped in hr_map:
        return hr_map[pstripped]
    parts = pstripped.split(' ')
    p_last, p_first = parts[-1], parts[0]
    if len(p_last) >= 4:
        for norm, h in hr_map.items():
            nparts = norm.split(' ')
            if nparts[-1] == p_last and nparts[0] == p_first:
                return h
    return None


def patch_row(player_name, game, date, result, hit_grade=None, ev=None, distance=None, angle=None):
    params = (
        f"player_name=eq.{urllib.parse.quote(player_name)}"
        f"&game=eq.{urllib.parse.quote(game or '')}"
        f"&date=eq.{date}"
    )
    url = f"{SUPABASE_URL}/rest/v1/hr_logs?{params}"
    body = {'result': result}
    if hit_grade is not None: body['hit_grade'] = hit_grade
    if ev is not None: body['ev'] = ev
    if distance is not None: body['distance'] = distance
    if angle is not None: body['angle'] = angle
    req = Request(url, data=json.dumps(body).encode('utf-8'),
                  headers={**HEADERS, 'Prefer': 'return=minimal'}, method='PATCH')
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status in (200, 204)
    except (URLError, HTTPError) as e:
        print(f"  PATCH failed for {player_name} / {game} / {date}: {e}")
        return False


def main():
    print("=== LAIro Pending HR Resolver ===")
    pending = get_pending_rows()
    if not pending:
        print("No Pending rows — nothing to resolve.")
        return
    print(f"Found {len(pending)} Pending rows across {len(set(r['date'] for r in pending))} date(s)")

    by_date = {}
    for row in pending:
        by_date.setdefault(row['date'], []).append(row)

    resolved_count = 0
    skipped_dates = []
    for date_str, rows in by_date.items():
        print(f"\nDate {date_str}: {len(rows)} pending")
        final, game_pks = all_games_final(date_str)
        if not final:
            print(f"  Not all games Final yet for {date_str} — skipping, will retry next run")
            skipped_dates.append(date_str)
            continue
        hr_map = fetch_hr_map_for_games(game_pks) if game_pks else {}
        print(f"  {len(hr_map)} players hit HRs across {len(game_pks)} completed games")

        for row in rows:
            match = find_match(row['player_name'], hr_map)
            if match:
                result = '2HR' if match['count'] >= 2 else 'HR'
                ok = patch_row(row['player_name'], row['game'], row['date'], result,
                                hit_grade=match['grade'], ev=match['ev'],
                                distance=match['dist'], angle=match['angle'])
            else:
                ok = patch_row(row['player_name'], row['game'], row['date'], 'No HR')
            if ok:
                resolved_count += 1

    tail = f", {len(skipped_dates)} date(s) skipped (games not final yet)" if skipped_dates else ""
    print(f"\nDone: {resolved_count} rows resolved{tail}")


if __name__ == '__main__':
    main()
