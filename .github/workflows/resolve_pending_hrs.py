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


SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}

def normalize_name(name):
    """
    Fuller normalization than strip_accents alone — this is the actual fix.
    The old matcher split on whitespace and took the LAST token as the surname.
    For any player with a suffix ("Luis Garcia Jr.", "Cedric Mullins II"), that
    last token was "jr."/"ii" instead of the real surname — silently breaking
    the fallback match for every suffixed name whenever the exact-string match
    also missed (e.g. due to a period, curly apostrophe, or spacing difference
    between our stored name and Savant's). This strips periods, normalizes
    curly apostrophes to straight ones, and removes suffix tokens entirely
    before either side of a comparison, on BOTH the pending row's name and
    Savant's batter_name — so a suffix can never masquerade as a surname.
    """
    s = strip_accents(name)
    s = s.replace('’', "'").replace('.', '')
    tokens = [t for t in s.split(' ') if t]
    if tokens and tokens[-1] in SUFFIXES:
        tokens = tokens[:-1]
    return ' '.join(tokens)


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
            norm = normalize_name(name)
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
    """Exact normalized match first, then last+first-name fallback (suffix-safe)."""
    pstripped = normalize_name(name)
    if pstripped in hr_map:
        return hr_map[pstripped]
    parts = pstripped.split(' ')
    if not parts:
        return None
    p_last, p_first = parts[-1], parts[0]
    if len(p_last) >= 4:
        for norm, h in hr_map.items():
            nparts = norm.split(' ')
            if nparts and nparts[-1] == p_last and nparts[0] == p_first:
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


def get_resolved_names_for_date(date_str):
    """
    Returns the set of normalized player names that already have a NON-Pending
    row for this date (e.g. caught live by autoLogHRs before this script ever
    ran). Added 7/23/2026 — previously the unmatched-Savant warning only
    checked against the CURRENT batch of Pending rows, so a player already
    resolved via live catch would incorrectly show up as "never matched" even
    though there was nothing left to resolve for them. False alarm, not a bug.
    """
    url = f"{SUPABASE_URL}/rest/v1/hr_logs?date=eq.{date_str}&result=neq.Pending&select=player_name"
    rows = fetch_json(url, headers=HEADERS) or []
    return set(normalize_name(r['player_name']) for r in rows if r.get('player_name'))


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

        # DIAGNOSTIC (added 7/19/2026): track exactly who matched vs who didn't,
        # so a future discrepancy between "Savant says N players homered" and
        # "we only logged M as HR" is directly diagnosable from this log instead
        # of requiring a manual investigation like this one did.
        matched_names = set()
        unmatched_pending = []

        for row in rows:
            match = find_match(row['player_name'], hr_map)
            if match:
                matched_names.add(normalize_name(match['name']))
                result = '2HR' if match['count'] >= 2 else 'HR'
                ok = patch_row(row['player_name'], row['game'], row['date'], result,
                                hit_grade=match['grade'], ev=match['ev'],
                                distance=match['dist'], angle=match['angle'])
            else:
                ok = patch_row(row['player_name'], row['game'], row['date'], 'No HR')
                unmatched_pending.append(row['player_name'])
            if ok:
                resolved_count += 1

        # FIXED 7/23/2026 — a Savant HR-hitter who was already resolved via a
        # DIFFERENT path (e.g. caught live by autoLogHRs before this script
        # ever ran) is NOT a real miss, even though they weren't in THIS
        # batch's matched_names. Check already-resolved rows for the date
        # before treating anyone as genuinely unmatched.
        already_resolved = get_resolved_names_for_date(date_str)
        unmatched_savant = [
            h['name'] for norm, h in hr_map.items()
            if norm not in matched_names and norm not in already_resolved
        ]
        if unmatched_savant:
            print(f"  ⚠ {len(unmatched_savant)} Savant HR-hitter(s) never matched to a pending row (check if these were in scope): {unmatched_savant}")

    tail = f", {len(skipped_dates)} date(s) skipped (games not final yet)" if skipped_dates else ""
    print(f"\nDone: {resolved_count} rows resolved{tail}")


if __name__ == '__main__':
    main()
