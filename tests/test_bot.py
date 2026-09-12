# -*- encoding: utf-8 -*-
"""Offline regression tests for the TJUPT attendance bot.

Run from the repository root:

    python -m unittest discover -s tests -t . -v

No internet access is needed. The bot is pointed at a throwaway HTTP server that
implements just enough of TJUPT's login/attendance pages, and every Douban lookup is
served from a pre-seeded ``douban.json`` so the captcha solving stays deterministic.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
  sys.path.insert(0, ROOT)

from main import Bot, load_config, normalize_title  # noqa: E402

MOVIE_MATCH = "\u8ba9\u5b50\u5f39\u98de"   # id p11111, this is the captcha image we serve
MOVIE_OTHER = "\u9738\u738b\u522b\u59ec"   # id p22222
ID_MATCH, ID_OTHER = "p11111", "p22222"     # as reported by Douban / the poster file name
NUM_MATCH, NUM_OTHER = "11111", "22222"      # as embedded in the radio button value


def captcha_html():
  options = "".join(
    '<input name="answer" type="radio" value="2026-09-12 00:00:00&amp;%s"/>%s<' % (pid, title)
    for title, pid in [(MOVIE_MATCH, NUM_MATCH), (MOVIE_OTHER, NUM_OTHER)]
  )
  return (
    "<html><body><table class=\"captcha\">"
    "<tr><td><img src=\"https://www.tjupt.org/pics/%s.jpg\" /></td></tr>"
    "<tr><td><form method=\"post\" action=\"attendance.php\"><table>%s</table></form></td></tr>"
    "</table></body></html>" % (ID_MATCH, options)
  )


class MockTjupt(BaseHTTPRequestHandler):
  """Minimal stand-in for www.tjupt.org's login + attendance endpoints."""

  server_version = "MockTJUPT/1.0"
  state = {}

  def log_message(self, *args):
    pass

  def _reply(self, body, code=200, headers=()):
    if body.startswith("<html>") and "charset" not in body:
      # Like TJUPT: the charset is only declared inside the document.
      body = '<html><meta http-equiv="Content-Type" content="text/html; charset=utf-8">' + body[len("<html>"):]
    payload = body.encode("utf-8")
    self.send_response(code)
    for key, value in headers:
      self.send_header(key, value)
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)

  def _signed_in(self):
    state = self.server.state
    return state["signed_in"] or "uid=1" in (self.headers.get("Cookie") or "")

  def do_GET(self):
    state = self.server.state
    if self.path.startswith("/attendance.php"):
      if state.get("bad"):
        return self._reply("<html>\u767b\u5f55\u5df2\u8fc7\u671f</html>")
      state["attendance_gets"] += 1
      if state.get("markup") == "broken":
        return self._reply("<html>the captcha widget moved somewhere else</html>")
      if self._signed_in() and state["attended"]:
        return self._reply("<html>\u4eca\u65e5\u5df2\u7b7e\u5230</html>")
      if self._signed_in():
        return self._reply(captcha_html())
      return self._reply("<html>redirect</html>", 302, [("Location", "/login.php")])
    state["login_gets"] += 1
    return self._reply("<html><form action=\"takelogin.php\"></form></html>")

  def do_POST(self):
    state = self.server.state
    body = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
    if self.path.startswith("/takelogin.php"):
      state["login_posts"] += 1
      if body.get("username") == ["good"] and body.get("password") == ["secret"]:
        state["signed_in"] = True
        if state.get("login_flaky") and state["login_posts"] < 2:
          return self._reply("<html>500-ish</html>", 502)
        return self._reply("<html>logout.php</html>", 200, [("Set-Cookie", "uid=1; Path=/")])
      return self._reply("<html>\u9519\u8bef\u7684\u7528\u6237\u540d\u6216\u5bc6\u7801</html>")
    if self.path.startswith("/attendance.php"):
      state["attendance_posts"] += 1
      if body.get("answer") and body["answer"][0] == "2026-09-12 00:00:00&%s" % NUM_MATCH:
        state["attended"] = True
        return self._reply("<html>\u7b7e\u5230\u6210\u529f</html>")
      return self._reply("<html>\u7b7e\u5230\u5931\u8d25</html>")
    return self._reply("<html>404</html>", 404)


