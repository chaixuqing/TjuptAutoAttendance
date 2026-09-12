# -*- encoding: utf-8 -*-
import json
import os
import pickle
import random
import re
import sys
import time
from argparse import ArgumentParser
from configparser import ConfigParser
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

# Every config key can be provided through the environment, so credentials never have to
# be passed as command line arguments (where they land in the process list and, on CI,
# in the echoed "run" script).
ENV_KEYS = {
  "username": "TJUPT_USERNAME",
  "password": "TJUPT_PASSWORD",
  "base_url": "TJUPT_BASE_URL",
  "cookies_path": "TJUPT_COOKIES_PATH",
  "douban_path": "TJUPT_DOUBAN_PATH",
}

RETRY_BACKOFF_SECONDS = 5.0
REQUEST_TIMEOUT = 30

# "2026-09-12 00:00:00&12345" is the value TJUPT expects back for one captcha option.
ANSWER_VALUE_RE = re.compile(r"^(\d+-\d+-\d+ \d+:\d+:\d+)&(\d+)$")


def normalize_title(title: str) -> str:
  """Clean up a captcha option label so it round-trips into the Douban cache key.

  Entities are resolved and angle brackets dropped. Without this, a label that the
  parser had to escape (``让子弹飞&lt;``) is looked up under a different key than the
  one cached by the previous run, so the cache never hits and the Douban request is
  made again on every attempt.
  """
  title = unescape(re.sub(r"<[^>]*>", " ", title or ""))
  title = re.sub(r"[<>]", " ", title)
  return re.sub(r"\s+", " ", title).strip()


def option_label(node) -> str:
  """Readable title of a captcha option: its <label> wrapper, else the next text node."""
  label = node.find_parent("label")
  if label is not None:
    text = label.get_text(" ", strip=True)
    value = node.get("value") or ""
    if value and value in text:
      text = text.replace(value, " ")
    if text.strip():
      return text

  for element in node.next_elements:
    if isinstance(element, str):
      text = element.strip()
      if text:
        return text
    elif getattr(element, "name", None) in ("input", "button", "table"):
      break
  return ""


def parse_captcha_options(markup: str) -> list:
  """Return ``[(value, option_id, title), ...]`` for the captcha radio buttons.

  The DOM is walked first so a label that is not glued to the ``<input>`` still parses;
  the historical regex over the raw markup is kept as a fallback.
  """
  tree = BeautifulSoup(markup, "html.parser")
  options = []
  for node in tree.select('.captcha input[name="answer"]'):
    if (node.get("type") or "").strip().lower() != "radio":
      continue
    value = unescape(node.get("value") or "").strip()
    match = ANSWER_VALUE_RE.match(value)
    if match:
      options.append((match.group(1) + "&" + match.group(2), match.group(2), option_label(node)))

  if options:
    return options

  return [
    (value, option_id, title)
    for value, option_id, title in re.findall(
      r'<input name="answer" type="radio" value="(\d+-\d+-\d+ \d+:\d+:\d+&amp;(\d+))"/>([^<>]*?)<', markup
    )
  ]


