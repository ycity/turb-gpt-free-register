# -*- coding: utf-8 -*-
"""席位探测必须和 2FA 解耦。

线上问题（2026-11-17）：
  关掉自动 2FA 做对照实验后，#103 #104 注册正常、账号也活着，
  却查不出席位。原因是席位探测只挂在 core/twofa_service.py 的
  2FA 成功分支里（_run_twofa 的 try 块末尾），2FA 一关或一失败，
  这条路径就彻底断了。

  但席位探测只用 access_token 直连 API，和 2FA 本来毫无关系。

修法：注册落库后也探一次，两条路径按"是否已有结果"去重。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class DecouplingTest(unittest.TestCase):
    def test_register_path_exists(self):
        """注册落库后要有独立的探测入口，不能只有 2FA 那一条。"""
        from core import account_export
        self.assertTrue(hasattr(account_export, "_maybe_probe_single_seat"))

    def test_called_from_save_account_data(self):
        import inspect
        from core import account_export
        src = inspect.getsource(account_export.save_account_data)
        self.assertIn("_maybe_probe_single_seat", src)

    def test_not_gated_on_2fa_config(self):
        """注册后这条路不能再看 2FA 的开关，否则等于没解耦。"""
        import inspect
        from core import account_export
        src = inspect.getsource(account_export._maybe_probe_single_seat)
        self.assertNotIn("ENABLE_2FA", src)
        self.assertNotIn("TEAM_PROBE_SINGLE_SEAT_AFTER_2FA", src)

    def test_not_gated_on_totp_secret(self):
        """有没有 totp_secret 都该探，这正是之前漏掉的情况。"""
        import inspect
        from core import account_export
        src = inspect.getsource(account_export._maybe_probe_single_seat)
        self.assertNotIn("totp_secret", src)


class BehaviourTest(unittest.TestCase):
    def _call(self, acc, *, token="tok", cfg_on=True, delay=0.0):
        from config import register as reg_cfg
        from core import account_export

        probe = MagicMock(return_value={"ok": True, "supported": True,
                                        "minimum_seats": 1})
        update = MagicMock()
        getacc = MagicMock(return_value=acc)

        def run_now(target=None, name=None, daemon=None):
            t = MagicMock()
            t.start = target        # 直接同步执行，便于断言
            return t

        # 注意：不能用 patch.dict(sys.modules, {"config.register": ...})。
        # `from config import register` 读的是 config 包上已设好的属性，
        # 不查 sys.modules，伪造不生效——那样配置开关测试会假性通过。
        with patch.object(reg_cfg, "PROBE_SINGLE_SEAT_AFTER_REGISTER", cfg_on), \
             patch.object(reg_cfg, "PROBE_SINGLE_SEAT_DELAY", delay), \
             patch("core.single_seat.probe_single_seat", probe), \
             patch("core.db.update_account_single_seat", update), \
             patch("core.db.get_account", getacc), \
             patch("threading.Thread", side_effect=run_now):
            account_export._maybe_probe_single_seat(7, "a@e.test", token)
        return probe, update

    def test_probes_account_without_2fa(self):
        """没有 totp_secret 的账号也要探——这就是线上漏掉的那批。"""
        probe, update = self._call({"id": 7, "access_token": "tok"})
        probe.assert_called_once()
        update.assert_called_once()

    def test_skips_when_already_probed(self):
        """2FA 那条路已经探过了就别重复，省一次请求。"""
        probe, update = self._call(
            {"id": 7, "single_seat_checked_at": "2026-11-17T10:00:00"})
        probe.assert_not_called()

    def test_skips_without_token(self):
        probe, update = self._call({"id": 7}, token="")
        probe.assert_not_called()

    def test_respects_config_switch(self):
        probe, update = self._call({"id": 7}, cfg_on=False)
        probe.assert_not_called()

    def test_probe_failure_does_not_raise(self):
        """注册已经成功了，探测炸了不能把注册搞成失败。"""
        from config import register as reg_cfg
        from core import account_export

        def run_now(target=None, name=None, daemon=None):
            t = MagicMock()
            t.start = target
            return t

        # 延迟必须置 0：不加这句会真的 sleep 180 秒，
        # 单元测试绝不能等真实延迟（这条之前漏了，整个文件跑了 180 秒）。
        with patch.object(reg_cfg, "PROBE_SINGLE_SEAT_AFTER_REGISTER", True), \
             patch.object(reg_cfg, "PROBE_SINGLE_SEAT_DELAY", 0.0), \
             patch("core.db.get_account", side_effect=RuntimeError("db 挂了")), \
             patch("threading.Thread", side_effect=run_now):
            account_export._maybe_probe_single_seat(7, "a@e.test", "tok")

    def test_runs_in_background(self):
        """探测要等几分钟再跑，不能占着注册工作线程。"""
        import inspect
        from core import account_export
        src = inspect.getsource(account_export._maybe_probe_single_seat)
        self.assertIn("threading.Thread", src)
        self.assertIn("daemon=True", src)

    def test_waits_before_probing(self):
        """刚注册完立刻探会被判 unusual activity（#105 实测），要先等。"""
        import inspect
        from core import account_export
        src = inspect.getsource(account_export._maybe_probe_single_seat)
        self.assertIn("PROBE_SINGLE_SEAT_DELAY", src)
        self.assertIn("time.sleep(delay)", src)

    def test_delay_is_applied(self):
        """延迟必须真的生效，不能只读配置不用。"""
        from config import register as reg_cfg
        from core import account_export

        slept = []

        def run_now(target=None, name=None, daemon=None):
            t = MagicMock()
            t.start = target
            return t

        with patch.object(reg_cfg, "PROBE_SINGLE_SEAT_AFTER_REGISTER", True), \
             patch.object(reg_cfg, "PROBE_SINGLE_SEAT_DELAY", 42.0), \
             patch.object(account_export.time, "sleep", slept.append), \
             patch("core.db.get_account", return_value={"id": 7}), \
             patch("core.single_seat.probe_single_seat",
                   return_value={"ok": True}), \
             patch("core.db.update_account_single_seat"), \
             patch("threading.Thread", side_effect=run_now):
            account_export._maybe_probe_single_seat(7, "a@e.test", "tok")
        self.assertIn(42.0, slept, "配置的延迟没有真正生效")


