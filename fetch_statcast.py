#!/usr/bin/env python3
"""
fetch_statcast.py — LAIro v10 Pitcher Data Pipeline

Pulls 21-day rolling Statcast data for ALL active MLB pitchers from
Baseball Savant, computes hr9/barrel%/wOBA, and upserts into Supabase.

This REPLACES the hardcoded PIT table as the source of truth. Run on a
schedule via GitHub Actions so pitcher data never goes stale again.

Usage:
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_SERVICE_KEY=xxx python3 fetch_statcast.py
"""

import os
import sys
import csv
import io
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import json

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required")
    sys.exit(1)

HEADERS = {
    'apikey': SUPABASE_SERVICE_KEY,
    'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'resolution=merge-duplicates',  # upsert behavior
}

WINDOW_DAYS = 21
MIN_BBE = 3  # minimum batted ball events to include a pitcher — lowered to catch spot starters and openers


def normalize_name(name):
    """Strip accents, lowercase — for fuzzy matching against app's name lookups"""
    nfkd = unicodedata.normalize('NFKD', name)
    no_accents = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return no_accents.lower().strip()


def build_savant_url(start_date, end_date):
    """
    Pitcher CSV URL — uses hit_into_play filter (validated approach from app)
    to stay under Savant's 25k row cap and get complete data for all pitchers.
    """
    return (
        'https://baseballsavant.mlb.com/statcast_search/csv?'
        'all=true&hfPT=&hfAB=&hfGT=R%7C&hfPR=hit_into_play%7C&hfZ=&stadium=&'
        'hfBBL=&hfNewZones=&hfPull=&hfC=&hfSea=2026%7C&hfSit=&'
        'player_type=pitcher&hfOuts=&opponent=&pitcher_throws=&batter_stands=&'
        f'hfSA=&game_date_gt={start_date}&game_date_lt={end_date}&'
        'hfInfield=&team=&position=&hfOutfield=&hfRO=&home_road=&'
        'hfFlag=&hfBBT=&metric_1=&hfInn=&min_pitches=0&min_results=0&'
        'sort_col=pitches&player_event_sort=api_p_release_speed&'
        'sort_order=desc&min_abs=0&type=details&'
    )


def fetch_csv(url, retries=4):
    """Fetch CSV with retries — Savant can be slow, use long timeout"""
    import time
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; LAIro-fetch/1.0)'}
    for attempt in range(retries):
        try:
            print(f"  Attempt {attempt+1}/{retries}...")
            req = Request(url, headers=headers)
            with urlopen(req, timeout=120) as resp:  # 2 min timeout — Savant is slow
                data = resp.read().decode('utf-8', errors='replace')
                print(f"  Fetched {len(data)} bytes")
                return data
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                wait = (attempt + 1) * 15  # 15s, 30s, 45s backoff
                print(f"  Waiting {wait}s before retry...")
                time.sleep(wait)
            else:
                raise
    return None


def parse_csv_row(line):
    """RFC 4180 compliant — handles quoted fields with embedded commas"""
    fields = []
    cur = ''
    in_q = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            if in_q and i + 1 < len(line) and line[i+1] == '"':
                cur += '"'
                i += 1
            else:
                in_q = not in_q
        elif ch == ',' and not in_q:
            fields.append(cur)
            cur = ''
        else:
            cur += ch
        i += 1
    fields.append(cur)
    return fields


