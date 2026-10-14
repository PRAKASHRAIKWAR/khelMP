from getpass import getpass

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Account, Role
from app.security import hash_password


def main() -> None:
    full_name = input("Admin full name: ").strip()
    try:
        email = validate_email(input("Admin email: ").strip(), check_deliverability=False).normalized
    except EmailNotValidError as error:
        raise SystemExit(f"Invalid email: {error}") from None
    mobile = input("Admin mobile: ").strip().replace(" ", "").replace("-", "")
    password = getpass("Admin password (12+ characters): ")
    confirmation = getpass("Confirm password: ")
    if (
        len(full_name) < 2
        or not mobile.removeprefix("+").isdigit()
        or not 10 <= len(mobile) <= 20
        or len(password) < 12
        or not any(char.islower() for char in password)
        or not any(char.isupper() for char in password)
        or not any(char.isdigit() for char in password)
        or password != confirmation
    ):
        raise SystemExit("Check the name and mobile, and use a matching 12+ character password with upper/lowercase letters and a number.")

    with SessionLocal() as session:
        existing = session.scalar(
            select(Account).where((Account.email == email) | (Account.mobile == mobile))
        )
        if existing:
            raise SystemExit("An account already uses that email or mobile number.")
        session.add(
            Account(
                full_name=full_name,
                email=email,
                mobile=mobile,
                password_hash=hash_password(password),
                role=Role.ADMIN.value,
                is_verified=True,
            )
        )
        session.commit()
    print("Administrator account created.")


if __name__ == "__main__":
    main()