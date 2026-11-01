import base64
import json
import unittest

from core.chatgpt_plan import parse_accounts_check

PERSONAL = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
TEAM = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"


def jwt(account_id=None, plan=None):
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan,
        },
        "https://api.openai.com/profile": {"email": "u@example.test"},
        "exp": 9999999999,
    }
    enc = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"{head}.{enc}.sig"


def ws(account_id, plan, structure, sub_plan, active, *, name=None,
       expires=None, deactivated=False, promos=None):
    entry = {
        "account": {"account_id": account_id, "plan_type": plan, "structure": structure,
                    "name": name, "account_user_role": "account-owner",
                    "is_deactivated": deactivated},
        "entitlement": {"subscription_plan": sub_plan, "has_active_subscription": active,
                        "expires_at": expires},
        "last_active_subscription": {"will_renew": active},
    }
    if promos is not None:
        entry["eligible_promo_campaigns"] = promos
    return entry


FREE = lambda promos=None: ws(PERSONAL, "free", "personal", "chatgptfreeplan", False, promos=promos)
TEAM_WS = ws(TEAM, "team", "workspace", "chatgptteamplan", True,
             name="SoSo", expires="2026-11-30T13:08:38+00:00")


class TeamWorkspacePlanTests(unittest.TestCase):
    """Team 成员的 token 指向个人 free workspace，团队套餐挂在另一个 workspace 上。"""

    def test_team_detected_when_token_points_at_personal_workspace(self):
        resp = {"accounts": {PERSONAL: FREE(), TEAM: TEAM_WS, "default": FREE()}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "free"))
        self.assertEqual(r["current_plan_type"], "team")
        self.assertEqual(r["subscription_plan"], "chatgptteamplan")
        self.assertTrue(r["has_active_subscription"])
        self.assertEqual(r["expires_at"], "2026-11-30T13:08:38+00:00")
        self.assertEqual(r["personal_plan_type"], "free")
        self.assertEqual(r["paid_workspace_name"], "SoSo")
        self.assertEqual(r["paid_workspace_account_id"], TEAM)

    def test_token_without_account_id_still_finds_team(self):
        resp = {"accounts": {"default": FREE(), TEAM: TEAM_WS}}
        r = parse_accounts_check(resp, token=jwt())
        self.assertEqual(r["current_plan_type"], "team")

    def test_default_alias_is_not_counted_twice(self):
        resp = {"accounts": {PERSONAL: FREE(), TEAM: TEAM_WS, "default": FREE()}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "free"))
        ids = [w["account_id"] for w in r["workspace_plans"]]
        self.assertEqual(sorted(ids), sorted({PERSONAL, TEAM}))
        self.assertEqual(len(ids), 2)
        token_ws = [w for w in r["workspace_plans"] if w["is_token_workspace"]]
        self.assertEqual([w["account_id"] for w in token_ws], [PERSONAL])

    def test_plain_free_account_is_unchanged(self):
        resp = {"accounts": {PERSONAL: FREE(), "default": FREE()}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "free"))
        self.assertEqual(r["current_plan_type"], "free")
        self.assertFalse(r["has_active_subscription"])
        self.assertIsNone(r["paid_workspace_account_id"])
        self.assertEqual(len(r["workspace_plans"]), 1)

    def test_plus_trial_eligibility_still_reads_personal_workspace(self):
        promos = {"plus": {"id": "plus-1-month-free",
                           "metadata": {"discount": {"percentage": 100}}}}
        resp = {"accounts": {PERSONAL: FREE(promos), TEAM: TEAM_WS, "default": FREE(promos)}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "free"))
        # 个人 workspace 仍是 free，团队付费不应吃掉它的 Plus 试用资格。
        self.assertTrue(r["plus_trial_eligible"])
        self.assertEqual(r["plus_trial_discount_percentage"], 100)
        self.assertIn("plus", r["eligible_promo_campaigns"])
        self.assertEqual(r["current_plan_type"], "team")

    def test_deactivated_team_workspace_is_ignored(self):
        dead = ws(TEAM, "team", "workspace", "chatgptteamplan", True,
                  name="Dead", deactivated=True)
        resp = {"accounts": {PERSONAL: FREE(), TEAM: dead, "default": FREE()}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "free"))
        self.assertEqual(r["current_plan_type"], "free")
        self.assertIsNone(r["paid_workspace_account_id"])

    def test_inactive_team_subscription_is_ignored(self):
        lapsed = ws(TEAM, "team", "workspace", "chatgptteamplan", False, name="Lapsed")
        resp = {"accounts": {PERSONAL: FREE(), TEAM: lapsed, "default": FREE()}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "free"))
        self.assertEqual(r["current_plan_type"], "free")

    def test_token_workspace_wins_when_it_is_also_paid(self):
        plus_personal = ws(PERSONAL, "plus", "personal", "chatgptplusplan", True)
        resp = {"accounts": {PERSONAL: plus_personal, TEAM: TEAM_WS, "default": plus_personal}}
        r = parse_accounts_check(resp, token=jwt(PERSONAL, "plus"))
        self.assertEqual(r["current_plan_type"], "plus")
        self.assertEqual(r["paid_workspace_account_id"], PERSONAL)
        # 团队 workspace 仍然出现在列表里，供前端额外挂标签。
        team_rows = [w for w in r["workspace_plans"] if w["account_id"] == TEAM]
        self.assertEqual(len(team_rows), 1)
        self.assertEqual(team_rows[0]["plan_type"], "team")

    def test_team_only_account_under_default(self):
        resp = {"accounts": {"default": TEAM_WS}}
        r = parse_accounts_check(resp, token=jwt())
        self.assertEqual(r["current_plan_type"], "team")
        self.assertEqual(len(r["workspace_plans"]), 1)


if __name__ == "__main__":
    unittest.main()
