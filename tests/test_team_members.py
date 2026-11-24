# -*- coding: utf-8 -*-
"""Team 成员管理：邀请、成员列表、席位占用。

席位是硬约束（通常只买 2 个）。超出席位的邀请对方点了也进不来，
白白浪费一次，所以面板必须先显示占用情况、后端也要拦。
"""
import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
SRC = (ROOT / "core" / "team_members.py").read_text(encoding="utf-8")
SESSION = (ROOT / "core" / "session.py").read_text(encoding="utf-8")


class EndpointTest(unittest.TestCase):
    """路径口径来自真实抓包，写错就是 404。"""

    def test_invite_path(self):
        self.assertIn('/backend-api/accounts/{ws}/invites', SRC)

    def test_members_path(self):
        self.assertIn('/backend-api/accounts/{ws}/users?offset=', SRC)

    def test_seat_counts_path(self):
        """注意是 /users/seat_type_counts，不是 /seat_type_counts。"""
        self.assertIn('/users/seat_type_counts', SRC)

    def test_invite_body_shape(self):
        for key in ("email_addresses", "role", "seat_type", "resend_emails"):
            self.assertIn(f'"{key}"', SRC)

    def test_revoke_uses_delete_with_body(self):
        src = inspect.getsource(
            __import__("core.team_members", fromlist=["revoke_invite"]).revoke_invite)
        self.assertIn("DELETE", src)
        self.assertIn("email_address", src)


class WorkspaceTest(unittest.TestCase):
    def test_prefers_paid_workspace(self):
        """token 里的 account_id 常指向个人 free workspace，用错会操作错对象。"""
        from core.team_members import _ws_of
        src = inspect.getsource(_ws_of)
        self.assertIn("paid_workspace_account_id", src)
        # 付费 workspace 必须先取、取到就返回，不能等到回退分支
        i = src.index("paid_workspace_account_id")
        self.assertIn("return ws", src[i:i + 200])
        # 回退到 token claims 的分支排在它后面
        self.assertLess(i, src.rindex("claims.get"))

    def test_header_overrides_account_id(self):
        """_common_headers 填的是 token 的 account_id，必须覆盖成付费 workspace。"""
        from core.team_members import _Client
        src = inspect.getsource(_Client._headers)
        self.assertIn('h["chatgpt-account-id"] = self.ws', src)


class SeatGuardTest(unittest.TestCase):
    def test_purchased_seats_not_from_maximum_seats(self):
        """maximum_seats 是平台上限 200，拿它当购买数会让"还剩几个"完全失真。"""
        from core.team_members import _purchased_seats
        src = inspect.getsource(_purchased_seats)
        self.assertIn("invoices", src)
        self.assertIn("quantity", src)
        # 用 AST 精确剥掉 docstring：注释里提 maximum_seats 是在解释为什么不用它
        tree = ast.parse(textwrap.dedent(src))
        fn = tree.body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)):
            fn.body = fn.body[1:]
        self.assertNotIn("maximum_seats", ast.unparse(fn))

    def test_pending_invites_count_as_used(self):
        """待处理邀请一旦接受就占席位，不算进去会超发。"""
        from core.team_members import fetch_overview
        src = inspect.getsource(fetch_overview)
        self.assertIn("len(members) + len(pending)", src)

    def test_invite_blocks_overflow_by_default(self):
        from core.team_members import invite_members
        sig = inspect.signature(invite_members)
        self.assertIs(sig.parameters["allow_overflow"].default, False)
        src = inspect.getsource(invite_members)
        self.assertIn("席位不够", src)

    def test_overflow_check_includes_pending(self):
        from core.team_members import invite_members
        src = inspect.getsource(invite_members)
        i = src.index("if not allow_overflow")
        body = src[i:i + 1200]
        self.assertIn("invites", body)
        self.assertIn("users", body)

    def test_validates_role_and_seat(self):
        from core.team_members import invite_members
        src = inspect.getsource(invite_members)
        self.assertIn("不支持的角色", src)
        self.assertIn("不支持的席位类型", src)


class SessionDeleteTest(unittest.TestCase):
    def test_browser_session_has_delete(self):
        """撤销邀请要 DELETE 带 body，原来只有 get/post。"""
        from core.session import BrowserSession
        self.assertTrue(hasattr(BrowserSession, "delete"))

    def test_delete_keeps_circuit_breaker(self):
        """直接用 self.session.delete 会绕过熔断和 target header 注入。"""
        from core.session import BrowserSession
        src = inspect.getsource(BrowserSession.delete)
        self.assertIn("_raise_if_circuit_open", src)
        self.assertIn("_attach_openai_target_headers_for_url", src)
        self.assertIn("_observe_response_for_circuit_breaker", src)


class RouteTest(unittest.TestCase):
    def test_routes_exist(self):
        for path in ("/api/team/members", "/api/team/invite",
                     "/api/team/revoke-invite", "/api/team/remove-member"):
            self.assertIn(f'"{path}"', APP)

    def test_members_requires_team_plan(self):
        """非 Team 账号调了只会 404/空，先给个明确提示。"""
        i = APP.index("def api_team_members")
        body = APP[i:i + 1400]
        self.assertIn('plan_type', body)
        self.assertIn("不是 Team", body)

    def test_accepts_single_email_or_list(self):
        i = APP.index("def api_team_invite")
        body = APP[i:i + 1600]
        self.assertIn("isinstance(raw, str)", body)


class UiTest(unittest.TestCase):
    def test_entry_button_only_for_team(self):
        i = HTML.index("function _planAction")
        body = HTML[i:i + 1200]
        self.assertIn("data-team-members", body)
        self.assertIn("=== 'team'", body)

    def test_click_handler_bound(self):
        self.assertIn("closest('[data-team-members]')", HTML)
        self.assertIn("openTeamMembers(", HTML)

    def test_modal_in_global_css_selector(self):
        """不进这个列表弹窗就没有 position:fixed，会垫在页面流里。"""
        self.assertIn("#teamMembersModal:not(.hidden)", HTML)

    def test_modal_in_scroll_lock(self):
        self.assertIn("$('#teamMembersModal').classList.contains('hidden')", HTML)

    def test_shows_seat_usage(self):
        self.assertIn("_teamSeatBoxHtml", HTML)
        self.assertIn("席位已满", HTML)

    def test_unknown_seat_total_is_honest(self):
        """读不到购买数时别瞎猜，说清楚是未知。"""
        i = HTML.index("function _teamSeatBoxHtml")
        body = HTML[i:i + 1400]
        self.assertIn("席位总数读不到", body)

    def test_invite_disabled_when_full(self):
        i = HTML.index("async function loadTeamMembers")
        body = HTML[i:i + 2600]
        self.assertIn("btnTeamInvite').disabled", body)

    def test_confirms_before_destructive_ops(self):
        for fn in ("_teamInvite", "_teamRevoke", "_teamRemove"):
            i = HTML.index(f"async function {fn}")
            body = HTML[i:i + 900]
            self.assertIn("confirm(", body, f"{fn} 没有确认框")

    def test_owner_cannot_be_removed(self):
        """拥有者移除不了，不该给按钮。"""
        i = HTML.index("async function loadTeamMembers")
        body = HTML[i:i + 2600]
        self.assertIn("m.is_owner", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
