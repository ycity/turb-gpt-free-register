import unittest
from unittest.mock import MagicMock, patch

from core import account_export
from core.account_export import (_is_cf_challenge_response,
                                 _is_retryable_reauth_error,
                                 _trigger_reauth_with_retry)


def resp(status=403, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    return r


def http_error(response):
    exc = Exception("HTTP Error 403: ")
    exc.response = response
    return exc


class CfChallengeDetectionTests(unittest.TestCase):
    def test_cf_mitigated_header_marks_challenge(self):
        self.assertTrue(_is_cf_challenge_response(
            resp(headers={"cf-mitigated": "challenge", "cf-ray": "a533bfbc-LAX"})))
        self.assertTrue(_is_cf_challenge_response(
            resp(headers={"CF-Mitigated": "CHALLENGE"})))

    def test_html_body_with_cf_ray_marks_challenge(self):
        # 实际失败日志：content_type=text/html cf_ray=a533bfbc295213a3-LAX
        self.assertTrue(_is_cf_challenge_response(
            resp(headers={"content-type": "text/html; charset=utf-8",
                          "cf-ray": "a533bfbc295213a3-LAX"})))

    def test_json_business_rejection_is_not_challenge(self):
        self.assertFalse(_is_cf_challenge_response(
            resp(headers={"content-type": "application/json", "cf-ray": "abc-LAX"})))

    def test_missing_or_bare_response_is_not_challenge(self):
        self.assertFalse(_is_cf_challenge_response(None))
        self.assertFalse(_is_cf_challenge_response(resp(headers={})))
        self.assertFalse(_is_cf_challenge_response(
            resp(headers={"content-type": "text/html"})))


class ReauthRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(account_export.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        circuit = patch.object(account_export, "_clear_twofa_session_circuit")
        circuit.start()
        self.addCleanup(circuit.stop)

    def test_cf_challenge_403_is_retried_and_can_succeed(self):
        challenge = http_error(resp(headers={"cf-mitigated": "challenge",
                                             "cf-ray": "r-LAX"}))
        with patch.object(account_export, "_trigger_reauth",
                          side_effect=[challenge, challenge, "https://auth.ok/url"]) as tr:
            out = _trigger_reauth_with_retry(MagicMock(), "u@example.test")
        self.assertEqual(out, "https://auth.ok/url")
        self.assertEqual(tr.call_count, 3)

    def test_business_403_still_fails_immediately(self):
        rejection = http_error(resp(headers={"content-type": "application/json"}))
        with patch.object(account_export, "_trigger_reauth",
                          side_effect=rejection) as tr:
            with self.assertRaises(Exception):
                _trigger_reauth_with_retry(MagicMock(), "u@example.test")
        # 业务拒绝不重放，只调用一次。
        self.assertEqual(tr.call_count, 1)

    def test_cf_challenge_exhausts_attempts_then_raises(self):
        challenge = http_error(resp(headers={"cf-mitigated": "challenge",
                                             "cf-ray": "r-LAX"}))
        with patch.object(account_export, "_trigger_reauth",
                          side_effect=challenge) as tr:
            with self.assertRaises(Exception):
                _trigger_reauth_with_retry(MagicMock(), "u@example.test")
        self.assertEqual(tr.call_count, 3)

    def test_transport_errors_remain_retryable(self):
        self.assertTrue(_is_retryable_reauth_error(Exception("connection reset")))
        self.assertTrue(_is_retryable_reauth_error(Exception("HTTP Error 429: ")))
        self.assertTrue(_is_retryable_reauth_error(Exception("timed out")))
        self.assertFalse(_is_retryable_reauth_error(Exception("invalid credentials")))


if __name__ == "__main__":
    unittest.main()