class Bot:
  MAX_TRIES = 5
  # Sentences TJUPT shows when the credentials themselves are wrong. Retrying those
  # only burns attempts against a site that may lock the account or ban the IP.
  AUTH_ERROR_MARKERS = ("密码错误", "密码不正确", "用户名或密码", "用户不存在", "禁止登录", "账号已被")

  def __init__(self, username, password, base_url, cookies_path, douban_path, *args, **kw_args):
    self.username = username
    self.password = password
    self.base_url = base_url
    self.cookies_path = cookies_path
    self.douban_path = douban_path
    # Short human readable explanation of the outcome (log, and the Actions summary).
    self.status = "not attempted"

    self.session = requests.Session()
    self.session.headers.update({
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36"
    })
    self.session.cookies = self.load_cookies()

    # Douban lookups get their own session so the site's cookies are never sent to a
    # third party.
    self.douban_session = requests.Session()
    self.douban_session.headers.update({
      "User-Agent": self.session.headers.get("User-Agent"),
      "Referer": "https://movie.douban.com/",
    })

    self.douban_data = self.load_douban_data()
  
  def log(self, *args, **kw) -> None:
    return print("[%s]" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, **kw)

  def sleep(self, seconds: float) -> None:
    """Single seam for the randomized back-off, so tests do not have to wait."""
    time.sleep(seconds)

  @staticmethod
  def page_text(response) -> str:
    """Response body, decoded with the charset the page actually uses.

    TJUPT serves GBK. When the HTTP headers stay silent about it, requests defaults to
    ISO-8859-1, which mangles "今日已签到" / "签到成功" and makes every marker check
    fail for the wrong reason - so honour the in-document declaration first, then let
    the encoding be guessed, before giving up on UTF-8.
    """
    content_type = response.headers.get("Content-Type", "").lower()
    if "charset=" in content_type:
      return response.text

    head = response.content[:2048].decode("ascii", errors="ignore")
    declared = re.search(r"charset=[\"']?([-\w]+)", head, re.IGNORECASE)
    encoding = declared.group(1) if declared else None
    if not encoding:
      encoding = response.apparent_encoding
    try:
      return response.content.decode(encoding or "utf-8", errors="replace")
    except LookupError:
      return response.content.decode("utf-8", errors="replace")
  
  def load_cookies(self) -> requests.cookies.RequestsCookieJar:
    if os.path.exists(self.cookies_path):
      try:
        with open(self.cookies_path, "rb") as file:
          cookies = pickle.load(file)
        if cookies:
          self.log(f"Cookies loaded from file: {self.cookies_path}")
          return cookies
      except Exception as e:
        self.log(f"Reading cookies error: {e}")
    else:
      self.log(f"Cookies file not exists: {self.cookies_path}")
    return requests.cookies.RequestsCookieJar()

  def save_cookies(self) -> None:
    try:
      os.makedirs(os.path.dirname(self.cookies_path), 0o755, True)
      with open(self.cookies_path, "wb") as f:
        pickle.dump(self.session.cookies, f)
      self.log(f"Cookies wrote to file: {self.cookies_path}")
    except Exception as e:
      self.log(f"Writing cookies error: {e}")
  
  def login(self):
    """Authenticate. Returns ``(ok, fatal)``: a fatal rejection is not worth retrying."""
    for attempt in range(self.MAX_TRIES, 0, -1):
      try:
        _ = self.session.get(f"{self.base_url}login.php", timeout=REQUEST_TIMEOUT)
        response = self.session.post(f"{self.base_url}takelogin.php", {
          "username": self.username,
          "password": self.password,
        }, timeout=REQUEST_TIMEOUT)
        text = self.page_text(response)
      except requests.RequestException as e:
        self.log(f"Log in request failed: {e}")
        if attempt > 1:
          self.sleep(random.random() * RETRY_BACKOFF_SECONDS)
        continue

      if "logout.php" in text:
        self.log("Logged in successfully")
        self.save_cookies()
        return True, False

      if any(marker in text for marker in self.AUTH_ERROR_MARKERS):
        self.log("Log in rejected by TJUPT (wrong username or password). Not retrying.")
        return False, True

      self.log(f"Log in error, unexpected response ({attempt - 1} left)")
      if attempt > 1:
        self.sleep(random.random() * RETRY_BACKOFF_SECONDS)

    self.log(f"Log in error after {self.MAX_TRIES} tries")
    return False, False
  
  def load_douban_data(self) -> dict:
    if os.path.exists(self.douban_path):
      try:
        with open(self.douban_path, encoding="utf-8") as file:
          douban_data = json.load(file)
        self.log(f"Douban data loaded from file: {self.douban_path}")
        return douban_data
      except Exception as e:
        self.log(f"Reading douban data error: {e}")
    else:
      self.log(f"Douban data file not exists: {self.douban_path}")
    return {}
  
  def save_douban_data(self) -> bool:
    try:
      os.makedirs(os.path.dirname(self.douban_path), 0o755, True)
      with open(self.douban_path, "w", encoding="utf-8") as f:
        json.dump(self.douban_data, f, ensure_ascii=False, indent=1)
        self.log(f"Douban data wrote to file: {self.douban_path}")
      return True
    except Exception as e:
      self.log(f"Writing douban data error: {e}")
      return False

  def extract_subject_id(self, url_or_src: str) -> str:
    """Pull the Douban subject id ("p123456") out of a poster URL."""
    match = re.search(r"/(p\d+)(?:\.|/|$)", url_or_src or "")
    return match.group(1) if match else None

  def get_id(self, title: str) -> str:
    title = normalize_title(title)
    if not title:
      return None
    if title in self.douban_data:
      return self.douban_data[title]

    self.sleep(random.random() * RETRY_BACKOFF_SECONDS)
    try:
      response = self.douban_session.get(
        "https://movie.douban.com/j/subject_suggest",
        params={"q": title},
        timeout=REQUEST_TIMEOUT,
      )
      response.raise_for_status()
      results = response.json()
    except (requests.RequestException, ValueError) as e:
      self.log(f"Douban lookup failed for \"{title}\": {e}")
      return None

    if not results:
      self.log(f"Douban returned no result for \"{title}\"")
      return None

    url_id = self.extract_subject_id(results[0].get("img", ""))
    if not url_id:
      self.log(f"Douban returned no usable id for \"{title}\": {response.text[:200]}")
      return None

    self.douban_data[title] = url_id
    self.log(f"Title \"{title}\" returned id \"{url_id}\"")
    return url_id
  
  def auto_attendance(self) -> bool:
    """Try to sign in. True only when the site confirms the sign-in - callers must
    propagate this into the process exit code, otherwise CI goes green on a failure."""
    detail = self.status
    for attempt in range(1, self.MAX_TRIES + 1):
      ok, retryable, detail = self.auto_attendance_once()
      self.status = detail
      if ok:
        self.log("Attended successfully")
        return True
      if not retryable:
        self.log(f"Attend error, not retrying: {detail}")
        return False
      if attempt < self.MAX_TRIES:
        self.sleep(random.random() * RETRY_BACKOFF_SECONDS)
        self.log(f"Attend error, try again ({self.MAX_TRIES - attempt} left)")

    self.log(f"Attend error after {self.MAX_TRIES} tries")
    self.status = f"{detail} (gave up after {self.MAX_TRIES} tries)"
    return False

  def auto_attendance_once(self):
    """One attempt. Returns ``(ok, retryable, detail)``."""
    try:
      response = self.session.get(f"{self.base_url}attendance.php", timeout=REQUEST_TIMEOUT)
      if "login.php" in response.url:
        self.log("Needed to log in")
        logged_in, fatal = self.login()
        if not logged_in:
          return False, not fatal, ("login rejected by TJUPT (check the USERNAME / PASSWORD secrets)"
                                    if fatal else "login failed")
        response = self.session.get(f"{self.base_url}attendance.php", timeout=REQUEST_TIMEOUT)

      text = self.page_text(response)
      if "今日已签到" in text:
        self.log("\"今日已签到\" found, already attended")
        return True, False, "already attended today"

      tree = BeautifulSoup(text, "html.parser")

      # Keep the historical selector, but tolerate a re-nested image: the site has
      # moved this markup before and an extra wrapper should not look like a failure.
      captcha_image = tree.select_one(".captcha > tr > td > img") or tree.select_one(".captcha img")
      if captcha_image is None:
        return False, False, ("captcha image not found - TJUPT changed its markup, "
                              "or this account/session is being asked to verify itself")

      captcha_image_id = self.extract_subject_id(captcha_image.attrs.get("src", ""))
      if not captcha_image_id:
        return False, False, f"captcha image id not recognised from src: {captcha_image.attrs.get('src')}"

      captcha_options = parse_captcha_options(str(tree))
      if not captcha_options:
        return False, False, ("captcha options not found - TJUPT changed its markup")

      available_choices = []

      for value, option_id, title in captcha_options:
        title = normalize_title(title)
        if not title:
          self.log(f"Captcha option {option_id} has no readable title, skipping")
          continue
        url_id = self.get_id(title)
        if captcha_image_id == url_id:
          choice = {
            "value": value,
            "id": option_id,
            "title": title,
            "url_id": url_id,
          }
          available_choices.append(choice)
          self.log(f"Available choice found: {json.dumps(choice, ensure_ascii=False)}")

      self.save_douban_data()

      if len(available_choices) == 0:
        return False, True, (f"no choice found for captcha {captcha_image_id} among "
                            f"{len(captcha_options)} options (Douban lookups failed, or the titles differ)")
      elif len(available_choices) > 1:
        return False, True, f"{len(available_choices)} choices found, refusing to guess"

      data = {
        "answer": available_choices[0]["value"],
        "submit": "提交"
      }
      response = self.session.post(f"{self.base_url}attendance.php", data, timeout=REQUEST_TIMEOUT)
      text = self.page_text(response)
      if "签到成功" in text:
        return True, False, "attended"
      if "今日已签到" in text:
        self.log("\"今日已签到\" found after submitting, already attended")
        return True, False, "already attended today"
      self.log(f"\"签到成功\" not found, response_text: {text[:500]}")
      return False, True, "TJUPT did not confirm the sign-in (\"签到成功\" missing)"
    except requests.RequestException as e:
      self.log(f"Network error: {e}")
      return False, True, f"network error while talking to TJUPT: {e.__class__.__name__}"
    except Exception as e:
      self.log(f"Error: {e}")
      return False, True, f"unexpected error: {e!r}"


