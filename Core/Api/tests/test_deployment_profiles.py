import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.deployment_profiles import (
    DeploymentProfileError,
    load_default_profile,
    update_default_profile,
)
from app.deployments import Base


class DeploymentProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_default_profile_is_created_with_safe_defaults(self) -> None:
        with Session(self.engine) as session:
            profile = load_default_profile(session)

            self.assertEqual(profile["profileName"], "Default")
            self.assertEqual(profile["localAdminName"], "localadmin")
            self.assertTrue(profile["enableBuiltInAdministrator"])
            self.assertTrue(profile["enableSetupLocalAdmin"])

    def test_default_profile_settings_are_persisted(self) -> None:
        with Session(self.engine) as session:
            updated = update_default_profile(
                session,
                {
                    "localAdminName": "deployadmin",
                    "enableBuiltInAdministrator": False,
                    "enableSetupLocalAdmin": False,
                },
            )
            session.commit()

        with Session(self.engine) as session:
            loaded = load_default_profile(session)

        self.assertEqual(updated, loaded)
        self.assertEqual(loaded["localAdminName"], "deployadmin")
        self.assertFalse(loaded["enableBuiltInAdministrator"])
        self.assertFalse(loaded["enableSetupLocalAdmin"])

    def test_invalid_local_admin_name_is_rejected(self) -> None:
        with Session(self.engine) as session:
            with self.assertRaises(DeploymentProfileError):
                update_default_profile(session, {"localAdminName": "bad name"})


if __name__ == "__main__":
    unittest.main()