def parse_pitcher_statcast(csv_text):
    """
    Game-aware accumulation with L/R platoon splits.
    Groups by pitcher + game_pk + batter_stand, caps barrels at 1/game/split
    to prevent outlier single-game inflation, then aggregates separately for
    overall, vs-L, and vs-R.
    """
    lines = csv_text.split('\n')
    if len(lines) < 2:
        return {}

    headers = [h.strip().strip('"') for h in parse_csv_row(lines[0])]
    idx = {}
    needed_cols = [
        'player_name', 'launch_speed', 'launch_angle', 'bb_type',
        'estimated_slg_using_speedangle', 'events', 'launch_speed_angle',
        'game_pk', 'game_date', 'p_throws', 'stand', 'woba_value', 'woba_denom'
    ]
    for col in needed_cols:
        idx[col] = headers.index(col) if col in headers else -1

    print(f"  Columns found: {len(headers)} | key indices: "
          f"lsa={idx['launch_speed_angle']} ev={idx['launch_speed']} "
          f"bb_type={idx['bb_type']} stand={idx['stand']}")

    # pitchers[name] = {
    #   'all': {games: {game_pk: {...}}, ...},
    #   'L':   {games: {game_pk: {...}}, ...},   <- batter_stand == 'L'
    #   'R':   {games: {game_pk: {...}}, ...},   <- batter_stand == 'R'
    #   throws, xslg_sum, woba_sum, ev_sum (overall only)
    # }
    def new_bucket():
        return {'games': {}, 'xslg_sum': 0.0, 'xslg_count': 0,
                'woba_val_sum': 0.0, 'woba_denom_sum': 0.0,
                'ev_sum': 0.0, 'ev_count': 0}

    pitchers = {}

    for line in lines[1:]:
        if not line.strip():
            continue
        cols = parse_csv_row(line)

        def get(field):
            i = idx[field]
            return cols[i].strip().strip('"') if i >= 0 and i < len(cols) else ''

        name_raw = get('player_name')
        if not name_raw:
            continue
        if ',' in name_raw:
            last, first = name_raw.split(',', 1)
            name = f"{first.strip()} {last.strip()}"
        else:
            name = name_raw

        ev_str = get('launch_speed')
        bb_type = get('bb_type')
        try:
            ev = float(ev_str)
        except (ValueError, TypeError):
            continue

        if ev <= 0 or not bb_type or bb_type == 'bunt':
            continue

        lsa_val = get('launch_speed_angle')
        events = get('events')
        game_pk = get('game_pk') or 'unknown'
        throws = get('p_throws') or 'R'
        batter_stand = get('stand')  # 'L' or 'R' — the SPLIT KEY

        try:
            xslg = float(get('estimated_slg_using_speedangle'))
        except (ValueError, TypeError):
            xslg = None

        try:
            woba_val = float(get('woba_value'))
            woba_denom = float(get('woba_denom'))
        except (ValueError, TypeError):
            woba_val = None
            woba_denom = None

        is_barrel = 1 if lsa_val == '6' else 0

        if name not in pitchers:
            pitchers[name] = {
                'throws': throws,
                'all': new_bucket(),
                'L': new_bucket(),
                'R': new_bucket(),
            }
        p = pitchers[name]

        # Accumulate into 'all' always, and into the matching split bucket if known
        buckets_to_update = [p['all']]
        if batter_stand in ('L', 'R'):
            buckets_to_update.append(p[batter_stand])

        for bucket in buckets_to_update:
            if game_pk not in bucket['games']:
                bucket['games'][game_pk] = {'bbe': 0, 'barrels': 0, 'hh': 0, 'fb': 0, 'hrs': 0}
            g = bucket['games'][game_pk]

            g['bbe'] += 1
            if ev >= 95:
                g['hh'] += 1
            if bb_type in ('fly_ball', 'popup'):
                g['fb'] += 1
            # CAP barrels at 1 per game PER BUCKET — applies independently to
            # overall and to each split, since split samples are smaller and
            # even more vulnerable to one-game inflation
            if is_barrel and g['barrels'] < 1:
                g['barrels'] = 1
            if events == 'home_run':
                g['hrs'] += 1

            if xslg is not None and 0 < xslg <= 4:
                bucket['xslg_sum'] += xslg
                bucket['xslg_count'] += 1
            if woba_val is not None and woba_denom is not None and woba_denom > 0:
                bucket['woba_val_sum'] += woba_val
                bucket['woba_denom_sum'] += woba_denom

            bucket['ev_sum'] += ev
            bucket['ev_count'] += 1

    def summarize_bucket(bucket, min_bbe):
        """
        Aggregate a bucket's games into final rate stats.
        Always returns the raw bbe/game_count even if below min_bbe — only the
        derived RATE stats (hr9, barrel%, etc) become None when sample is too thin.
        This way the app can see '6 BBE vs LHH' even when we don't trust a rate
        computed from only 6 events, instead of silently showing 0.
        """
        games = list(bucket['games'].values())
        total_bbe = sum(g['bbe'] for g in games)
        too_thin = total_bbe < min_bbe
        total_barrels = sum(g['barrels'] for g in games)
        total_hh = sum(g['hh'] for g in games)
        total_fb = sum(g['fb'] for g in games)
        total_hrs = sum(g['hrs'] for g in games)

        if total_bbe == 0:
            return None  # genuinely zero data for this split — nothing to report at all

        innings_est = total_bbe / 2.6
        hr9_est = round((total_hrs / innings_est) * 9, 2) if (innings_est > 0 and not too_thin) else None
        woba = (bucket['woba_val_sum'] / bucket['woba_denom_sum']) if bucket['woba_denom_sum'] > 0 else None

        return {
            # Rate stats — null when sample too thin to trust (caller's min_bbe threshold)
            'hr9': hr9_est,
            'barrel_pct': round(total_barrels / total_bbe * 100, 1) if not too_thin else None,
            'hh_pct': round(total_hh / total_bbe * 100, 1) if not too_thin else None,
            'fb_pct': round(total_fb / total_bbe * 100) if not too_thin else None,
            'woba': round(woba, 3) if (woba is not None and not too_thin) else None,
            'ev': round(bucket['ev_sum'] / bucket['ev_count'], 1) if bucket['ev_count'] > 0 else None,
            # Raw counts — ALWAYS reported accurately regardless of threshold
            'bbe': total_bbe,
            'game_count': len(games),
            'too_thin': too_thin,
        }

    # MIN_BBE for splits is intentionally lower than overall — split samples
    # are naturally smaller. Below this, leave the split null and let the
    # app fall back to overall hr9 for that side (handled in getPit already).
    SPLIT_MIN_BBE = 5  # lowered from 8 — catch thin-sample pitchers in splits too

    result = {}
    for name, p in pitchers.items():
        overall = summarize_bucket(p['all'], MIN_BBE)
        if overall is None:
            continue  # not enough overall sample at all — skip entirely

        split_l = summarize_bucket(p['L'], SPLIT_MIN_BBE)
        split_r = summarize_bucket(p['R'], SPLIT_MIN_BBE)

        record = {
            'name': name,
            'name_normalized': normalize_name(name),
            'throws': p['throws'],
            'hr9': overall['hr9'],
            'barrel_pct_allowed': overall['barrel_pct'],
            'hard_hit_pct_allowed': overall['hh_pct'],
            'fb_pct_allowed': overall['fb_pct'],
            'woba_allowed': overall['woba'],
            'avg_exit_velo': overall['ev'],
            'bbe_sample': overall['bbe'],
            'game_count': overall['game_count'],
            # Split fields — null if sample too thin, app falls back to overall
            'hr9_vs_l': split_l['hr9'] if split_l else None,
            'hr9_vs_r': split_r['hr9'] if split_r else None,  # matches existing schema column names
            'barrel_pct_vs_l': split_l['barrel_pct'] if split_l else None,
            'barrel_pct_vs_r': split_r['barrel_pct'] if split_r else None,
            'fb_pct_vs_l': split_l['fb_pct'] if split_l else None,
            'fb_pct_vs_r': split_r['fb_pct'] if split_r else None,
            'bbe_vs_l': split_l['bbe'] if split_l else 0,
            'bbe_vs_r': split_r['bbe'] if split_r else 0,
            'data_source': 'savant',
            'last_updated': datetime.utcnow().isoformat(),
            'last_fetch_success': True,
        }
        result[name] = record

    return result


