"""
Seed the `managers` table with the 12 league members.

Idempotent (INSERT OR IGNORE keyed on the UNIQUE name column) - safe to
rerun, e.g. if a manager's name was mistyped and you're adding the
corrected one alongside it before manually deleting the old row.

Usage:
    python seed_league.py "Alice" "Bob" "Carol" ... (12 names)
"""

from __future__ import annotations

import argparse

from engine.db import get_connection
from engine.ownership import list_managers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="+", help="Manager names, e.g. 12 names for this league")
    args = parser.parse_args()

    conn = get_connection()
    for name in args.names:
        conn.execute("INSERT OR IGNORE INTO managers (name) VALUES (?)", (name,))
    conn.commit()

    managers = list_managers(conn)
    print(f"managers table now has {len(managers)} manager(s):")
    for m in managers:
        print(f"  {m['manager_id']:>3}  {m['name']}")

    conn.close()


if __name__ == "__main__":
    main()
