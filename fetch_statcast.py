import json
import datetime
import sys
import pandas as pd
from pybaseball import statcast

def fetch_statcast_data():
    end = datetime.date.today()
    start = end - datetime.timedelta(days=21)
    start_str = start.strftime('%Y-%m-%d')
    end_str = end.strftime('%Y-%m-%d')
    
    print(f"Fetching Statcast data from {start_str} to {end_str}...")
    
    df = statcast(start_dt=start_str, end_dt=end_str)
    
    print(f"Columns available: {list(df.columns)}")
    print(f"Total rows: {len(df)}")
    
    # Filter to batted ball events only
    df = df[df['launch_speed'].notna()].copy()
    print(f"Rows with launch speed: {len(df)}")
    
    # Pybaseball uses 'batter_name' or builds it from first/last
    # Check which name columns exist
    name_cols = [c for c in df.columns if 'name' in c.lower()]
    print(f"Name columns: {name_cols}")
    
    # Build batter name from available columns
    if 'player_name' in df.columns:
        # player_name format is "Last, First" — convert to "First Last"
        df['batter_name'] = df['player_name'].apply(
            lambda x: ' '.join(reversed(x.split(', '))) if isinstance(x, str) and ', ' in x else x
        )
        df['pitcher_name'] = df['player_name'].apply(
            lambda x: ' '.join(reversed(x.split(', '))) if isinstance(x, str) and ', ' in x else x
        )
    elif 'batter_name' in df.columns:
        pass  # already correct
    else:
        # Build from id columns — use mlbam id lookup not available, use what we have
        print("WARNING: No name column found, using player_name fallback")
        df['batter_name'] = df.get('player_name', 'Unknown')

    # The issue is player_name refers to the PITCHER in statcast data
    # Batter info comes from 'batter' (ID) — we need to use the correct column
    # In pybaseball statcast: 'batter' = batter MLBAM ID, 'pitcher' = pitcher MLBAM ID
    # 'player_name' = pitcher name, 'des' has batter info
    # We need to use the correct groupby field
    
    # Check if there's a dedicated batter name field
    if 'batter_name' not in df.columns:
        # In recent pybaseball versions the column may be structured differently
        # Use 'batter' ID and map, or check for name_batter type columns
        alt_cols = [c for c in df.columns if 'batter' in c.lower()]
        print(f"Batter columns: {alt_cols}")
        pit_cols = [c for c in df.columns if 'pitcher' in c.lower() or 'pitch' in c.lower()]
        print(f"Pitcher columns: {pit_cols[:10]}")

    # Add computed fields
    df['is_barrel'] = ((df['launch_speed'] >= 98) & 
                       (df['launch_angle'] >= 26) & 
                       (df['launch_angle'] <= 30)).astype(int)
    df['is_hard_hit'] = (df['launch_speed'] >= 95).astype(int)
    df['is_fb'] = df['bb_type'].isin(['fly_ball', 'popup']).astype(int)
    
    # Group by batter ID first, then merge names
    batter_stats = df.groupby('batter').agg(
        bbe=('launch_speed', 'count'),
        barrels=('is_barrel', 'sum'),
        hard_hits=('is_hard_hit', 'sum'),
        fly_balls=('is_fb', 'sum'),
        xslg_sum=('estimated_slg_using_speedangle', 'sum'),
        xslg_count=('estimated_slg_using_speedangle', 'count')
    ).reset_index()
    
    # Get name mapping from batter ID
    # In statcast data, when player_type=batter, player_name IS the batter
    # But standard statcast() returns pitcher perspective — player_name = pitcher
    # We need to use a different approach to get batter names
    
    # Get unique batter ID to name mapping from the raw data
    # Look for any column that has the batter's name
    if 'player_name' in df.columns:
        # Check if player_name matches batter or pitcher
        # In statcast data returned by pybaseball, player_name = pitcher name
        # Batter names need to come from a separate lookup or different API call
        print("Note: player_name in statcast() = pitcher. Need batter-specific fetch.")
        
    # Use statcast_batter approach instead
    print("Switching to expected stats endpoint...")
    
    from pybaseball import batting_stats_bref
    from pybaseball import statcast_batter_exitvelo_barrels
    
    try:
        # This endpoint gives us pre-aggregated batter Statcast
        batter_sc = statcast_batter_exitvelo_barrels(start_str, end_str, minBBE=10)
        print(f"Got {len(batter_sc)} batters from exitvelo endpoint")
        print(f"Columns: {list(batter_sc.columns)}")
        
        result = {}
        for _, row in batter_sc.iterrows():
            # Build name
            fname = row.get('first_name', '')
            lname = row.get('last_name', '')
            name = f"{fname} {lname}".strip() if fname and lname else str(row.get('player_name', ''))
            if not name:
                continue
            bar = float(row.get('barrel_batted_rate', 0) or 0)
            if bar < 1: bar = bar * 100  # convert decimal to percent if needed
            hh = float(row.get('hard_hit_percent', 0) or 0)
            if hh < 1: hh = hh * 100
            xs = float(row.get('xslg', 0.420) or 0.420)
            fb = float(row.get('launch_angle_avg', 12) or 12)
            # Convert launch angle to rough FB% estimate if fb% not available
            # fb% from launch angle: higher avg angle = more fly balls
            fb_pct = max(25, min(55, 36 + (fb - 12) * 1.5)) if 'fb_percent' not in batter_sc.columns else float(row.get('fb_percent', 36))
            
            result[name] = {
                'bar': round(bar, 1),
                'hh': round(hh, 1),
                'xs': round(xs, 3),
                'fb': round(fb_pct, 1),
                'bbe': int(row.get('bbe', 0) or 0),
                'source': 'live_21day'
            }
        
        # Pitcher splits using statcast pitcher endpoint
        print("Fetching pitcher splits...")
        from pybaseball import statcast_pitcher_exitvelo_barrels
        pitcher_result = {}
        
        try:
            pit_sc = statcast_pitcher_exitvelo_barrels(start_str, end_str, minBBE=8)
            print(f"Got {len(pit_sc)} pitchers")
            for _, row in pit_sc.iterrows():
                fname = row.get('first_name', '')
                lname = row.get('last_name', '')
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
        except Exception as pe:
            print(f"Pitcher fetch error: {pe}")
        
        output = {
            'generated': end_str,
            'window_start': start_str,
            'window_end': end_str,
            'batters': result,
            'pitchers': pitcher_result
        }
        
        with open('statcast.json', 'w') as f:
            json.dump(output, f)
        
        print(f"SUCCESS: {len(result)} batters, {len(pitcher_result)} pitchers saved to statcast.json")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    fetch_statcast_data()
