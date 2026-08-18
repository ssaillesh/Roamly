"""Import all models so Alembic/metadata can discover them."""
from app.models.user import User

__all__ = ["User"]
