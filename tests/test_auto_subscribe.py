# -*- coding: utf-8 -*-
"""自动订阅：定时给单席位账号开 Team。

需求（2026-11-18）：
  每到新加坡时间 10/12/14/16/18/20 点检查一次账号池，有未订阅的单席位
  账号就自动订阅。订阅时随机选一张 VCC 卡，按卡号查该卡登记的账单地址
  （GET https://ssr.im/address?id={卡号}），完成支付。

关键约束：
  - 会真实扣款，所以默认关闭，且手动触发必须显式 confirm
  - 一次只订一个：扣款不可逆，止损面最小
  - 账单地址必须按卡取：每张卡地址不同，配置里只能写死一份，
    对不上 Stripe 直接拒付
"""
import datetime
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

from core import auto_subscribe_scheduler as sched  # noqa: E402

_TZ = datetime.timezone(datetime.timedelta(hours=8))


class HoursTest(unittest.TestCase):
    def test_default_hours(self):
        with patch.object(sched, "_cfg", lambda n, d: d):
            self.assertEqual(sched._hours(), [10, 12, 14, 16, 18, 20, 22])

    def test_parses_comma_string(self):
        with patch.object(sched, "_cfg", lambda n, d: "9, 13 ,21"):
            self.assertEqual(sched._hours(), [9, 13, 21])

    def test_tolerates_junk(self):
        """配错一个字段不该让整个调度停摆。"""
        with patch.object(sched, "_cfg", lambda n, d: "10,abc,,99,-3,12"):
            self.assertEqual(sched._hours(), [10, 12])

    def test_dedupes_and_sorts(self):
        with patch.object(sched, "_cfg", lambda n, d: "20,10,20,12"):
            self.assertEqual(sched._hours(), [10, 12, 20])

    def test_accepts_list_too(self):
        with patch.object(sched, "_cfg", lambda n, d: [10, 14]):
            self.assertEqual(sched._hours(), [10, 14])


class NextRunTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(sched, "_cfg",
                         lambda n, d: "10,12,14,16,18,20"
                         if n == "AUTO_SUBSCRIBE_HOURS" else d)
        p.start()
        self.addCleanup(p.stop)

    def test_picks_next_slot_today(self):
        now = datetime.datetime(2026, 11, 18, 11, 30, tzinfo=_TZ)
        self.assertEqual(sched.next_run_at(now).hour, 12)

    def test_rolls_to_tomorrow(self):
        now = datetime.datetime(2026, 11, 18, 21, 0, tzinfo=_TZ)
        nxt = sched.next_run_at(now)
        self.assertEqual(nxt.hour, 10)
        self.assertEqual(nxt.day, 19)

    def test_exact_hour_goes_to_next(self):
        """正好 12:00 时下一次是 14 点，不是又一个 12 点。"""
        now = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=_TZ)
        self.assertEqual(sched.next_run_at(now).hour, 14)

    def test_uses_singapore_tz(self):
        import inspect
        src = inspect.getsource(sched)
        self.assertIn("timedelta(hours=8)", src)


class CandidateTest(unittest.TestCase):
    """筛选条件：支持单席位 + 未订阅 + 可用。"""

    def _run(self, rows):
        with patch("core.db.list_accounts", return_value={"items": rows}):
            return sched.find_candidates()

    def _acc(self, **kw):
        base = {"id": 1, "email": "a@e.test", "single_seat_supported": True,
                "access_token": "tok", "current_plan_type": "free"}
        base.update(kw)
        return base

    def test_picks_single_seat_unpaid(self):
        got = self._run([self._acc()])
        self.assertEqual(len(got), 1)

    def test_skips_multi_seat(self):
        got = self._run([self._acc(single_seat_supported=False)])
        self.assertEqual(got, [])

    def test_skips_unprobed(self):
        """没探过席位的不订——可能是 2 席，会多花一倍钱。"""
        got = self._run([self._acc(single_seat_supported=None)])
        self.assertEqual(got, [])

    def test_skips_already_paid(self):
        got = self._run([self._acc(paid_at="2026-11-18T10:00:00")])
        self.assertEqual(got, [])

    def test_skips_with_subscription_no(self):
        got = self._run([self._acc(subscription_no=81)])
        self.assertEqual(got, [])

    def test_skips_non_free_plan(self):
        got = self._run([self._acc(current_plan_type="team")])
        self.assertEqual(got, [])

    def test_skips_deactivated(self):
        got = self._run([self._acc(live_check_status="deactivated")])
        self.assertEqual(got, [])

    def test_skips_without_token(self):
        got = self._run([self._acc(access_token="")])
        self.assertEqual(got, [])

    def test_sorted_by_id(self):
        """先建先订，别让新号插队把老号饿死。"""
        got = self._run([self._acc(id=9), self._acc(id=3), self._acc(id=5)])
        self.assertEqual([r["id"] for r in got], [3, 5, 9])


