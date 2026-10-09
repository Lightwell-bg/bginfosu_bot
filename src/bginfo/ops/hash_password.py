"""``python -m bginfo.ops.hash_password``: спросить пароль и напечатать argon2-хеш для ADMIN_PASSWORD_HASH."""

from __future__ import annotations

import getpass
import sys

from argon2 import PasswordHasher


def main() -> int:
    pw = getpass.getpass("Пароль админки: ")
    if getpass.getpass("Повторите пароль: ") != pw:
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    if len(pw) < 10:
        print("Пароль слишком короткий (минимум 10 символов)", file=sys.stderr)
        return 1
    print(PasswordHasher().hash(pw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
