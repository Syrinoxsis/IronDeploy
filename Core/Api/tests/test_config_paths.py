import unittest

from app.config import API_ROOT, IRONDEPLOY_ROOT, _expand_irondeploy_root


def read_example_value(name: str) -> str:
    example_path = API_ROOT / ".env.example"
    for line in example_path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{name} is missing from .env.example")


class PortablePathTests(unittest.TestCase):
    def test_runtime_state_examples_live_below_irondeploy_root(self) -> None:
        self.assertEqual(API_ROOT, IRONDEPLOY_ROOT / "Api")
        self.assertEqual(
            _expand_irondeploy_root(read_example_value("IRONAPI_DATABASE_URL")),
            f"sqlite:///{IRONDEPLOY_ROOT.as_posix()}/Data/irondeploy.db",
        )
        self.assertEqual(
            _expand_irondeploy_root(read_example_value("IRONAPI_ODJ_BLOB_DIR")),
            f"{IRONDEPLOY_ROOT.as_posix()}\\ODJ\\pending",
        )

    def test_root_token_expands_after_directory_move(self) -> None:
        value = _expand_irondeploy_root(
            "sqlite:///{IRONDEPLOY_ROOT}/Data/irondeploy.db"
        )
        self.assertEqual(
            value,
            f"sqlite:///{IRONDEPLOY_ROOT.as_posix()}/Data/irondeploy.db",
        )


if __name__ == "__main__":
    unittest.main()