class AttendanceTestCase(unittest.TestCase):
  """Spins up the mock site and drives main.py through real invocations."""

  def setUp(self):
    self.server = HTTPServer(("127.0.0.1", 0), MockTjupt)
    self.server.state = {
      "signed_in": False, "attended": False, "bad": False,
      "login_gets": 0, "login_posts": 0, "attendance_gets": 0, "attendance_posts": 0,
    }
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    self.thread.start()
    self.base_url = "http://127.0.0.1:%d/" % self.server.server_address[1]

    self.workdir = tempfile.mkdtemp(prefix="tjupt-test-")
    self.data_dir = os.path.join(self.workdir, "data")
    os.makedirs(self.data_dir)
    self.douban_path = os.path.join(self.data_dir, "douban.json")
    self.cookies_path = os.path.join(self.data_dir, "cookies.pkl")
    self.seed_douban_cache({MOVIE_MATCH: ID_MATCH, MOVIE_OTHER: ID_OTHER})

  def tearDown(self):
    self.server.shutdown()
    self.server.server_close()
    self.thread.join(timeout=5)
    shutil.rmtree(self.workdir, ignore_errors=True)

  def seed_douban_cache(self, mapping):
    with open(self.douban_path, "w", encoding="utf-8") as f:
      json.dump(mapping, f, ensure_ascii=False)

  def make_bot(self, **overrides):
    config = {
      "username": "good", "password": "secret", "base_url": self.base_url,
      "cookies_path": self.cookies_path, "douban_path": self.douban_path,
    }
    config.update(overrides)
    bot = Bot(**config)
    bot.sleep = lambda seconds: None          # no waiting in tests
    bot.MAX_TRIES = 3
    bot.REQUEST_TIMEOUT = 10
    return bot

  def run_main(self, args=(), env=None):
    """Run main.py in a subprocess (no TJUPT_* leaking in) and return CompletedProcess."""
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("TJUPT_")}
    child_env["PYTHONPATH"] = ROOT
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env.update(env or {})
    driver = "import sys, main; main.Bot.sleep = lambda self, s: None; sys.exit(main.main())"
    return subprocess.run(
      [sys.executable, "-c", driver] + list(args),
      cwd=self.workdir, env=child_env, capture_output=True, text=True, timeout=120,
    )

  # ---- happy paths -------------------------------------------------------------

  def test_successful_run_exits_zero_and_attends(self):
    result = self.run_main(["-u", "good", "-p", "secret", "-b", self.base_url,
                            "-c", self.cookies_path, "-d", self.douban_path])
    self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
    self.assertIn("Attended successfully", result.stdout)
    self.assertEqual(self.server.state["attendance_posts"], 1)

  def test_already_attended_is_a_success_not_a_failure(self):
    self.server.state.update(signed_in=True, attended=True)
    result = self.run_main(["-u", "good", "-p", "secret", "-b", self.base_url,
                            "-c", self.cookies_path, "-d", self.douban_path])
    self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
    self.assertIn("already attended", result.stdout)

  def test_cookies_are_saved_and_reused_on_the_next_run(self):
    args = ["-u", "good", "-p", "secret", "-b", self.base_url,
            "-c", self.cookies_path, "-d", self.douban_path]
    first = self.run_main(args)
    self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
    self.assertTrue(os.path.exists(self.cookies_path), "cookies.pkl should be written")
    self.assertEqual(self.server.state["login_posts"], 1)

    # A saved session means the second run must not authenticate again.
    self.server.state.update(signed_in=False, attended=False)
    second = self.run_main(args)
    self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
    self.assertIn("Cookies loaded from file", second.stdout)
    self.assertEqual(self.server.state["login_posts"], 1, "second run should reuse the cookies")

  def test_credentials_come_from_the_environment(self):
    result = self.run_main(env={"TJUPT_USERNAME": "good", "TJUPT_PASSWORD": "secret",
                                "TJUPT_BASE_URL": self.base_url})
    self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
    self.assertNotIn("-u good", result.stdout)

  def test_no_douban_request_is_sent_for_a_cached_title(self):
    # An empty cache forces a Douban lookup; a warm one must not touch the network.
    calls = []

    class Boom:
      def get(self, *a, **kw):
        calls.append(a)
        raise AssertionError("Douban should not be queried for a cached title")

    bot = self.make_bot()
    bot.douban_session = Boom()
    self.assertEqual(bot.get_id(MOVIE_MATCH), ID_MATCH)
    self.assertEqual(calls, [])

  # ---- failure paths: these must be visible in the exit code ------------------

  def test_unmatched_captcha_fails_the_run(self):
    self.seed_douban_cache({})           # no cached ids and no network -> nothing matches
    result = self.run_main(["-u", "good", "-p", "secret", "-b", self.base_url,
                            "-c", os.path.join(self.data_dir, "none.pkl"),
                            "-d", os.path.join(self.data_dir, "empty.json")])
    self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
    self.assertIn("Result:", result.stdout)

  def test_missing_credentials_exit_two(self):
    result = self.run_main()
    self.assertEqual(result.returncode, 2, msg=result.stdout + result.stderr)
    self.assertIn("::error::", result.stdout)

  def test_rejected_login_is_not_retried_five_times(self):
    result = self.run_main(["-u", "good", "-p", "wrong-password", "-b", self.base_url,
                            "-c", self.cookies_path, "-d", self.douban_path])
    self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
    self.assertIn("Not retrying", result.stdout)
    self.assertLessEqual(self.server.state["login_posts"], 1)

  def test_retryable_failure_keeps_the_reason_and_the_attempt_count(self):
    self.seed_douban_cache({MOVIE_MATCH: "p99999", MOVIE_OTHER: "p88888"})
    bot = self.make_bot()
    self.assertFalse(bot.auto_attendance())
    self.assertIn("no choice found", bot.status)
    self.assertIn("gave up after 3 tries", bot.status)
    # 1 redirect probe + 1 captcha page for the first attempt, then one GET per retry.
    self.assertEqual(self.server.state["attendance_gets"], 4)

  def test_flaky_login_is_retried(self):
    self.server.state["login_flaky"] = True
    bot = self.make_bot()
    self.assertTrue(bot.auto_attendance())
    self.assertEqual(self.server.state["login_posts"], 2)

  def test_site_markup_change_is_reported(self):
    self.server.state["markup"] = "broken"
    bot = self.make_bot()
    self.assertFalse(bot.auto_attendance())
    self.assertIn("markup", bot.status)
    self.assertEqual(self.server.state["attendance_gets"], 1, "a changed markup will not fix itself between retries")

  def test_job_summary_is_written_when_requested(self):
    summary = os.path.join(self.workdir, "summary.md")
    result = self.run_main(
      ["-u", "good", "-p", "nope", "-b", self.base_url, "-c", self.cookies_path, "-d", self.douban_path],
      env={"GITHUB_STEP_SUMMARY": summary, "TJUPT_USERNAME": "", "TJUPT_PASSWORD": ""},
    )
    self.assertEqual(result.returncode, 1)
    with open(summary, encoding="utf-8") as f:
      content = f.read()
    self.assertIn("**Failed**", content)

  # ---- pure helpers -----------------------------------------------------------

  def test_titles_are_normalised_before_the_douban_lookup(self):
    self.assertEqual(normalize_title(" \u8ba9\u5b50\u5f39\u98de&nbsp;"), MOVIE_MATCH)
    self.assertEqual(normalize_title("\u9738\u738b\u522b\u59ec&lt;"), MOVIE_OTHER)
    self.assertEqual(normalize_title("\u8ba9\u5b50\u5f39\u98de <b>x</b>"), "\u8ba9\u5b50\u5f39\u98de x")
    self.assertEqual(normalize_title(None), "")

  def test_subject_id_extraction(self):
    bot = self.make_bot()
    self.assertEqual(bot.extract_subject_id("https://www.tjupt.org/pics/p12345.jpg"), "p12345")
    self.assertEqual(bot.extract_subject_id("/tjupt/pics/p7.jpg"), "p7")
    self.assertEqual(bot.extract_subject_id(None), None)
    self.assertEqual(bot.extract_subject_id("https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2561439800.jpg"), "p2561439800")

  def test_douban_cache_is_keyed_on_the_normalised_title(self):
    self.seed_douban_cache({})
    bot = self.make_bot()
    bot.sleep = lambda seconds: None
    looked_up = {}

    class FakeResponse:
      text = json.dumps([{"img": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p11111.jpg"}])
      def raise_for_status(self): pass
      def json(self):
        return json.loads(self.text)

    def fake_get(url, params=None, timeout=None):
      looked_up[params["q"]] = True
      return FakeResponse()

    bot.douban_session.get = fake_get
    self.assertEqual(bot.get_id(" %s&nbsp;" % MOVIE_MATCH), ID_MATCH)
    self.assertEqual(list(looked_up), [MOVIE_MATCH], "title sent to Douban must be clean")
    self.assertEqual(bot.douban_data[MOVIE_MATCH], ID_MATCH)

  def test_ini_and_environment_precedence(self):
    ini = os.path.join(self.workdir, "config.ini")
    with open(ini, "w", encoding="utf-8") as f:
      f.write("[Bot]\nbase-url = https://example.invalid/\nusername = ini-user\npassword = ini-pass\n")

    saved = dict(os.environ)
    try:
      for key in ("TJUPT_BASE_URL", "TJUPT_USERNAME", "TJUPT_PASSWORD"):
        os.environ.pop(key, None)
      config = load_config(ini)
      self.assertEqual(config["base_url"], "https://example.invalid/", "dashed ini keys must be honoured")
      self.assertEqual(config["username"], "ini-user")

      os.environ["TJUPT_BASE_URL"] = "https://env.invalid/"
      config = load_config(ini)
      self.assertEqual(config["base_url"], "https://env.invalid/", "environment overrides the ini file")
      self.assertEqual(config["password"], "ini-pass", "unset keys keep the ini value")
    finally:
      os.environ.clear()
      os.environ.update(saved)

  def test_module_defaults_still_point_at_tjupt(self):
    saved = dict(os.environ)
    try:
      for key in ("TJUPT_BASE_URL", "TJUPT_USERNAME", "TJUPT_PASSWORD"):
        os.environ.pop(key, None)
      config = load_config(os.path.join(self.workdir, "does-not-exist.ini"))
      self.assertEqual(config["base_url"], "https://www.tjupt.org/")
      self.assertEqual(config["cookies_path"], "data/cookies.pkl")
      self.assertIsNone(config["username"])
    finally:
      os.environ.clear()
      os.environ.update(saved)


if __name__ == "__main__":
  unittest.main(verbosity=2)
