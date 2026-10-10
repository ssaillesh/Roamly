from pydantic import BaseModel, EmailStr, Field


class UserPublic(BaseModel):
    id: str
    username: str
    display_name: str
    avatar_url: str | None = None
    bio: str | None = None
    home_city: str | None = None


class UserProfile(UserPublic):
    email: str | None = None
    is_admin: bool = False      # the web app shows the admin link when true
    needs_terms: bool = False   # hasn't accepted the current Terms & Privacy Policy yet


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=300)
    avatar_url: str | None = Field(default=None, max_length=500)
    home_city: str | None = Field(default=None, max_length=100)
