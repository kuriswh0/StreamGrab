"""Live stream helpers: live status checks and Twitch live chat recording (anonymous IRC)."""
import json
import random
import re
import socket
import ssl
import subprocess
import threading
import time

import chat


def twitch_login(query):
    q = query.strip()
    q = re.sub(r"^(https?://)?(www\.|m\.)?twitch\.tv/", "", q, flags=re.I)
    return q.split("/")[0].split("?")[0].lstrip("@").lower()


def twitch_status(query):
    login = twitch_login(query)
    q = ("query($l:String!){user(login:$l){id login displayName profileImageURL(width:70) "
         "stream{id title viewersCount createdAt game{name} previewImageURL(width:640,height:360)}}}")
    d = chat.twitch_gql({"query": q, "variables": {"l": login}})
    user = (d.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"Twitch channel '{login}' not found")
    s = user.get("stream")
    return {
        "platform": "twitch", "login": user["login"], "channel": user["displayName"],
        "avatar": user.get("profileImageURL") or "", "url": f"https://www.twitch.tv/{user['login']}",
        "live": bool(s), "stream_id": s and s["id"], "title": s and s["title"] or "",
        "viewers": s and s["viewersCount"], "started": s and s["createdAt"],
        "game": s and (s.get("game") or {}).get("name") or "",
        "thumbnail": s and s["previewImageURL"] or "",
    }


def youtube_live_url(query):
    q = query.strip()
    if "watch?v=" in q or "youtu.be/" in q or "/live/" in q:
        return q
    if q.startswith("http"):
        return re.sub(r"/(videos|streams|shorts|featured|live)/?$", "", q.rstrip("/")) + "/live"
    return f"https://www.youtube.com/{q if q.startswith('@') else '@' + q}/live"


def youtube_status(query):
    url = youtube_live_url(query)
    p = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--no-warnings", url],
                       capture_output=True, text=True, timeout=60)
    base = {"platform": "youtube", "url": url, "live": False, "login": "", "avatar": "",
            "channel": query, "title": "", "viewers": None, "started": None, "game": "", "thumbnail": ""}
    if p.returncode != 0:
        err = " ".join(l for l in p.stderr.splitlines() if "ERROR" in l)
        if re.search(r"not currently live|will begin|Premieres|not live|does not have a /live|This live event", err, re.I):
            return {**base, "note": "Not live right now"}
        raise RuntimeError(err or "Could not check this YouTube channel")
    info = json.loads(p.stdout)
    status = info.get("live_status")
    return {**base, "url": info.get("webpage_url") or url, "live": status == "is_live",
            "note": "Scheduled / upcoming" if status == "is_upcoming" else "",
            "channel": info.get("channel") or info.get("uploader") or query,
            "stream_id": info.get("id"), "title": info.get("title") or "",
            "viewers": info.get("concurrent_view_count"), "thumbnail": info.get("thumbnail") or "",
            "started": info.get("release_timestamp")}


def status(platform, query):
    return twitch_status(query) if platform == "twitch" else youtube_status(query)


# ------------------------------------------------------------------ Twitch IRC

def _parse_tags(raw):
    tags = {}
    for kv in raw.split(";"):
        k, _, v = kv.partition("=")
        tags[k] = v.replace(r"\s", " ").replace(r"\:", ";").replace(r"\\", "\\")
    return tags


def _parts_from_irc(text, emotes_tag):
    """Splits a message into text/emote parts using the IRC `emotes` tag (code point offsets)."""
    spans = []
    for group in filter(None, (emotes_tag or "").split("/")):
        eid, _, ranges = group.partition(":")
        for r in ranges.split(","):
            a, _, b = r.partition("-")
            if a.isdigit() and b.isdigit():
                spans.append((int(a), int(b), eid))
    spans.sort()
    parts, pos = [], 0
    for a, b, eid in spans:
        if a > pos:
            parts.append({"text": text[pos:a]})
        parts.append({"emote": text[a:b + 1],
                      "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/1.0"})
        pos = b + 1
    if pos < len(text):
        parts.append({"text": text[pos:]})
    return parts or [{"text": text}]


class TwitchChatRecorder(threading.Thread):
    """Collects a channel's live chat. Each message gets `wall` = arrival time (epoch seconds)."""

    def __init__(self, login):
        super().__init__(daemon=True)
        self.login = login.lower()
        self.messages = []
        self.stop_event = threading.Event()
        self.error = ""

    def stop(self):
        self.stop_event.set()

    def run(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self._session()
                backoff = 1
            except OSError as e:
                self.error = str(e)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _session(self):
        raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=15)
        s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
        s.settimeout(1.0)
        nick = f"justinfan{random.randint(10000, 99999)}"
        for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE",
                     f"NICK {nick}", f"JOIN #{self.login}"):
            s.sendall((line + "\r\n").encode())
        buf, last_rx = b"", time.time()
        try:
            while not self.stop_event.is_set():
                try:
                    data = s.recv(65536)
                    if not data:
                        raise OSError("chat connection closed")
                    last_rx = time.time()
                except socket.timeout:
                    if time.time() - last_rx > 360:  # Twitch PINGs every ~5 min
                        raise OSError("chat connection timed out")
                    continue
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for b in lines:
                    line = b.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        s.sendall(("PONG" + line[4:] + "\r\n").encode())
                    elif " PRIVMSG #" in line:
                        self._privmsg(line)
                    elif " RECONNECT" in line:
                        raise OSError("server asked to reconnect")
        finally:
            s.close()

    def _privmsg(self, line):
        now = time.time()
        tags = {}
        if line.startswith("@"):
            raw_tags, _, line = line[1:].partition(" ")
            tags = _parse_tags(raw_tags)
        prefix, _, rest = line.partition(" PRIVMSG #")
        _, _, text = rest.partition(" :")
        login = prefix.lstrip(":").split("!")[0]
        action = text.startswith("\x01ACTION ") and text.endswith("\x01")
        if action:
            text = text[8:-1]
        self.messages.append({
            "id": tags.get("id") or f"{now}",
            "wall": now,
            "user": tags.get("display-name") or login,
            "color": tags.get("color", ""),
            "badges": [b for b in (tags.get("badges") or "").split(",") if b],
            "parts": _parts_from_irc(text, tags.get("emotes")),
        })