def upsert_to_supabase(pitcher_data):
    """Upsert pitcher records to Supabase in batches"""
    records = list(pitcher_data.values())
    batch_size = 100
    success_count = 0
    error_count = 0

    for i in range(0, len(records), batch_size):
        batch = records[i:i+batch_size]
        body = json.dumps(batch).encode('utf-8')
        req = Request(
            f'{SUPABASE_URL}/rest/v1/pitchers?on_conflict=name',
            data=body,
            headers=HEADERS,
            method='POST'
        )
        try:
            with urlopen(req, timeout=30) as resp:
                status = resp.status
                if status in (200, 201, 204):
                    success_count += len(batch)
                else:
                    error_count += len(batch)
                    print(f"  Batch {i//batch_size + 1}: HTTP {status}")
        except (URLError, HTTPError) as e:
            error_count += len(batch)
            print(f"  Batch {i//batch_size + 1} failed: {e}")
            try:
                print(f"    Response: {e.read().decode('utf-8', errors='replace')[:500]}")
            except Exception:
                pass

    return success_count, error_count


def main():
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=WINDOW_DAYS)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    print(f"=== LAIro Pitcher Statcast Fetch ===")
    print(f"Window: {start_str} to {end_str} ({WINDOW_DAYS} days)")

    url = build_savant_url(start_str, end_str)
    print(f"Fetching from Savant...")

    try:
        csv_text = fetch_csv(url)
    except Exception as e:
        print(f"FATAL: Could not fetch Savant data: {e}")
        sys.exit(1)

    row_count = csv_text.count('\n')
    print(f"CSV fetched: ~{row_count} rows, {len(csv_text)} bytes")

    if row_count >= 24500:
        print("WARNING: Row count near 25k cap — some pitchers may have incomplete data")

    print("Parsing (game-aware, barrel-capped)...")
    pitcher_data = parse_pitcher_statcast(csv_text)
    print(f"Parsed {len(pitcher_data)} pitchers with {MIN_BBE}+ BBE")

    if not pitcher_data:
        print("FATAL: No pitcher data parsed — aborting upsert to avoid wiping table")
        sys.exit(1)

    # Sanity check — print a few known names if present
    for check_name in ['Kyle Bradish', 'Joe Ryan', 'Cam Schlittler']:
        if check_name in pitcher_data:
            d = pitcher_data[check_name]
            print(f"  Sanity check {check_name}: hr9={d['hr9']} barrel%={d['barrel_pct_allowed']} "
                  f"bbe={d['bbe_sample']} | vsL: hr9={d.get('hr9_vs_l')} bbe={d.get('bbe_vs_l')} "
                  f"| vsR: hr9={d.get('hr9_vs_r')} bbe={d.get('bbe_vs_r')}")

    print(f"Upserting to Supabase...")
    success, errors = upsert_to_supabase(pitcher_data)
    print(f"Done: {success} upserted, {errors} errors")

    if errors > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
