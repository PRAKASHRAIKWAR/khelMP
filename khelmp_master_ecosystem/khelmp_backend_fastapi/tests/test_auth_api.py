import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app import api as api_module
from app.database import Base, get_db
from app.main import app
from app.models import Account, Role
from app.security import hash_password
from app.storage import LocalStorage


class AuthApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)
        settings.auto_create_schema = False
        settings.jwt_secret_key = "test-signing-secret-that-is-long-enough"
        cls.original_storage = api_module.storage
        cls.upload_directory = TemporaryDirectory()
        api_module.storage = LocalStorage(Path(cls.upload_directory.name))

        def override_get_db():
            with cls.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        app.dependency_overrides.clear()
        api_module.storage = cls.original_storage
        cls.upload_directory.cleanup()
        settings.jwt_secret_key = ""
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        with self.session_factory() as session:
            session.add(
                Account(
                    full_name="Platform Admin",
                    email="admin@example.com",
                    mobile="9876543210",
                    password_hash=hash_password("SecureAdminPassword123"),
                    role=Role.ADMIN.value,
                    is_verified=True,
                )
            )
            session.commit()

    def register_player(self):
        return self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Aarav Player",
                "email": "aarav@example.com",
                "mobile": "9876543211",
                "password": "SecurePlayerPassword123",
                "confirm_password": "SecurePlayerPassword123",
                "role": "PLAYER",
                "date_of_birth": "2005-05-14",
                "gender": "male",
                "state": "Madhya Pradesh",
                "district": "Bhopal",
                "sport": "Hockey",
            },
        )

    def token_for(self, identifier, password):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"identifier": identifier, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def test_player_signup_hashes_password_and_jwt_authenticates(self):
        response = self.register_player()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("password", response.json())
        with self.session_factory() as session:
            player = session.query(Account).filter_by(email="aarav@example.com").one()
            self.assertNotEqual(player.password_hash, "SecurePlayerPassword123")

        token = self.token_for("aarav@example.com", "SecurePlayerPassword123")
        profile = self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["role"], Role.PLAYER.value)

    def test_player_cannot_use_selector_search(self):
        self.assertEqual(self.register_player().status_code, 201)
        token = self.token_for("aarav@example.com", "SecurePlayerPassword123")
        response = self.client.get("/api/v1/players", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 403)

    def test_admin_role_is_not_available_in_public_signup(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={"full_name": "Attacker", "email": "attacker@example.com", "mobile": "9876543212", "password": "SecurePassword123", "confirm_password": "SecurePassword123", "role": "ADMIN"},
        )
        self.assertEqual(response.status_code, 422)

    def test_institutional_account_requires_admin_verification(self):
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Selector Person",
                "email": "selector@example.com",
                "mobile": "9876543213",
                "password": "SecureSelectorPassword123",
                "confirm_password": "SecureSelectorPassword123",
                "role": "SELECTOR",
                "organization": "District Sports Office",
                "experience_years": 8,
                "state": "Madhya Pradesh",
                "district": "Bhopal",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        pending_login = self.client.post(
            "/api/v1/auth/login",
            json={"identifier": "selector@example.com", "password": "SecureSelectorPassword123"},
        )
        self.assertEqual(pending_login.status_code, 403)

        admin_token = self.token_for("admin@example.com", "SecureAdminPassword123")
        verified = self.client.patch(
            f"/api/v1/admin/accounts/{response.json()['id']}/verification",
            json={"is_verified": True},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        selector_token = self.token_for("selector@example.com", "SecureSelectorPassword123")
        players = self.client.get("/api/v1/players", headers={"Authorization": f"Bearer {selector_token}"})
        self.assertEqual(players.status_code, 200)

    def test_uploaded_media_is_private_to_its_owner(self):
        self.assertEqual(self.register_player().status_code, 201)
        owner_token = self.token_for("aarav@example.com", "SecurePlayerPassword123")
        upload = self.client.post(
            "/api/v1/media",
            headers={"Authorization": f"Bearer {owner_token}"},
            files={"upload": ("profile.png", b"\x89PNG\r\n\x1a\nvalid-test-image", "image/png")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        asset_id = upload.json()["id"]
        own_file = self.client.get(f"/api/v1/media/{asset_id}", headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(own_file.status_code, 200)
        self.assertTrue(own_file.content.startswith(b"\x89PNG"))

        second_player = self.client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Another Player",
                "email": "other@example.com",
                "mobile": "9876543214",
                "password": "SecureOtherPassword123",
                "confirm_password": "SecureOtherPassword123",
                "role": "PLAYER",
                "date_of_birth": "2004-01-01",
                "gender": "female",
                "state": "Madhya Pradesh",
                "district": "Indore",
                "sport": "Hockey",
            },
        )
        self.assertEqual(second_player.status_code, 201, second_player.text)
        other_token = self.token_for("other@example.com", "SecureOtherPassword123")
        private_file = self.client.get(f"/api/v1/media/{asset_id}", headers={"Authorization": f"Bearer {other_token}"})
        self.assertEqual(private_file.status_code, 404)