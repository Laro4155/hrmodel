import json
import datetime
import sys
import pandas as pd

def fetch_statcast_data():
    end = datetime.date.today()
    start = end - datetime.timedelta(days=21)
    start_str = start.strftime('%Y-%m-%d')
    end_str = end.strftime('%Y-%m-%d')
    
    print(f"Fetching Statcast data {start_str} to {end_str}...")
    
    from pybaseball import statcast_batter_exitvelo_barrels, statcast_pitcher_exitvelo_barrels
    
    # Fetch batter exit velo / barrel data — pre-aggregated, one row per batter
    # Don't pass minBBE as keyword — use positional args only
    batter_sc = statcast_batter_exitvelo_barrels(start_str, end_str, 10)
    print(f"Batter columns: {list(batter_sc.columns)}")
    print(f"Got {len(batter_sc)} batters")
    
    result = {}
    for _, row in batter_sc.iterrows():
        fname = str(row.get('first_name', '') or '')
        lname = str(row.get('last_name', '') or '')
        name = f"{fname} {lname}".strip()
        if not name or name == ' ':
            # Try player_name column
            name = str(row.get('player_name', '') or '')
            if ', ' in name:
                parts = name.split(', ')
                name = f"{parts[1]} {parts[0]}".strip()
        if not name:
            continue
        
        bar = float(row.get('barrel_batted_rate', 0) or 0)
        if bar < 1: bar = bar * 100
        hh = float(row.get('hard_hit_percent', 0) or 0)  
        if hh < 1: hh = hh * 100
        xs = float(row.get('xslg', 0) or 0)
        if xs == 0: xs = 0.420
        la = float(row.get('launch_angle_avg', 12) or 12)
        # Estimate FB% from average launch angle
        fb_pct = max(25, min(55, 36 + (la - 12) * 1.5))
        
        result[name] = {
            'bar': round(bar, 1),
            'hh': round(hh, 1),
            'xs': round(xs, 3),
            'fb': round(fb_pct, 1),
            'bbe': int(row.get('bbe', 0) or 0),
            'source': 'live_21day'
        }
    
    print(f"Processed {len(result)} batters")
    
    # Fetch pitcher data
    pitcher_result = {}
    try:
        pit_sc = statcast_pitcher_exitvelo_barrels(start_str, end_str, 8)
        print(f"Got {len(pit_sc)} pitchers")
        for _, row in pit_sc.iterrows():
            fname = str(row.get('first_name', '') or '')
            lname = str(row.get('last_name', '') or '')
            name = f"{fname} {lname}".strip()
            if not name: continue
            hh = float(row.get('hard_hit_percent', 35) or 35)
            if hh < 1: hh = hh * 100
            xs = float(row.get('xslg', 0.380) or 0.380)
            est_hr9 = max(0.4, min(2.5, (xs - 0.32) * 8))
            pitcher_result[name] = {
                'hr9': round(est_hr9, 2), 'hr9L': round(est_hr9, 2), 'hr9R': round(est_hr9, 2),
                'hh': round(hh, 1), 'hhL': round(hh, 1), 'hhR': round(hh, 1),
                'fb': 36, 'fbL': 36, 'fbR': 36,
                'xslgAgainst': round(xs, 3), 'xslgL': round(xs, 3), 'xslgR': round(xs, 3),
                'bbeL': 20, 'bbeR': 20, 'bbe': 40, 'source': 'live_21day'
            }
        print(f"Processed {len(pitcher_result)} pitchers")
    except Exception as pe:
        print(f"Pitcher fetch error (non-fatal): {pe}")
    
    output = {
        'generated': end_str,
        'window_start': start_str,
        'window_end': end_str,
        'batters': result,
        'pitchers': pitcher_result
    }
    
    with open('statcast.json', 'w') as f:
        json.dump(output, f)
    
    print(f"SUCCESS: {len(result)} batters, {len(pitcher_result)} pitchers → statcast.json")

if __name__ == '__main__':
    fetch_statcast_data()
