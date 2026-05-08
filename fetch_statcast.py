import json
import datetime
import sys

try:
    from pybaseball import statcast
    import pandas as pd
except ImportError:
    print("Installing dependencies...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pybaseball", "pandas"])
    from pybaseball import statcast
    import pandas as pd

def fetch_statcast_data():
    end = datetime.date.today()
    start = end - datetime.timedelta(days=21)
    start_str = start.strftime('%Y-%m-%d')
    end_str = end.strftime('%Y-%m-%d')
    
    print(f"Fetching Statcast data from {start_str} to {end_str}...")
    
    # Fetch all batted ball events
    df = statcast(start_dt=start_str, end_dt=end_str)
    
    # Filter to only batted ball events with exit velocity
    df = df[df['launch_speed'].notna()].copy()
    
    # Add barrel flag (standard MLB barrel definition)
    df['is_barrel'] = (
        (df['launch_speed'] >= 98) & 
        (df['launch_angle'] >= 26) & 
        (df['launch_angle'] <= 30)
    ).astype(int)
    
    # Add hard hit flag (95+ mph)
    df['is_hard_hit'] = (df['launch_speed'] >= 95).astype(int)
    
    # Add fly ball flag
    df['is_fb'] = df['bb_type'].isin(['fly_ball', 'popup']).astype(int)
    
    # Group by batter
    batter_stats = df.groupby('batter_name').agg(
        bbe=('launch_speed', 'count'),
        barrels=('is_barrel', 'sum'),
        hard_hits=('is_hard_hit', 'sum'),
        fly_balls=('is_fb', 'sum'),
        xslg_sum=('estimated_slg_using_speedangle', 'sum'),
        xslg_count=('estimated_slg_using_speedangle', 'count')
    ).reset_index()
    
    # Filter to minimum 10 BBE
    batter_stats = batter_stats[batter_stats['bbe'] >= 10]
    
    # Calculate rates
    batter_stats['barrel_pct'] = (batter_stats['barrels'] / batter_stats['bbe'] * 100).round(1)
    batter_stats['hard_hit_pct'] = (batter_stats['hard_hits'] / batter_stats['bbe'] * 100).round(1)
    batter_stats['fb_pct'] = (batter_stats['fly_balls'] / batter_stats['bbe'] * 100).round(1)
    batter_stats['xslg'] = (batter_stats['xslg_sum'] / batter_stats['xslg_count']).round(3)
    
    # Build result dict
    result = {}
    for _, row in batter_stats.iterrows():
        name = row['batter_name']
        result[name] = {
            'bar': float(row['barrel_pct']),
            'hh': float(row['hard_hit_pct']),
            'fb': float(row['fb_pct']),
            'xs': float(row['xslg']) if pd.notna(row['xslg']) else 0.420,
            'bbe': int(row['bbe']),
            'source': 'live_21day'
        }
    
    # Also fetch pitcher splits (vs LHH and RHH)
    print("Fetching pitcher split data...")
    pitcher_result = {}
    
    for hand in ['L', 'R']:
        df_hand = df[df['stand'] == hand].copy()
        pitcher_stats = df_hand.groupby('pitcher_name').agg(
            bbe=('launch_speed', 'count'),
            hrs=('events', lambda x: (x == 'home_run').sum()),
            hard_hits=('is_hard_hit', 'sum'),
            fly_balls=('is_fb', 'sum'),
            xslg_sum=('estimated_slg_using_speedangle', 'sum'),
            xslg_count=('estimated_slg_using_speedangle', 'count')
        ).reset_index()
        
        pitcher_stats = pitcher_stats[pitcher_stats['bbe'] >= 8]
        pitcher_stats['est_ip'] = pitcher_stats['bbe'] / 3
        pitcher_stats['hr9'] = (pitcher_stats['hrs'] / (pitcher_stats['est_ip'] / 9)).round(2)
        pitcher_stats['hard_hit_pct'] = (pitcher_stats['hard_hits'] / pitcher_stats['bbe'] * 100).round(1)
        pitcher_stats['fb_pct'] = (pitcher_stats['fly_balls'] / pitcher_stats['bbe'] * 100).round(1)
        pitcher_stats['xslg'] = (pitcher_stats['xslg_sum'] / pitcher_stats['xslg_count']).round(3)
        
        for _, row in pitcher_stats.iterrows():
            name = row['pitcher_name']
            if name not in pitcher_result:
                pitcher_result[name] = {'source': 'live_21day'}
            suffix = 'L' if hand == 'L' else 'R'
            pitcher_result[name][f'hr9{suffix}'] = float(row['hr9'])
            pitcher_result[name][f'hh{suffix}'] = float(row['hard_hit_pct'])
            pitcher_result[name][f'fb{suffix}'] = float(row['fb_pct'])
            pitcher_result[name][f'xslg{suffix}'] = float(row['xslg']) if pd.notna(row['xslg']) else 0.380
            pitcher_result[name][f'bbe{suffix}'] = int(row['bbe'])
    
    output = {
        'generated': end_str,
        'window_start': start_str,
        'window_end': end_str,
        'batters': result,
        'pitchers': pitcher_result
    }
    
    with open('statcast.json', 'w') as f:
        json.dump(output, f)
    
    print(f"Done! {len(result)} batters, {len(pitcher_result)} pitchers saved to statcast.json")

if __name__ == '__main__':
    fetch_statcast_data()
