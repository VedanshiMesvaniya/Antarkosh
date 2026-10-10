import sqlite3
import sys
from pathlib import Path

import pandas as pd

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from scripts._paths import REPO_ROOT


repo_root = REPO_ROOT
csv_path = repo_root / "data" / "nvidia_gpu_sales_synthetic_2026.csv"
db_path = repo_root / "data" / "live_data.db"

def main():
    print(f"Loading {csv_path.name} into {db_path}...")
    df = pd.read_csv(csv_path)
    
    # Standardize column names to be SQL friendly
    df.columns = [c.lower().replace(" ", "_").replace("-", "_") for c in df.columns]
    
    with sqlite3.connect(db_path) as conn:
        df.to_sql("gpu_sales", conn, if_exists="replace", index=False)
        
    print(f"Loaded {len(df)} rows into 'gpu_sales' table.")

if __name__ == "__main__":
    main()
