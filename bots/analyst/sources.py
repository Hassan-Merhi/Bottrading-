"""Collect recent items from RSS feeds and public Telegram channels.

Telegram channels are read from their public web preview (t.me/s/<name>), the
same page anyone sees in a browser: no account, no login, no joining groups.
Everything fetched here is untrusted text.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

import requests

MAX_BYTES = 2_000_000
MAX_TEXT = 600
CHANNEL_NAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")
HEADERS = {"User-Agent": "trading-bots-analyst/1.0 (+news digest)"}


@dataclass
class Item:
    source: str
    title: str
    text: str
    url: str
    published: int | None  # ms


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(html: str) -> str:
    p = _TextOnly()
    p.feed(html or "")
    return " ".join(" ".join(p.parts).split())


def _clip(text: str, n: int = MAX_TEXT) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _get(session, url: str) -> str:
    r = session.get(url, timeout=20, headers=HEADERS)
    r.raise_for_status()
    return r.text[:MAX_BYTES]


def _ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def parse_feed(xml_text: str, source: str) -> list[Item]:
    """RSS 2.0 or Atom."""
    root = ElementTree.fromstring(xml_text)
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: c for c in node}
        title = strip_html((fields["title"].text or "") if "title" in fields else "")
        body = ""
        for key in ("description", "summary", "content"):
            if key in fields and fields[key].text:
                body = strip_html(fields[key].text)
                break
        link = ""
        if "link" in fields:
            link = fields["link"].text or fields["link"].get("href", "") or ""
        published = None
        for key in ("pubDate", "published", "updated"):
            if key in fields:
                published = _ms(fields[key].text)
                break
        if title:
            items.append(Item(source, _clip(title, 200), _clip(body), link.strip(), published))
    return items


class _ChannelParser(HTMLParser):
    """Messages on a t.me/s/<channel> page: text and timestamp per post."""

    def __init__(self):
        super().__init__()
        self.posts: list[dict] = []
        self._depth = 0          # div depth inside the current post
        self._text_depth = 0     # div depth inside the post's text block

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            if self._depth:
                self._depth += 1
                if self._text_depth:
                    self._text_depth += 1
                elif "tgme_widget_message_text" in cls:
                    self._text_depth = 1
            elif "tgme_widget_message " in cls + " " and a.get("data-post"):
                self._depth = 1
                self.posts.append({"post": a["data-post"], "text": [], "time": None})
        elif tag == "time" and self._depth and a.get("datetime"):
            self.posts[-1]["time"] = a["datetime"]
        elif tag == "br" and self._text_depth:
            self.posts[-1]["text"].append(" ")

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1
            if self._text_depth:
                self._text_depth -= 1

    def handle_data(self, data):
        if self._text_depth:
            self.posts[-1]["text"].append(data)


def parse_channel(html: str, channel: str) -> list[Item]:
    p = _ChannelParser()
    p.feed(html)
    items = []
    for post in p.posts:
        text = " ".join("".join(post["text"]).split())
        if text:
            items.append(Item(f"t.me/{channel}", _clip(text, 200), _clip(text),
                              f"https://t.me/{post['post']}", _ms(post["time"])))
    return items


def collect(acfg: dict, session=None, now_ms: int | None = None) -> tuple[list[Item], list[str]]:
    """Recent items from every configured source, newest first, plus any fetch errors."""
    session = session or requests.Session()
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - acfg["lookback_hours"] * 3_600_000
    items: list[Item] = []
    errors: list[str] = []
    for url in acfg.get("rss_feeds") or []:
        try:
            host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            items += parse_feed(_get(session, url), host)
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the digest
            errors.append(f"{url}: {e}")
    for name in acfg.get("telegram_channels") or []:
        if not CHANNEL_NAME.match(str(name)):
            errors.append(f"{name}: not a valid public channel name")
            continue
        try:
            items += parse_channel(_get(session, f"https://t.me/s/{name}"), name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"t.me/{name}: {e}")
    seen, fresh = set(), []
    for it in sorted(items, key=lambda i: i.published or 0, reverse=True):
        key = it.title.lower()
        if key in seen or (it.published is not None and it.published < since):
            continue
        seen.add(key)
        fresh.append(it)
    return fresh[: acfg["max_items"]], errors
