import unittest
from pathlib import Path


STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


class AccessControlPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_ROOT / "access-control.html").read_text(encoding="utf-8")
        cls.css = (STATIC_ROOT / "auth-admin.css").read_text(encoding="utf-8")
        cls.javascript = (STATIC_ROOT / "auth-admin.js").read_text(encoding="utf-8")

    def test_uses_two_focused_tabs_and_persistent_action_rail(self) -> None:
        self.assertIn('data-access-tab="permissions"', self.html)
        self.assertIn('data-access-tab="authorization"', self.html)
        self.assertIn('class="access-actions"', self.html)
        self.assertIn('id="access-save-button"', self.html)
        self.assertNotIn('class="page-heading"', self.html)

    def test_pin_editor_is_inside_the_pin_authorization_choice(self) -> None:
        pin_choice = self.html.index('name="winpe-auth-mode" value="pin"')
        pin_editor = self.html.index('class="pin-editor"')
        no_auth_choice = self.html.index('name="winpe-auth-mode" value="none"')
        self.assertLess(pin_choice, pin_editor)
        self.assertLess(pin_editor, no_auth_choice)

    def test_permissions_use_compact_user_selector_without_user_sidebar(self) -> None:
        self.assertIn('id="access-user-select"', self.html)
        self.assertIn('id="access-list"', self.html)
        self.assertNotIn('class="access-users-sidebar"', self.html)
        self.assertIn("function renderSelectedUser()", self.javascript)
        self.assertIn("function syncExclusiveDeploymentAccess", self.javascript)

    def test_dark_workspace_is_scoped_to_access_control(self) -> None:
        self.assertIn("body.access-control-page", self.css)
        self.assertIn(".access-workspace", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 250px", self.css)
        self.assertIn(".authorization-option:has(input:checked)", self.css)


if __name__ == "__main__":
    unittest.main()
