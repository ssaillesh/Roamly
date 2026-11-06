"""Read-only: which stored venues the office rule keeps out of plans, with a sample to read
by hand (are they really offices?). Run it before changing venue_index._OFFICE_NAME.

    python -m scripts.audit_nonvenues [--sample 40] [--seed 1]
"""
import argparse
import random
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Venue
from app.services.venue_index import is_office


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    with SessionLocal() as db:
        rows = db.execute(select(Venue.name, Venue.category, Venue.website, Venue.notability, Venue.slots)).all()
    flagged = [r for r in rows if is_office(r.name)]
    print(f"{len(rows)} stored venues; {len(flagged)} kept out as offices "
          f"({sum(1 for r in flagged if (r.notability or 0) > 0)} of them well known: check those first)")
    print("top categories:", ", ".join(f"{c} {n}" for c, n in Counter(r.category for r in flagged).most_common(10)))
    for r in random.Random(args.seed).sample(flagged, min(args.sample, len(flagged))):
        print(f"  - {r.name}  [{r.category}; {'site' if r.website else 'no site'}; slots {','.join(r.slots or [])}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