class RunOnceTest(unittest.TestCase):
    def _cfg(self, **over):
        d = {"AUTO_SUBSCRIBE_ENABLED": True,
             "AUTO_SUBSCRIBE_HOURS": "10,12"}
        d.update(over)
        return lambda n, dd: d.get(n, dd)

    def test_disabled_does_nothing(self):
        with patch.object(sched, "is_enabled", return_value=False):
            r = sched.run_once()
        self.assertFalse(r["acted"])
        self.assertIn("未启用", r["reason"])

    def test_off_hour_skipped(self):
        now = datetime.datetime(2026, 11, 18, 11, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now):
            r = sched.run_once()
        self.assertFalse(r["acted"])
        self.assertIn("不在计划内", r["reason"])

    def test_same_slot_not_repeated(self):
        """一个整点内只触发一次，别因为每分钟轮询就订一堆。"""
        now = datetime.datetime(2026, 11, 18, 12, 30, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", return_value="2026-11-18T12"):
            r = sched.run_once()
        self.assertFalse(r["acted"])
        self.assertIn("已处理过", r["reason"])

    def test_skips_when_payment_running(self):
        now = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", return_value=""), \
             patch.object(sched, "_busy", return_value="已有支付会话"):
            r = sched.run_once()
        self.assertFalse(r["acted"])

    def test_only_one_per_slot(self):
        """有多个候选时也只订一个。"""
        now = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=_TZ)
        start = MagicMock(return_value={"ok": True, "session_id": "s1"})
        cands = [{"id": i, "email": "a%d@e.test" % i} for i in (1, 2, 3)]
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", return_value=""), \
             patch("core.db.set_meta"), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=cands), \
             patch.object(sched, "start_subscription", start):
            r = sched.run_once()
        self.assertTrue(r["acted"])
        start.assert_called_once_with(1)

    def test_force_ignores_hour(self):
        now = datetime.datetime(2026, 11, 18, 3, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=[]):
            r = sched.run_once(force=True)
        self.assertNotIn("不在计划内", r["reason"])

    def test_subscription_error_is_caught(self):
        """订阅炸了不能把调度线程带崩。"""
        now = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", return_value=""), \
             patch("core.db.set_meta"), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates",
                          return_value=[{"id": 1, "email": "a@e.test"}]), \
             patch.object(sched, "start_subscription",
                          side_effect=RuntimeError("炸了")):
            r = sched.run_once()
        self.assertFalse(r["ok"])
        self.assertIn("炸了", r["reason"])


class DeadCandidateRetryTest(unittest.TestCase):
    """2026-11-18 事故：候选第一个账号 token 被吊销，18:00/20:00 两个整点全废。

    #104 的 token 被 OpenAI 吊销（token_revoked HTTP 401），但它既没查过活
    也没被标记，于是稳居候选队首。旧逻辑只试 cands[0]，失败就 return 并占用
    整点，导致后面 5 个活号一次都没轮到。修复后必须：遇死号换下一个、把死号
    标记掉、且非死号失败时不占用整点。
    """

    def _cfg(self):
        d = {"AUTO_SUBSCRIBE_ENABLED": True, "AUTO_SUBSCRIBE_HOURS": "10,12"}
        return lambda n, dd: d.get(n, dd)

    def _run(self, start_side_effect, cands, *, force=False):
        now = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=_TZ)
        start = MagicMock(side_effect=start_side_effect)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", return_value=""), \
             patch("core.db.set_meta") as set_meta, \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=cands), \
             patch.object(sched, "start_subscription", start), \
             patch.object(sched, "_mark_account_dead") as mark_dead:
            r = sched.run_once(force=force)
        return r, start, set_meta, mark_dead

    def _cands(self, *ids):
        return [{"id": i, "email": "a%d@e.test" % i} for i in ids]

    def test_dead_first_candidate_falls_through_to_next(self):
        """第一个是死号 -> 标记它，并继续用第二个真正订阅。"""
        def side(acc_id):
            if acc_id == 104:
                return {"ok": False,
                        "error": "生成长链失败：Encountered invalidated oauth token "
                                 "for user, failing request [token_revoked] [HTTP 401]"}
            return {"ok": True, "session_id": "s106"}

        r, start, set_meta, mark_dead = self._run(side, self._cands(104, 106))
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], 106)
        # 死号被标记，活号没被误标
        mark_dead.assert_called_once()
        self.assertEqual(mark_dead.call_args[0][0], 104)
        self.assertEqual([c.args[0] for c in start.call_args_list], [104, 106])

    def test_dead_account_is_marked_before_retry(self):
        """标记必须发生在换下一个之前，否则下次还会先撞它。"""
        order = []

        def side(acc_id):
            if acc_id == 104:
                return {"ok": False, "error": "token_revoked"}
            order.append("started-106")
            return {"ok": True}

        now = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", return_value=""), \
             patch("core.db.set_meta"), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=self._cands(104, 106)), \
             patch.object(sched, "start_subscription", side), \
             patch.object(sched, "_mark_account_dead",
                          side_effect=lambda *a, **k: order.append("marked-104")):
            sched.run_once()
        self.assertEqual(order, ["marked-104", "started-106"])

    def test_all_dead_candidates_stops_and_occupies_slot(self):
        """全是死号：标记每个，并占用整点避免本小时内空转。"""
        r, start, set_meta, mark_dead = self._run(
            lambda acc_id: {"ok": False, "error": "account_deactivated"},
            self._cands(1, 2, 3))
        self.assertFalse(r["ok"])
        self.assertFalse(r["acted"])
        self.assertEqual(mark_dead.call_count, 3)
        self.assertEqual(start.call_count, 3)
        slot_writes = [c for c in set_meta.call_args_list
                       if c.args and c.args[0] == sched._DONE_SLOT_KEY]
        self.assertEqual(len(slot_writes), 1, "全死号要占用整点")

    def test_non_dead_error_stops_without_consuming_slot(self):
        """环境类失败要轮转换号继续试，整轮没成不占整点。

        尝试时间戳（auto_sub_last_attempt）和游标（auto_sub_cursor_id）
        允许写入；整点键（auto_sub_last_slot）绝不能写。
        """
        r, start, set_meta, mark_dead = self._run(
            lambda acc_id: {"ok": False, "error": "拿不到 EG 出口，无法保证结算国家一致"},
            self._cands(104, 106))
        self.assertFalse(r["ok"])
        self.assertTrue(r["acted"])
        # 环境失败换下一个号接着试，不是停在第一个
        self.assertEqual([c.args[0] for c in start.call_args_list], [104, 106])
        mark_dead.assert_not_called()
        slot_writes = [c for c in set_meta.call_args_list
                       if c.args and c.args[0] == sched._DONE_SLOT_KEY]
        self.assertEqual(slot_writes, [], "失败后不该占用整点")

    def test_non_dead_error_does_not_mark_account_dead(self):
        """金额闸门拦截不是账号死亡，绝不能把好号标成废号。"""
        self._run(lambda acc_id: {"ok": False,
                                  "error": "金额超限：EGP 1200.00 > 上限 EGP 920.00"},
                  self._cands(110))
        _, _, _, mark_dead = self._run(lambda acc_id: {"ok": False, "error": "金额超限"},
                                       self._cands(110))
        mark_dead.assert_not_called()

    def test_dead_error_detection(self):
        for text in ("token_revoked", "[token_revoked] [HTTP 401]",
                     "Encountered invalidated oauth token for user",
                     "account_deactivated", "account has been deactivated"):
            self.assertTrue(sched._is_dead_account_error(text), text)
        for text in ("", "拿不到 EG 出口", "金额超限", "OTP 超时（>90s）",
                     "no available card", "结算国家不一致"):
            self.assertFalse(sched._is_dead_account_error(text), text)

    def test_find_candidates_skips_known_dead_error(self):
        """查活已读到死号特征的，别再进候选队列。"""
        rows = [
            {"id": 1, "single_seat_supported": True, "access_token": "t",
             "current_plan_type": "free", "live_check_error": "[token_revoked] [HTTP 401]"},
            {"id": 2, "single_seat_supported": True, "access_token": "t",
             "current_plan_type": "free"},
        ]
        with patch("core.db.list_accounts", return_value={"items": rows}):
            got = sched.find_candidates()
        self.assertEqual([r["id"] for r in got], [2])

    def test_find_candidates_keeps_otp_timeout_account(self):
        """OTP 超时只是"没查成"，账号可能还活着，不能剔出候选。"""
        rows = [{"id": 3, "single_seat_supported": True, "access_token": "t",
                 "current_plan_type": "free",
                 "live_check_error": "等待 x 的 OTP 超时（>90s）"}]
        with patch("core.db.list_accounts", return_value={"items": rows}):
            got = sched.find_candidates()
        self.assertEqual([r["id"] for r in got], [3])


