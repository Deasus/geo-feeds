"""
geo-feeds AQI Data Pipeline
============================
Fetches global PM2.5 air quality data from Open-Meteo Air Quality API.
Outputs a dense grid of readings as JSON for geo-feeds to render as haze overlay.

Grid: 2° spacing globally = ~5,400 points (much denser than browser-side fetch)
Updates: Every 30 minutes via GitHub Actions
Cost: $0 (Open-Meteo is free, no API key)
"""

import json
import os
import time
import requests
from datetime import datetime, timezone

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def fetch_aqi_grid():
    """Fetch global PM2.5 grid at 2° resolution."""
    
    print("=" * 60)
    print("geo-feeds AQI Data Pipeline")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    
    # Generate global grid at 4° spacing
    # ~2,700 points — fits within 10-min GitHub Actions timeout
    # Latitude: -50 to 70 (skip extreme poles)
    # Longitude: -180 to 180
    points = []
    for lat in range(-50, 72, 4):
        for lng in range(-180, 182, 4):
            if lng > 180:
                continue
            points.append({'lat': lat, 'lng': lng})
    
    print(f"\nGrid: {len(points)} points at 4° spacing")
    
    # Fetch in batches of 50 (Open-Meteo limit per request)
    all_readings = []
    batch_size = 50
    total_batches = (len(points) + batch_size - 1) // batch_size
    
    for i in range(0, len(points), batch_size):
        batch = points[i:i+batch_size]
        batch_num = i // batch_size + 1
        
        lat_str = ','.join(str(p['lat']) for p in batch)
        lng_str = ','.join(str(p['lng']) for p in batch)
        
        url = (
            f'https://air-quality-api.open-meteo.com/v1/air-quality?'
            f'latitude={lat_str}'
            f'&longitude={lng_str}'
            f'&current=pm2_5,pm10,us_aqi,carbon_monoxide,nitrogen_dioxide,ozone'
        )
        
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                results = data if isinstance(data, list) else [data]
                
                for r in results:
                    if r and r.get('current'):
                        pm25 = r['current'].get('pm2_5')
                        if pm25 is not None:
                            reading = {
                                'lat': r['latitude'],
                                'lng': r['longitude'],
                                'pm25': round(pm25, 1),
                                'aqi': r['current'].get('us_aqi', 0),
                            }
                            # Only include pm10 if significantly different from pm25
                            pm10 = r['current'].get('pm10')
                            if pm10 and pm10 > pm25 * 1.5:
                                reading['pm10'] = round(pm10, 1)
                            all_readings.append(reading)
                
                if batch_num % 20 == 0 or batch_num == total_batches:
                    print(f"  Batch {batch_num}/{total_batches}: {len(all_readings)} readings so far")
            
            elif resp.status_code == 429:
                print(f"  Batch {batch_num}: Rate limited — waiting 5s")
                time.sleep(5)
                # Retry
                resp2 = requests.get(url, timeout=30)
                if resp2.status_code == 200:
                    data = resp2.json()
                    results = data if isinstance(data, list) else [data]
                    for r in results:
                        if r and r.get('current'):
                            pm25 = r['current'].get('pm2_5')
                            if pm25 is not None:
                                all_readings.append({
                                    'lat': r['latitude'],
                                    'lng': r['longitude'],
                                    'pm25': round(pm25, 1),
                                    'aqi': r['current'].get('us_aqi', 0),
                                })
            else:
                print(f"  Batch {batch_num}: HTTP {resp.status_code}")
        
        except Exception as e:
            print(f"  Batch {batch_num} failed: {e}")
        
        # Small delay every 30 batches to avoid rate limiting
        if batch_num % 30 == 0:
            time.sleep(0.3)
    
    print(f"\nTotal readings: {len(all_readings)}")
    
    if not all_readings:
        print("ERROR: No AQI data fetched!")
        return
    
    # Compute stats
    pm25_values = [r['pm25'] for r in all_readings if r['pm25'] > 0]
    max_pm25 = max(pm25_values) if pm25_values else 0
    avg_pm25 = sum(pm25_values) / len(pm25_values) if pm25_values else 0
    unhealthy = len([v for v in pm25_values if v > 35])
    hazardous = len([v for v in pm25_values if v > 150])
    
    # Build output — compact format to minimize file size
    # Instead of full objects, use arrays: [lat, lng, pm25, aqi]
    compact_grid = []
    for r in all_readings:
        compact_grid.append([r['lat'], r['lng'], r['pm25'], r.get('aqi', 0)])
    
    output = {
        'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'resolution_deg': 4,
        'total_points': len(compact_grid),
        'stats': {
            'max_pm25': round(max_pm25, 1),
            'avg_pm25': round(avg_pm25, 1),
            'unhealthy_points': unhealthy,
            'hazardous_points': hazardous,
        },
        'grid': compact_grid
    }
    
    # Write main file
    feed_path = os.path.join(DATA_DIR, 'current-aqi.json')
    with open(feed_path, 'w') as f:
        json.dump(output, f, separators=(',', ':'))
    
    size_kb = os.path.getsize(feed_path) / 1024
    
    # Write metadata
    meta = {
        'updated': output['updated'],
        'total_points': output['total_points'],
        'stats': output['stats'],
        'source': 'Open-Meteo Air Quality API (air-quality-api.open-meteo.com)'
    }
    with open(os.path.join(DATA_DIR, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)
    
    print(f"\n{'='*60}")
    print(f"Output: {feed_path} ({size_kb:.0f} KB)")
    print(f"Grid points: {len(compact_grid)}")
    print(f"Max PM2.5: {max_pm25:.1f} µg/m³")
    print(f"Avg PM2.5: {avg_pm25:.1f} µg/m³")
    print(f"Unhealthy (>35): {unhealthy} points")
    print(f"Hazardous (>150): {hazardous} points")
    print("Done!")

if __name__ == "__main__":
    fetch_aqi_grid()
