"""Make an account an admin (or take admin away).

The API never lets anyone change is_admin, so this is the only way in. Run it
from backend/ locally, or in the Render shell for production:

    python -m scripts.make_admin you@example.com
    python -m scripts.make_admin you@example.com --remove
"""
import sys

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import AdminAuditLog, User


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    remove = "--remove" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    email = args[0].strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if not user:
            print(f"No account with email {email}. Sign up in the app first.")
            return 1
        user.is_admin = not remove
        db.add(AdminAuditLog(admin_id=None, action="revoke_admin" if remove else "grant_admin",
                             target=user.email, details={"via": "scripts/make_admin.py"}))
        db.commit()
        print(f"{'Removed admin from' if remove else 'Made admin:'} {user.email} (@{user.username})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