class AddressMismatchTest(unittest.TestCase):
    """城市和州对不上的地址会被多收税，要在选卡阶段筛掉。

    实测（2026-11-18）：1588 卡地址是 "Philadelphia, DE"，而费城在宾州，
    Stripe 按 PA 收 8% 税，920 的八折单变成 993.60，撞上金额闸门。
    数据源侧没有修正这条，所以代码要能自己识别。
    """

    def _addr(self, city, state):
        body = json.dumps({
            "full_name": "A", "street": "S", "city": city, "state": state,
            "state_name": "X", "zip_code": "12345", "country": "US",
        }).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        return resp

    def test_flags_philadelphia_de(self):
        from core.card_address import fetch_card_address
        with patch("urllib.request.urlopen",
                   return_value=self._addr("Philadelphia", "DE")):
            got = fetch_card_address("4746130000001588")
        self.assertTrue(got["state_mismatch"])

    def test_accepts_matching_city(self):
        from core.card_address import fetch_card_address
        with patch("urllib.request.urlopen",
                   return_value=self._addr("Philadelphia", "PA")):
            got = fetch_card_address("4746130000001588")
        self.assertFalse(got["state_mismatch"])

    def test_accepts_unknown_city(self):
        """没收录的城市不做判断，宁可放行也不误杀。"""
        from core.card_address import fetch_card_address
        with patch("urllib.request.urlopen",
                   return_value=self._addr("New Castle", "DE")):
            got = fetch_card_address("4746130000001771")
        self.assertFalse(got["state_mismatch"])

    def test_case_insensitive(self):
        from core.card_address import fetch_card_address
        with patch("urllib.request.urlopen",
                   return_value=self._addr("PHILADELPHIA", "DE")):
            got = fetch_card_address("4746130000001588")
        self.assertTrue(got["state_mismatch"])

    def test_selection_skips_mismatched(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("_addr_is_payable", src)

    def test_tries_other_cards(self):
        """一张卡地址不行要换下一张，不能卡在同一张坏卡上。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        i = src.index("_addr_is_payable(client, pick)")
        seg = src[i:i + 700]
        self.assertIn("for _", seg)
        self.assertIn("rest", seg)

    def test_payable_check_handles_errors(self):
        """取地址失败要当成不可用，不能抛异常把订阅带崩。"""
        client = MagicMock()
        client.card_secrets.side_effect = RuntimeError("接口挂了")
        self.assertFalse(sched._addr_is_payable(
            client, {"cardId": "x", "cardNoMask": "1234"}))


class CardUsageTest(unittest.TestCase):
    """卡使用次数统计 + 均衡选卡。

    需求：随机选卡时从使用次数最少的几张里选，保证每张卡用得均衡。
    """

    def _card(self, tail, bal=50.0):
        return {"cardId": "id-" + tail, "cardNoMask": "537872****" + tail,
                "balance": bal, "cardStatus": "ACTIVE"}

    def test_reads_empty_as_zero(self):
        with patch("core.db.get_meta", return_value=None):
            self.assertEqual(sched.card_usage(), {})

    def test_parses_counts(self):
        with patch("core.db.get_meta", return_value='{"1771": 3, "9735": 1}'):
            self.assertEqual(sched.card_usage(), {"1771": 3, "9735": 1})

    def test_corrupt_data_does_not_crash(self):
        """计数坏了不该让订阅停摆。"""
        with patch("core.db.get_meta", return_value="{不是JSON"):
            self.assertEqual(sched.card_usage(), {})

    def test_increments(self):
        saved = {}
        with patch("core.db.get_meta", return_value='{"1771": 2}'), \
             patch("core.db.set_meta",
                   side_effect=lambda k, v: saved.update({k: v})):
            sched.note_card_used("537872****1771")
        import json
        self.assertEqual(json.loads(list(saved.values())[0])["1771"], 3)

    def test_first_use_starts_at_one(self):
        saved = {}
        with patch("core.db.get_meta", return_value="{}"), \
             patch("core.db.set_meta",
                   side_effect=lambda k, v: saved.update({k: v})):
            sched.note_card_used("537872****9999")
        import json
        self.assertEqual(json.loads(list(saved.values())[0])["9999"], 1)

    def test_blank_mask_ignored(self):
        with patch("core.db.set_meta") as sm:
            sched.note_card_used("")
        sm.assert_not_called()

    def test_picks_least_used(self):
        cards = [self._card("1111"), self._card("2222"), self._card("3333")]
        with patch.object(sched, "card_usage",
                          return_value={"1111": 5, "2222": 5, "3333": 0}):
            got = sched._pick_balanced(cards)
        self.assertTrue(got["cardNoMask"].endswith("3333"))

    def test_random_within_lowest_tier(self):
        """最低档有多张时要随机，不能固定轮转（固定顺序易被识别）。"""
        cards = [self._card("1111"), self._card("2222"), self._card("3333")]
        with patch.object(sched, "card_usage",
                          return_value={"1111": 0, "2222": 0, "3333": 9}):
            seen = {sched._pick_balanced(cards)["cardNoMask"] for _ in range(40)}
        self.assertEqual(len(seen), 2, "应在两张最少用的之间随机")
        self.assertNotIn("537872****3333", seen, "用得最多的不该被选中")

    def test_unused_card_wins(self):
        """从没用过的卡（计数里没有）算 0 次，优先选。"""
        cards = [self._card("1111"), self._card("7777")]
        with patch.object(sched, "card_usage", return_value={"1111": 3}):
            got = sched._pick_balanced(cards)
        self.assertTrue(got["cardNoMask"].endswith("7777"))

    def test_all_equal_picks_any(self):
        cards = [self._card("1111"), self._card("2222")]
        with patch.object(sched, "card_usage", return_value={}):
            got = sched._pick_balanced(cards)
        self.assertIn(got["cardNoMask"][-4:], ("1111", "2222"))

    def test_used_in_selection(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("_pick_balanced(usable)", src)

    def test_counted_only_after_session_starts(self):
        """前面任何一步失败都不该占配额，否则会把好卡挤到高档去。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        i = src.index("note_card_used")
        seg = src[max(0, i - 400):i]
        self.assertIn("start_session(", seg)

    def test_status_exposes_usage(self):
        import inspect
        src = inspect.getsource(sched.status)
        self.assertIn("card_usage", src)
        self.assertIn("card_blacklist", src)


class CardBlacklistTest(unittest.TestCase):
    """黑名单里的卡不参与随机选卡。"""

    def test_parses_tails(self):
        with patch.object(sched, "_cfg", lambda n, d: "5931"):
            self.assertEqual(sched._card_blacklist(), {"5931"})

    def test_multiple(self):
        with patch.object(sched, "_cfg", lambda n, d: "5931, 1771"):
            self.assertEqual(sched._card_blacklist(), {"5931", "1771"})

    def test_accepts_full_card_number(self):
        """填完整卡号也能用，取后四位匹配。"""
        with patch.object(sched, "_cfg", lambda n, d: "4746130000005931"):
            self.assertEqual(sched._card_blacklist(), {"5931"})

    def test_blank_means_none(self):
        with patch.object(sched, "_cfg", lambda n, d: ""):
            self.assertEqual(sched._card_blacklist(), set())

    def test_default_blocks_5931(self):
        """5931 是用户点名要拉黑的卡。"""
        from config import auto_subscribe as cfg
        self.assertIn("5931", cfg.AUTO_SUBSCRIBE_CARD_BLACKLIST)

    def test_default_blocks_8361(self):
        """8361 被 Stripe 拒付过（余额充足、出口在 EG 仍拒），拉黑。"""
        from config import auto_subscribe as cfg
        self.assertIn("8361", cfg.AUTO_SUBSCRIBE_CARD_BLACKLIST)

    def test_blacklist_covers_both_cards(self):
        from config import auto_subscribe as cfg
        tails = sched._card_blacklist()
        self.assertEqual(tails, {"5931", "8361"})

    def test_blacklist_applied_in_selection(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("_card_blacklist()", src)
        self.assertIn("blacklist", src)

    def test_does_not_filter_by_balance(self):
        """不按余额筛卡：不够会自动充值（和手动订阅一样）。

        按余额筛会让刚用过的卡长期选不中，失去分散用量的意义。
        """
        import inspect
        src = inspect.getsource(sched.start_subscription)
        i = src.index("usable.append")
        seg = src[max(0, i - 700):i]
        self.assertNotIn("bal >= need", seg,
                         "选卡阶段不该按余额过滤，充值逻辑会兜底")


class AmountGateTest(unittest.TestCase):
    """支付金额上限：超过就不付。"""

    def test_default_is_920_egp(self):
        """920 = 1150 × 0.8，八折价；高于它说明优惠码没生效。"""
        from config import auto_subscribe as cfg
        self.assertEqual(cfg.AUTO_SUBSCRIBE_MAX_AMOUNT, 920.0)

    def test_country_is_eg(self):
        """当前在用 EG/EGP，不是 US。"""
        from config import auto_subscribe as cfg
        self.assertEqual(cfg.AUTO_SUBSCRIBE_COUNTRY, "EG")

    def test_gate_wired_into_session(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("VCC_MAX_CHARGE", src)
        self.assertIn("AUTO_SUBSCRIBE_MAX_AMOUNT", src)

    def test_currency_pinned_with_cap(self):
        """只设上限不设币种会出事：920 USD 和 920 EGP 差 50 倍。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("VCC_EXPECTED_CURRENCY", src)

    def test_cap_matches_funding(self):
        """EGP 上限折算后要落在备款额以内，否则备的钱不够付。"""
        from config import auto_subscribe as a
        from config import vcc as v
        usd = a.AUTO_SUBSCRIBE_MAX_AMOUNT / float(v.VCC_USD_RATE or 56.0)
        self.assertLessEqual(usd, v.VCC_FUNDING_USD,
                             "920 EGP 折算后超过备款额，卡里钱不够")

    def test_gate_applied_to_clone_not_global(self):
        """闸门只作用于本次会话，不能改全局配置。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        i = src.index("VCC_MAX_CHARGE")
        seg = src[max(0, i - 300):i]
        self.assertIn("_with_card", seg)


class AutoConfirmTest(unittest.TestCase):
    """闸门过了就自动提交，不停在等待确认。

    2026-11-18 实测：首次跑通后停在 awaiting_confirm，10 分钟后会话被
    回收，整单白跑。自动流程没人盯着，再让人点一次也没增加任何判断。
    """

    def test_enabled_when_cap_set(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("VCC_AUTO_CONFIRM = True", src)

    def test_disabled_without_cap(self):
        """没设金额上限就不敢自动付——那等于没有任何校验。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("VCC_AUTO_CONFIRM = False", src)

    def test_global_default_is_off(self):
        """手动订阅不受影响，默认仍要人确认。"""
        from config import vcc
        self.assertFalse(vcc.VCC_AUTO_CONFIRM)

    def test_never_submits_when_gate_failed(self):
        import inspect
        from core import vcc_pay_session as ps
        src = inspect.getsource(ps._wait_and_submit)
        i = src.index("VCC_AUTO_CONFIRM")
        seg = src[i:i + 600]
        self.assertIn("sess.gate_error", seg)
        self.assertIn("不自动提交", seg)

    def test_auto_confirm_marks_command(self):
        """日志要能区分自动提交和人工提交，排查时不至于混。"""
        import inspect
        from core import vcc_pay_session as ps
        src = inspect.getsource(ps._wait_and_submit)
        self.assertIn('"auto": True', src)
        self.assertIn('cmd.get("auto")', src)

    def test_second_check_still_runs(self):
        """自动提交也要走"金额有没有变"的二次校验。"""
        import inspect
        from core import vcc_pay_session as ps
        src = inspect.getsource(ps._wait_and_submit)
        self.assertIn("页面金额已变化", src)


class SafetyTest(unittest.TestCase):
    def test_disabled_by_default(self):
        """会真实扣款，不能因为部署新版本就自动开始花钱。

        断言的是**代码默认值**，不是运行时值——线上 .env 可以开启它。
        直接读源码，绕开 .env 覆盖（和 ENABLE_2FA 同样的处理）。
        """
        src = (ROOT / "config" / "auto_subscribe.py").read_text(encoding="utf-8")
        i = src.index("AUTO_SUBSCRIBE_ENABLED = ")
        line = src[i:src.index("\n", i)]
        self.assertIn("False", line, "代码默认值必须是 False")

    def test_manual_trigger_requires_confirm(self):
        src = (ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        i = src.index("def api_auto_subscribe_run_now")
        seg = src[i:i + 900]
        self.assertIn("confirm", seg)
        self.assertIn("扣款", seg)

    def test_rejects_multi_seat_link(self):
        """生成长链后若发现不是 1 席，要放弃而不是照付。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("seats", src)
        self.assertIn("不符合单席位条件", src)

    def test_checks_funds_before_paying(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("ensure_funds", src)

    def test_exit_country_matches_settlement(self):
        """出口国家必须和结算货币同区，否则 Stripe 拒付。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("_verified_proxies_in_country", src)

    def test_does_not_mutate_global_cfg(self):
        """钉卡号时不能改全局配置，会影响别的流程。"""
        from config import vcc
        before = getattr(vcc, "VCC_CARD_ID", "")
        clone = sched._with_card(vcc, "CARD-XYZ")
        self.assertEqual(clone.VCC_CARD_ID, "CARD-XYZ")
        self.assertEqual(getattr(vcc, "VCC_CARD_ID", ""), before)


class CardAddressTest(unittest.TestCase):
    """按卡号取账单地址。"""

    def test_maps_api_fields(self):
        from core.card_address import fetch_card_address
        body = (b'{"full_name":"Clay Foy","street":"1201 Meadow Drive",'
                b'"city":"Livingston","state":"MT","state_name":"Montana",'
                b'"zip_code":"59047","country":"US"}')
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            got = fetch_card_address("4746130000000363")
        self.assertEqual(got["name"], "Clay Foy")
        self.assertEqual(got["line1"], "1201 Meadow Drive")
        self.assertEqual(got["city"], "Livingston")
        self.assertEqual(got["postal"], "59047")
        self.assertEqual(got["country"], "US")

    def test_uses_two_letter_state(self):
        """Stripe 州下拉按两位码匹配，填全称会选不中。"""
        from core.card_address import fetch_card_address
        body = (b'{"full_name":"A","street":"S","city":"C","state":"MT",'
                b'"state_name":"Montana","zip_code":"1","country":"US"}')
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            got = fetch_card_address("4746130000000363")
        self.assertEqual(got["state"], "MT")

    def test_rejects_bad_card_number(self):
        from core.card_address import CardAddressError, fetch_card_address
        for bad in ("", "123", "abcd"):
            with self.assertRaises(CardAddressError):
                fetch_card_address(bad)

    def test_rejects_incomplete_address(self):
        """字段不全就报错，不能拿半截地址去填表。"""
        from core.card_address import CardAddressError, fetch_card_address
        resp = MagicMock()
        resp.read.return_value = b'{"full_name":"A","country":"US"}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(CardAddressError):
                fetch_card_address("4746130000000363")

    def test_wired_into_payment(self):
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        self.assertIn("fetch_card_address", src)
        self.assertIn("VCC_FETCH_CARD_ADDRESS", src)

    def test_only_for_vcc_cards(self):
        """自有卡池的卡用卡池里填的信息，不走这个接口。"""
        src = (ROOT / "core" / "vcc_pay_session.py").read_text(encoding="utf-8")
        i = src.index("fetch_card_address")
        seg = src[max(0, i - 900):i]
        self.assertIn("is_custom_id", seg)


class LoginProxyRotationTest(unittest.TestCase):
    """登录失败换下一个出口 IP，而不是同一个出口反复撞。

    2026-11-19 16:00 事故：EG 出口被风控，4 轮登录全用同一个代理，
    只换设备指纹没用，403 全灭、整点静默烧掉。
    """

    def _patch_login_chain(self, preflight):
        return [
            patch("core.db.get_account_by_email", return_value={}),
            patch("core.account_liveness._network_preflight_with_retry",
                  side_effect=preflight),
            patch("core.account_liveness._login_via_password_or_otp"),
            patch("core.openai_auth.follow_authorize"),
            patch("core.session.close_browser_session"),
        ]

    def test_rotates_to_next_proxy(self):
        """第一个出口 403 要换第二个，第二个成功就返回。"""
        from core import vcc_pay_session as ps
        calls = []

        def preflight(email, proxy, fingerprint_state=None):
            calls.append(proxy)
            if proxy.endswith("7004"):
                raise RuntimeError("HTTP Error 403")
            sess = MagicMock()
            sess.session.cookies.get_dict.return_value = {
                "__Secure-next-auth.session-token": "tok"}
            return sess, "https://auth.openai.com/authorize"

        patches = self._patch_login_chain(preflight)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cookies = ps._login_cookies_for(
            "a@e.test", ["http://1.1.1.1:7004", "http://2.2.2.2:7005"],
            gap=0)
        self.assertEqual(calls, ["http://1.1.1.1:7004", "http://2.2.2.2:7005"])
        self.assertTrue(any("session-token" in c["name"] for c in cookies))

    def test_refetch_when_pool_exhausted(self):
        """池子耗尽要重拉，重拉到的新出口能接上。"""
        from core import vcc_pay_session as ps
        calls = []

        def preflight(email, proxy, fingerprint_state=None):
            calls.append(proxy)
            if proxy.endswith("7004"):
                raise RuntimeError("HTTP Error 403")
            sess = MagicMock()
            sess.session.cookies.get_dict.return_value = {
                "__Secure-next-auth.session-token": "tok"}
            return sess, "https://auth.openai.com/authorize"

        patches = self._patch_login_chain(preflight)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        refetch = MagicMock(return_value=["http://3.3.3.3:7006"])
        cookies = ps._login_cookies_for(
            "a@e.test", ["http://1.1.1.1:7004"], gap=0, refetch=refetch)
        refetch.assert_called_once_with()
        self.assertEqual(calls, ["http://1.1.1.1:7004", "http://3.3.3.3:7006"])
        self.assertTrue(cookies)

    def test_single_string_proxy_still_works(self):
        """旧调用只传一个字符串，也该按单出口池子跑通。"""
        from core import vcc_pay_session as ps

        def preflight(email, proxy, fingerprint_state=None):
            sess = MagicMock()
            sess.session.cookies.get_dict.return_value = {
                "__Secure-next-auth.session-token": "tok"}
            return sess, "https://auth.openai.com/authorize"

        patches = self._patch_login_chain(preflight)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cookies = ps._login_cookies_for("a@e.test", "http://1.1.1.1:7004",
                                        gap=0, rounds=1)
        self.assertTrue(cookies)

    def test_no_refetch_no_infinite_loop(self):
        """没有 refetch 回调时，池子空了就停，不能空转。"""
        from core import vcc_pay_session as ps

        def preflight(email, proxy, fingerprint_state=None):
            raise RuntimeError("HTTP Error 403")

        patches = self._patch_login_chain(preflight)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        got = ps._login_cookies_for("a@e.test", ["http://1.1.1.1:7004"],
                                    gap=0, rounds=8)
        self.assertEqual(got, [])


class RetryIntervalTest(unittest.TestCase):
    """支付失败不占整点后，必须有最小重试间隔，否则每分钟空转一次。"""

    def _cfg(self, over=None):
        d = {"AUTO_SUBSCRIBE_ENABLED": True, "AUTO_SUBSCRIBE_HOURS": "10,12",
             "AUTO_SUBSCRIBE_RETRY_INTERVAL_MIN": 15}
        d.update(over or {})
        return lambda n, dd: d.get(n, dd)

    def _meta(self, attempt_age_sec):
        import time as _time
        ts = str(_time.time() - attempt_age_sec) if attempt_age_sec is not None else ""
        return lambda k: ts if k == "auto_sub_last_attempt" else ""

    def test_recent_attempt_skipped(self):
        import time as _time
        now = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", side_effect=self._meta(60)), \
             patch.object(sched, "find_candidates") as fc:
            r = sched.run_once()
        self.assertFalse(r["acted"])
        self.assertIn("间隔", r["reason"])
        fc.assert_not_called()

    def test_stale_attempt_proceeds(self):
        """过了间隔就该正常发起，不能把重试也挡死。"""
        import time as _time
        now = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=_TZ)
        start = MagicMock(return_value={"ok": True})
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", side_effect=self._meta(16 * 60)), \
             patch("core.db.set_meta"), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates",
                          return_value=[{"id": 1, "email": "a@e.test"}]), \
             patch.object(sched, "start_subscription", start):
            r = sched.run_once()
        self.assertTrue(r["acted"])
        start.assert_called_once_with(1)

    def test_force_ignores_interval(self):
        """手动触发不受间隔限制。"""
        now = datetime.datetime(2026, 11, 19, 3, 0, tzinfo=_TZ)
        with patch.object(sched, "_cfg", self._cfg()), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", side_effect=self._meta(10)), \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=[]):
            r = sched.run_once(force=True)
        self.assertNotIn("间隔", r["reason"])

    def test_attempt_timestamp_written(self):
        """发起前要落尝试时间戳，闸门才有依据。"""
        import inspect
        src = inspect.getsource(sched.run_once)
        self.assertIn("set_meta(_ATTEMPT_KEY", src)


class CursorRotationTest(unittest.TestCase):
    """轮转游标：记住上次尝试位置，成功/失败都推进，到尾回绕。

    需求（2026-11-19）：环境失败后 15 分钟重试要换下一个号，不能死磕
    队首；下个整点从"上次成功 +1"开始；已经是最后一个单席位号时回到
    第一个未订阅的单席位号。+1 只在单席位候选列表内算（列表本身已
    过滤掉 2 席位账号），不按全量账号表偏移。
    """

    def _cands(self, *ids):
        return [{"id": i, "email": "a%d@e.test" % i} for i in ids]

    def _meta(self, cursor_id):
        def getter(key):
            if key == sched._CURSOR_KEY:
                return str(cursor_id)
            return ""
        return getter

    def _run(self, *, cursor, cands, start_side_effect):
        import time as _time
        now = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=_TZ)
        start = MagicMock(side_effect=start_side_effect)
        with patch.object(sched, "_cfg",
                          lambda n, dd: {"AUTO_SUBSCRIBE_ENABLED": True,
                                         "AUTO_SUBSCRIBE_HOURS": "10,12"}.get(n, dd)), \
             patch.object(sched, "_now_sgt", return_value=now), \
             patch("core.db.get_meta", side_effect=self._meta(cursor)), \
             patch("core.db.set_meta") as set_meta, \
             patch.object(sched, "_busy", return_value=""), \
             patch.object(sched, "find_candidates", return_value=cands), \
             patch.object(sched, "start_subscription", start), \
             patch.object(sched, "_mark_account_dead"):
            r = sched.run_once()
        return r, start, set_meta

    def test_cursor_picks_next_candidate(self):
        """游标在 104：跳过 104，从 106 开始。"""
        r, start, _ = self._run(
            cursor=104, cands=self._cands(104, 106, 108),
            start_side_effect=lambda acc_id: {"ok": True})
        start.assert_called_once_with(106)
        self.assertTrue(r["ok"])

    def test_cursor_wraps_to_first(self):
        """游标已是最后一个单席位号：回到第一个未订阅的继续。"""
        r, start, _ = self._run(
            cursor=155, cands=self._cands(129, 149, 155),
            start_side_effect=lambda acc_id: {"ok": True})
        start.assert_called_once_with(129)
        self.assertTrue(r["ok"])

    def test_no_cursor_starts_at_first(self):
        """没有游标（首次部署）：从第一个候选开始。"""
        r, start, _ = self._run(
            cursor=0, cands=self._cands(129, 149),
            start_side_effect=lambda acc_id: {"ok": True})
        start.assert_called_once_with(129)

    def test_success_sets_cursor(self):
        """成功后游标推进到该账号，下个整点从它 +1 开始。"""
        r, start, set_meta = self._run(
            cursor=0, cands=self._cands(104, 106),
            start_side_effect=lambda acc_id: {"ok": True} if acc_id == 106
            else {"ok": False, "error": "account_deactivated"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], 106)
        cursor_writes = [c.args for c in set_meta.call_args_list
                         if c.args and c.args[0] == sched._CURSOR_KEY]
        self.assertEqual(cursor_writes, [(sched._CURSOR_KEY, "106")])

    def test_failure_advances_cursor(self):
        """环境失败也推游标：15 分钟后的重试从下一个号起，不再死磕队首。"""
        r, start, set_meta = self._run(
            cursor=0, cands=self._cands(104, 106, 108),
            start_side_effect=lambda acc_id: {
                "ok": False, "error": "支付会话失败：登录不成"})
        self.assertFalse(r["ok"])
        self.assertEqual([c.args[0] for c in start.call_args_list],
                         [104, 106, 108])
        cursor_writes = [c.args for c in set_meta.call_args_list
                         if c.args and c.args[0] == sched._CURSOR_KEY]
        self.assertEqual(cursor_writes, [(sched._CURSOR_KEY, "104"),
                                         (sched._CURSOR_KEY, "106"),
                                         (sched._CURSOR_KEY, "108")])

    def test_three_env_failures_give_up_round(self):
        """连续 3 个环境失败：放弃本轮，不占整点。"""
        r, start, set_meta = self._run(
            cursor=0, cands=self._cands(1, 2, 3, 4),
            start_side_effect=lambda acc_id: {
                "ok": False, "error": "出口被风控"})
        self.assertEqual(start.call_count, 3, "连败 3 个就该停，不烧第 4 个")
        self.assertFalse(r["ok"])
        self.assertTrue(r["acted"])
        slot_writes = [c for c in set_meta.call_args_list
                       if c.args and c.args[0] == sched._DONE_SLOT_KEY]
        self.assertEqual(slot_writes, [], "环境失败不占整点")

    def test_two_env_failures_exhaust_small_pool(self):
        """候选不足 3 个且全环境失败：轮完就停，同样不占整点。"""
        r, start, set_meta = self._run(
            cursor=0, cands=self._cands(1, 2),
            start_side_effect=lambda acc_id: {
                "ok": False, "error": "出口被风控"})
        self.assertEqual(start.call_count, 2)
        self.assertFalse(r["ok"])
        self.assertTrue(r["acted"])
        slot_writes = [c for c in set_meta.call_args_list
                       if c.args and c.args[0] == sched._DONE_SLOT_KEY]
        self.assertEqual(slot_writes, [], "环境失败不占整点")

    def test_dead_failures_dont_advance_cursor(self):
        """死号标废即从候选消失，不推游标也不占 3 次环境失败额度。"""
        def side(acc_id):
            if acc_id == 104:
                return {"ok": False, "error": "token_revoked"}
            return {"ok": True}

        r, start, set_meta = self._run(
            cursor=0, cands=self._cands(104, 106),
            start_side_effect=side)
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], 106)
        cursor_writes = [c.args for c in set_meta.call_args_list
                         if c.args and c.args[0] == sched._CURSOR_KEY]
        self.assertEqual(cursor_writes, [(sched._CURSOR_KEY, "106")],
                         "游标只记成功位，不记死号")


class PayResultFeedbackTest(unittest.TestCase):
    """支付结果必须回流调度器：同步等终态，失败换来的信息要能用上。"""

    def test_session_receives_proxy_pool(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("extra_proxies=", src)
        self.assertIn("refetch_proxies=", src)

    def test_waits_for_terminal_state(self):
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("ST_FAILED", src)
        self.assertIn("sess.state not in terminal", src)

    def test_failure_maps_to_not_ok(self):
        """会话终态 failed -> start_subscription 返回 ok=False，
        这样 run_once 的"非死号错误，本轮停止"分支才真正可达。"""
        import inspect
        src = inspect.getsource(sched.start_subscription)
        self.assertIn("sess.state == ST_DONE", src)
        self.assertIn('"error": "支付会话失败', src)

    def test_all_verified_proxies_kept(self):
        """出口验证要把全部匹配的都留下，不能只取第一个。"""
        import inspect
        src = inspect.getsource(sched._verified_proxies_in_country)
        self.assertIn("out.append", src)
        start_src = inspect.getsource(sched.start_subscription)
        self.assertIn("extra_proxies=pay_proxies[1:]", start_src)


if __name__ == "__main__":
    unittest.main()
