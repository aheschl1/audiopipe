"""Raw-SQL repository for the ``users`` table."""

from uuid import UUID

from psycopg import errors

from database.exceptions import DuplicateEmailError, NotFoundError
from database.repos.base import BaseRepo
from database.schema.users import User, UserCreate, UserWithSecret, normalize_email
from database.security import (
    hash_password,
    needs_rehash,
    verify_password,
    waste_password_time,
)

COLUMNS = "id, email, display_name, is_active, read_only, created_at, updated_at"
EMAIL_INDEX = "users_email_lower_key"


class UsersRepo(BaseRepo):
    async def create(self, data: UserCreate) -> User:
        sql = f"""
            INSERT INTO users (email, password_hash, display_name)
            VALUES (%s, %s, %s)
            RETURNING {COLUMNS}
        """
        params = (
            data.email,
            hash_password(data.password.get_secret_value()),
            data.display_name,
        )
        try:
            # A savepoint, so a duplicate email does not abort the surrounding
            # transaction and the caller can carry on.
            async with self._conn.transaction():
                user = await self._fetch_one(User, sql, params)
        except errors.UniqueViolation as exc:
            if EMAIL_INDEX in str(exc):
                raise DuplicateEmailError(data.email) from exc
            raise
        assert user is not None  # INSERT ... RETURNING always yields a row
        return user

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._fetch_one(User, f"SELECT {COLUMNS} FROM users WHERE id = %s", (user_id,))

    async def get_by_email(self, email: str) -> User | None:
        return await self._fetch_one(
            User,
            f"SELECT {COLUMNS} FROM users WHERE lower(email) = lower(%s)",
            (normalize_email(email),),
        )

    async def list(self, limit: int = 50, offset: int = 0) -> list[User]:
        return await self._fetch_all(
            User,
            f"SELECT {COLUMNS} FROM users ORDER BY created_at, id LIMIT %s OFFSET %s",
            (limit, offset),
        )

    async def authenticate(self, email: str, password: str) -> User | None:
        """Return the user when the password matches, otherwise ``None``.

        Deactivated users never authenticate. The stored hash is re-hashed when
        argon2's recommended parameters have moved on.
        """
        record = await self._fetch_one(
            UserWithSecret,
            f"SELECT {COLUMNS}, password_hash FROM users WHERE lower(email) = lower(%s)",
            (normalize_email(email),),
        )
        if record is None:
            waste_password_time()
            return None
        if not verify_password(record.password_hash, password):
            return None
        if not record.is_active:
            return None
        if needs_rehash(record.password_hash):
            await self.set_password(record.id, password)
        return User.model_validate(record.model_dump(exclude={"password_hash"}))

    async def set_password(self, user_id: UUID, password: str) -> None:
        affected = await self._execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (hash_password(password), user_id),
        )
        if affected == 0:
            raise NotFoundError("user", user_id)

    async def set_active(self, user_id: UUID, is_active: bool) -> User:
        user = await self._fetch_one(
            User,
            f"UPDATE users SET is_active = %s WHERE id = %s RETURNING {COLUMNS}",
            (is_active, user_id),
        )
        if user is None:
            raise NotFoundError("user", user_id)
        return user

    async def set_read_only(self, user_id: UUID, read_only: bool) -> User:
        user = await self._fetch_one(
            User,
            f"UPDATE users SET read_only = %s WHERE id = %s RETURNING {COLUMNS}",
            (read_only, user_id),
        )
        if user is None:
            raise NotFoundError("user", user_id)
        return user

    async def update_display_name(self, user_id: UUID, display_name: str | None) -> User:
        user = await self._fetch_one(
            User,
            f"UPDATE users SET display_name = %s WHERE id = %s RETURNING {COLUMNS}",
            (display_name, user_id),
        )
        if user is None:
            raise NotFoundError("user", user_id)
        return user

    async def delete(self, user_id: UUID) -> bool:
        """Delete a user and, by cascade, all of their tokens."""
        return await self._execute("DELETE FROM users WHERE id = %s", (user_id,)) > 0
