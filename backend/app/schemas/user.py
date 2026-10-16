from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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
    # Only these fields can be changed here; anything else (is_admin, username, a
    # password…) is refused rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None
    display_name: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=300)
    avatar_url: str | None = Field(default=None, max_length=500)
    home_city: str | None = Field(default=None, max_length=100)
    # Needed to change the email (where password resets go); checked, never stored.
    current_password: str | None = Field(default=None, max_length=128)

    @field_validator("email", "display_name")
    @classmethod
    def _not_null(cls, v):
        # Only runs for values actually sent: an explicit null would blank a required column.
        if v is None:
            raise ValueError("can't be empty")
        return v
