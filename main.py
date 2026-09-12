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
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


class Bot:
  def __init__(self, username, password, base_url, cookies_path, douban_path, *args, **kw_args):
    self.username = username
    self.password = password
    self.base_url = base_url
    self.cookies_path = cookies_path
    self.douban_path = douban_path

    self.session = requests.Session()
    self.session.headers.update({
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36"
    })
    self.session.cookies = self.load_cookies()

    self.douban_data = self.load_douban_data()

  def normalize_base_url(self, url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
      next_base_url = f"{parts.scheme}://{parts.netloc}/"
      if next_base_url != self.base_url:
        self.log(f"Base url updated: {self.base_url} -> {next_base_url}")
      self.base_url = next_base_url
  
  def log(self, *args, **kw) -> None:
    return print("[%s]" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, **kw)
  
  def load_cookies(self) -> requests.cookies.RequestsCookieJar:
    cookies = None
    if os.path.exists(self.cookies_path):
      try:
        with open(self.cookies_path, "rb") as file:
          cookies = pickle.load(file)
        self.log(f"Cookies loaded from file: {self.cookies_path}")
        return cookies
      except Exception as e:
        self.log(f"Reading cookies error: {e}")
    else:
      self.log(f"Cookies file not exists: {self.cookies_path}")
    if cookies:
      return cookies
    else:
      return requests.cookies.RequestsCookieJar()
  
  def login(self) -> bool:
    try_time = 5
    while True:
      try:
        response = self.session.get(urljoin(self.base_url, "login.php"), timeout=20)
        self.normalize_base_url(response.url)
        tree = BeautifulSoup(response.text, "html.parser")
        form = tree.select_one('form[action*="takelogin.php"]')
        payload = {}
        if form:
          for input_tag in form.select('input[type="hidden"][name]'):
            payload[input_tag.attrs["name"]] = input_tag.attrs.get("value", "")
        payload.update({
          "username": str(self.username or "").strip(),
          "password": str(self.password or "").strip(),
        })

        login_response = self.session.post(urljoin(self.base_url, "takelogin.php"), payload, timeout=20)
        self.normalize_base_url(login_response.url)
        attendance_response = self.session.get(urljoin(self.base_url, "attendance.php"), timeout=20)
        self.normalize_base_url(attendance_response.url)
        if "logout.php" in login_response.text or "login.php" not in attendance_response.url:
          self.log(f"Logged in successfully")
          os.makedirs(os.path.dirname(self.cookies_path), 0o755, True)
          with open(self.cookies_path, "wb") as f:
            pickle.dump(self.session.cookies, f)
          self.log(f"Cookies wrote to file: {self.cookies_path}")
          return True
      except Exception as e:
        self.log(f"Log in request error: {e}")

      try_time -= 1
      if try_time > 0:
        self.log(f"Log in error, try again ({try_time} left)")
      else:
        self.log(f"Log in error after 5 tries")
        return False
  
  def load_douban_data(self) -> dict:
    douban_data = {}
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
    return douban_data
  
  def save_douban_data(self) -> bool:
    try:
      os.makedirs(os.path.dirname(self.douban_path), 0o755, True)
      with open(self.douban_path, "w", encoding="utf-8") as f:
        json.dump(self.douban_data, f, ensure_ascii=False, indent=1)
        self.log(f"Douban data wrote to file: {self.douban_path}")
      return True
    except:
      return False

  def get_id(self, title: str) -> str:
    if title in self.douban_data:
      return self.douban_data[title]

    time.sleep(random.random() * 5)
    response = requests.get(f"https://movie.douban.com/j/subject_suggest?q={requests.utils.requote_uri(title)}", headers={
      "User-Agent": self.session.headers.get("User-Agent"),
      "Referer": "https://movie.douban.com/"
    })
    try:
      url_id = re.findall(r"(?<=/)p\d+(?=\.)", response.json()[0]["img"])[0]
      self.douban_data[title] = url_id
      self.log(f"Title \"{title}\" returned id \"{url_id}\"")
      return url_id
    except Exception as e:
      self.log(f"Error: {e}, title: {title}, response: {response.text}")
      return None
  
  def auto_attendance(self) -> bool:
    try_time = 5
    while True:
      if self.auto_attendance_once():
        self.log(f"Attended successfully")
        return True
      time.sleep(random.random() * 5)
      try_time -= 1
      if try_time > 0:
        self.log(f"Attend error, try again ({try_time} left)")
      else:
        self.log(f"Attend error after 5 tries")
        return False

  def auto_attendance_once(self) -> bool:
    try:
      response = self.session.get(urljoin(self.base_url, "attendance.php"))
      self.normalize_base_url(response.url)
      if "login.php" in response.url:
        self.log("Needed to log in")
        if not self.login():
          return False
        response = self.session.get(urljoin(self.base_url, "attendance.php"))
        self.normalize_base_url(response.url)

      text = response.text
      if "今日已签到" in text:
        self.log("\"今日已签到\" found, already attended")
        return True

      tree = BeautifulSoup(text, "html.parser")

      captcha_image_element = tree.select_one(".captcha img")
      if not captcha_image_element:
        self.log("Captcha image not found")
        return False
      captcha_image = captcha_image_element.attrs.get("src", "")
      captcha_image_id_match = re.search(r"(?<=/)p\d+(?=\.)", captcha_image)
      if not captcha_image_id_match:
        self.log(f"Captcha image id not found, captcha_image: {captcha_image}")
        return False
      captcha_image_id = captcha_image_id_match.group(0)

      captcha_table = tree.select_one(".captcha form table")
      if not captcha_table:
        self.log("Captcha options table not found")
        return False

      captcha_options = []
      for option_input in captcha_table.select('input[name="answer"][type="radio"]'):
        value = option_input.attrs.get("value", "").replace("&amp;", "&")
        id_match = re.search(r"&(\d+)$", value)
        option_id = id_match.group(1) if id_match else ""

        title_parts = []
        for sibling in option_input.next_siblings:
          if getattr(sibling, "name", None) == "br":
            break
          if hasattr(sibling, "get_text"):
            sibling_text = sibling.get_text(" ", strip=True)
          else:
            sibling_text = str(sibling).strip()
          if sibling_text:
            title_parts.append(sibling_text)

        title = "".join(title_parts).strip()
        if not title:
          self.log(f"Captcha option title empty, value: {value}")
          continue
        captcha_options.append((value, option_id, title))

      available_choices = []

      for value, id, title in captcha_options:
        value = value.replace("&amp;", "&")
        url_id = self.get_id(title)
        if captcha_image_id == url_id:
          available_choices.append({
            "value": value,
            "id": id,
            "title": title,
            "url_id": url_id,
            "captcha_image": captcha_image,
          })
          self.log(f"Available choice found: {json.dumps(available_choices[-1], ensure_ascii=False)}")

      self.save_douban_data()
      
      if len(available_choices) == 0:
        self.log(f"No choice found")
        return False
      elif len(available_choices) > 1:
        self.log(f"{len(available_choices)} choices found")
        return False
      else:
        data = {
          "answer": available_choices[0]["value"],
          "submit": "提交"
        }
        response = self.session.post(urljoin(self.base_url, "attendance.php"), data)
        self.normalize_base_url(response.url)
        if "签到成功" in response.text:
          return True
        else:
          self.log(f"\"签到成功\" not found, response_text: {response.text}")
          return False
    except Exception as e:
      self.log(f"Error: {e}")
      return False

if __name__ == "__main__":
  config = {
    "username": None,
    "password": None,
    "base_url": "https://www.tjupt.org/",
    "cookies_path": "data/cookies.pkl",
    "douban_path": "data/douban.json",
  }

  argument_parser = ArgumentParser(description="Auto adttendance bot for TJUPT.")
  argument_parser.add_argument("-i", "--ini-path", default="config/config.ini", help="File path for config.ini (default: config/config.ini). The arguments provided by command line will override the settings in this file.")
  argument_parser.add_argument("-u", "--username", help="Your username for TJUPT.")
  argument_parser.add_argument("-p", "--password", help="Your password for TJUPT.")
  argument_parser.add_argument("-b", "--base-url", help="Base url path for TJUPT (default: https://www.tjupt.org/).")
  argument_parser.add_argument("-c", "--cookies-path", help="File path for cookies.pkl (default: data/cookies.pkl).")
  argument_parser.add_argument("-d", "--douban-path", help="File path for douban.json (default: data/douban.json).")
  args = argument_parser.parse_args()

  if args.ini_path and os.path.exists(args.ini_path):
    config_parser = ConfigParser()
    config_parser.read(args.ini_path, "utf-8")
    for key in config:
      config[key] = str(config_parser.get("Bot", key, fallback=config[key]))

  for key, value in args._get_kwargs():
    if value:
      config[key] = value

  bot = Bot(**config)
  sys.exit(0 if bot.auto_attendance() else 1)