class ConfigTest(unittest.TestCase):
    def test_flag_exists_and_on(self):
        from config import register as cfg
        self.assertTrue(cfg.PROBE_SINGLE_SEAT_AFTER_REGISTER)

    def test_delay_matches_2fa_rhythm(self):
        """延迟要和 2FA 那条路同量级，否则一条能探一条被拒。"""
        from config import register as cfg
        from config import twofa as t2
        self.assertGreater(cfg.PROBE_SINGLE_SEAT_DELAY, 0)
        self.assertGreaterEqual(cfg.PROBE_SINGLE_SEAT_DELAY,
                                t2.TWOFA_REGISTRATION_DELAY * 0.5)

    def test_flag_is_env_overridable(self):
        src = (ROOT / "config" / "register.py").read_text(encoding="utf-8")
        i = src.index("apply_env_overrides")
        self.assertIn("PROBE_SINGLE_SEAT_AFTER_REGISTER", src[i:])
        self.assertIn("PROBE_SINGLE_SEAT_DELAY", src[i:])

    def test_old_2fa_path_kept(self):
        """2FA 那条路用的是刚刷新的新 token，更准，不该删掉。"""
        from config import register as cfg
        self.assertTrue(hasattr(cfg, "TEAM_PROBE_SINGLE_SEAT_AFTER_2FA"))
        import inspect
        from core import twofa_service
        src = inspect.getsource(twofa_service._run_twofa)
        self.assertIn("_probe_single_seat_after_2fa", src)


if __name__ == "__main__":
    unittest.main()