def load_config(ini_path: str) -> dict:
  config = {
    "username": None,
    "password": None,
    "base_url": "https://www.tjupt.org/",
    "cookies_path": "data/cookies.pkl",
    "douban_path": "data/douban.json",
  }

  # Precedence: defaults < config.ini < environment < command line.
  if ini_path and os.path.exists(ini_path):
    config_parser = ConfigParser()
    config_parser.read(ini_path, "utf-8")
    for key in config:
      # The shipped template spells its keys with dashes (base-url) while the code uses
      # underscores, so accept both instead of silently falling back to the default.
      value = config_parser.get("Bot", key, fallback=None)
      if value is None:
        value = config_parser.get("Bot", key.replace("_", "-"), fallback=None)
      if value is not None:
        config[key] = str(value)

  for key, env_key in ENV_KEYS.items():
    value = os.environ.get(env_key)
    if value:
      config[key] = value.strip()

  return config


def write_job_summary(bot: "Bot", ok: bool) -> None:
  """Best effort GitHub Actions job summary; a no-op anywhere else."""
  summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
  if not summary_path:
    return
  try:
    with open(summary_path, "a", encoding="utf-8") as f:
      f.write("## TJUPT auto attendance: %s\n\n" % ("**Success**" if ok else "**Failed**"))
      f.write("- Result: %s\n" % bot.status)
      f.write("- Finished: %s UTC\n" % datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
  except Exception:
    pass


def main() -> int:
  argument_parser = ArgumentParser(description="Auto adttendance bot for TJUPT. Credentials can also be supplied through TJUPT_USERNAME / TJUPT_PASSWORD so they never appear on the command line.")
  argument_parser.add_argument("-i", "--ini-path", default="config/config.ini", help="File path for config.ini (default: config/config.ini). Priority: defaults < ini file < TJUPT_* environment variables < command line.")
  argument_parser.add_argument("-u", "--username", help="Your username for TJUPT.")
  argument_parser.add_argument("-p", "--password", help="Your password for TJUPT.")
  argument_parser.add_argument("-b", "--base-url", help="Base url path for TJUPT (default: https://www.tjupt.org/).")
  argument_parser.add_argument("-c", "--cookies-path", help="File path for cookies.pkl (default: data/cookies.pkl).")
  argument_parser.add_argument("-d", "--douban-path", help="File path for douban.json (default: data/douban.json).")
  args = argument_parser.parse_args()

  config = load_config(args.ini_path)

  for key, value in vars(args).items():
    if key in config and value:
      config[key] = value

  if not config["username"] or not config["password"]:
    print("::error::No TJUPT username/password provided. Set the USERNAME and PASSWORD repository secrets (they are read as TJUPT_USERNAME / TJUPT_PASSWORD), pass -u/-p, or fill in config/config.ini.")
    return 2

  for key, value in config.items():
    if isinstance(value, str):
      config[key] = value.strip()
  if not config["base_url"].endswith("/"):
    config["base_url"] += "/"

  bot = Bot(**config)
  ok = bot.auto_attendance()
  print("[%s] Result: %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), bot.status))
  write_job_summary(bot, ok)
  if not ok:
    print("::error::Auto attendance did not succeed: %s" % bot.status)
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
