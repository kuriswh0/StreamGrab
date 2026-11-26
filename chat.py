"""Chat downloaders for Twitch VODs and YouTube live-stream replays.

Both produce a normalized list of messages:
    {"id", "t" (seconds into the video), "user", "color", "badges": [str],
     "parts": [{"text": str} | {"emote": name, "url": image_url}]}
"""
import compat
import html
import json
import os
import subprocess
import urllib.parse
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TWITCH_GQL = "https://gql.twitch.tv/gql"
# Public client ID used by twitch.tv itself (same one TwitchDownloader / yt-dlp use).
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


class Cancelled(Exception):
    pass


def twitch_gql(body, timeout=30):
    req = urllib.request.Request(
        TWITCH_GQL,
        data=json.dumps(body).encode(),
        headers={"Client-ID": TWITCH_CLIENT_ID, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# After ~100 quick requests Twitch throttles for several seconds, and it does so by answering with an
# EMPTY page ("no more chat") instead of an error. A page asked for mid-VOD is never really empty (an
# offset query returns the next messages however far ahead they are), so those are retried.
EMPTY_RETRY_WAITS = (2, 3, 4, 5, 6)


def _comments_page(video_id, offset=None, cursor=None, expect_more=False):
    """One page of VOD chat. expect_more: chat continues after `offset`, so an empty page means Twitch
    is throttling; wait and ask again (up to ~20 s) instead of returning it."""
    for wait in (*EMPTY_RETRY_WAITS, None) if expect_more else (None,):
        page = _comments_page_once(video_id, offset, cursor)
        if page.get("edges") or page.get("pageInfo", {}).get("hasNextPage") or wait is None:
            return page
        threading.Event().wait(wait)
    return page


def _comments_page_once(video_id, offset=None, cursor=None):
    variables = {"videoID": str(video_id)}
    if cursor:
        variables["cursor"] = cursor
    else:
        variables["contentOffsetSeconds"] = int(offset or 0)
    body = [{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": variables,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH}},
    }]
    for attempt in range(5):
        try:
            data = twitch_gql(body)[0]
            if data.get("errors"):
                raise RuntimeError(data["errors"][0].get("message", "GQL error"))
            video = data["data"]["video"]
            if video is None:
                raise RuntimeError("VOD not found (deleted or sub-only?)")
            return video["comments"] or {"edges": [], "pageInfo": {"hasNextPage": False}}
        except (OSError, json.JSONDecodeError, KeyError):
            if attempt == 4:
                raise
            threading.Event().wait(1.5 * (attempt + 1))


def _twitch_msg(node):
    parts = []
    for f in node["message"]["fragments"] or []:
        emote = f.get("emote")
        if emote and emote.get("emoteID"):
            parts.append({
                "emote": f["text"],
                "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{emote['emoteID']}/default/dark/1.0",
            })
        else:
            parts.append({"text": f["text"]})
    commenter = node.get("commenter") or {}
    return {
        "id": node["id"],
        "t": node["contentOffsetSeconds"],
        "user": commenter.get("displayName") or commenter.get("login") or "(deleted)",
        "color": node["message"].get("userColor") or "",
        "badges": [f"{b['setID']}/{b.get('version') or '1'}"
                   for b in node["message"].get("userBadges") or [] if b.get("setID")],
        "parts": parts,
    }


def download_twitch_chat(video_id, duration, start=0, end=None, workers=8,
                         progress=None, cancel=None):
    """Download all chat for a Twitch VOD between [start, end] seconds.

    The range is split into chunks fetched in parallel, each one paging
    forward with cursors until it reaches the next chunk's start.
    """
    end = duration if end is None else min(end, duration or end)
    start = max(0, start or 0)
    span = max(1, (end or 1) - start)
    n = int(max(1, min(workers, span // 120)))
    bounds = [start + span * i / n for i in range(n)] + [end]
    done_secs = [0] * n
    messages = {}
    lock = threading.Lock()

    # Twitch now rejects cursor paging from non-browser clients ("failed
    # integrity check"), so page by time offset instead: each request returns
    # a bucket of messages around the offset; jump to the last timestamp seen.
    def run_chunk(i):
        lo, hi = bounds[i], bounds[i + 1]
        offset = lo
        while True:
            if cancel and cancel.is_set():
                raise Cancelled()
            # Mid-VOD, an empty page means throttling (see _comments_page); near the end it's real.
            page = _comments_page(video_id, offset=offset, expect_more=offset < min(hi, (duration or hi) - 5))
            edges = page.get("edges") or []
            last_t = offset
            with lock:
                for e in edges:
                    node = e["node"]
                    t = node["contentOffsetSeconds"]
                    last_t = max(last_t, t)
                    if lo <= t and (t < hi or (i == n - 1 and t <= hi)):
                        messages[node["id"]] = _twitch_msg(node)
                done_secs[i] = min(hi, last_t) - lo
                if progress:
                    progress(sum(done_secs) / span, len(messages))
            if not edges or last_t >= hi or not page.get("pageInfo", {}).get("hasNextPage"):
                done_secs[i] = hi - lo
                return
            offset = last_t if last_t > offset else offset + 1

    with ThreadPoolExecutor(n) as pool:
        for f in [pool.submit(run_chunk, i) for i in range(n)]:
            f.result()
    out = sorted(messages.values(), key=lambda m: m["t"])
    if progress:
        progress(1.0, len(out))
    return out


def twitch_activity(video_id, duration, bins=120, workers=8):
    """Rough chat activity across a Twitch VOD (messages per minute in `bins` slots).

    Fetches one comments page at the start of each slot (a page holds the next few dozen messages),
    so a busy slot's page covers only a few seconds and a quiet one covers minutes. Much cheaper than
    downloading the whole chat: `bins` requests, run in parallel.
    """
    duration = max(1.0, float(duration or 0))
    bins = int(max(8, min(bins, duration // 15 or 8)))
    step = duration / bins

    def rate(i):
        lo = i * step
        try:
            edges = _comments_page(video_id, offset=lo, expect_more=lo < duration - 60).get("edges") or []
        except Exception:  # noqa: BLE001 - one missing slot just reads as quiet
            return 0.0
        ts = [e["node"]["contentOffsetSeconds"] for e in edges]
        ts = [t for t in ts if lo <= t < lo + step]
        if not ts:
            return 0.0
        # The page may stop before the slot ends; measure the rate over the stretch it covers.
        span = (lo + step) - lo if len(ts) < 20 else max(5.0, max(ts) - lo)
        return len(ts) / span * 60

    with ThreadPoolExecutor(workers) as pool:
        values = list(pool.map(rate, range(bins)))
    return {"duration": duration, "step": step, "per_minute": [round(v, 1) for v in values]}


# ---------------------------------------------------------------- YouTube

def _yt_runs(runs):
    parts = []
    for r in runs or []:
        if "text" in r:
            parts.append({"text": r["text"]})
        elif "emoji" in r:
            em = r["emoji"]
            thumbs = em.get("image", {}).get("thumbnails") or [{}]
            label = (em.get("shortcuts") or [em.get("emojiId", "")])[0]
            if em.get("isCustomEmoji"):
                parts.append({"emote": label, "url": thumbs[0].get("url", "")})
            else:
                parts.append({"text": em.get("emojiId", label)})
    return parts


def parse_youtube_live_chat(path, start=0, end=None):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            replay = obj.get("replayChatItemAction", {})
            t = int(replay.get("videoOffsetTimeMsec", obj.get("videoOffsetTimeMsec", 0))) / 1000
            for action in replay.get("actions", []):
                item = action.get("addChatItemAction", {}).get("item", {})
                r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not r:
                    continue
                if t < (start or 0) or (end is not None and t > end):
                    continue
                badges = []
                for b in r.get("authorBadges", []):
                    tip = b.get("liveChatAuthorBadgeRenderer", {}).get("tooltip", "")
                    if tip:
                        badges.append(tip.split(" ")[0].lower())
                parts = _yt_runs(r.get("message", {}).get("runs"))
                if "purchaseAmountText" in r:
                    parts.insert(0, {"text": f"[{r['purchaseAmountText'].get('simpleText', '')}] "})
                out.append({
                    "id": r.get("id", ""),
                    "t": t,
                    "user": r.get("authorName", {}).get("simpleText", "(unknown)"),
                    "color": "",
                    "badges": badges,
                    "parts": parts,
                })
    out.sort(key=lambda m: m["t"])
    return out


def download_youtube_chat(url, workdir, start=0, end=None, cancel=None, progress=None):
    tmpl = os.path.join(workdir, "%(id)s")
    proc = subprocess.Popen(
        ["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
         "--no-playlist", "--newline", "-o", tmpl, url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    tail = []
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-15:]
        if cancel and cancel.is_set():
            compat.kill_tree(proc)
            raise Cancelled()
        if progress and "[download]" in line:
            progress(None, None)
    proc.wait()
    files = [f for f in os.listdir(workdir) if f.endswith(".live_chat.json")]
    if not files:
        raise RuntimeError("No live chat replay available for this video.\n" + "\n".join(tail[-5:]))
    path = os.path.join(workdir, files[0])
    msgs = parse_youtube_live_chat(path, start, end)
    os.remove(path)
    return msgs


# ---------------------------------------------------------------- writers

def fmt_ts(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def plain_text(msg):
    return "".join(p.get("text") or p.get("emote", "") for p in msg["parts"])


def write_json(path, meta, messages):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"video": meta, "comments": messages}, fh, ensure_ascii=False)


def write_txt(path, messages, offset=0):
    with open(path, "w", encoding="utf-8") as fh:
        for m in messages:
            fh.write(f"[{fmt_ts(m['t'] - offset)}] {m['user']}: {plain_text(m)}\n")


def write_html(path, meta, messages, video_file=None, offset=0):
    """Self-contained chat replay page. If a video file sits next to it, the
    chat scrolls in sync with playback."""
    template = open(os.path.join(os.path.dirname(__file__), "static", "chat_replay.html"),
                    encoding="utf-8").read()
    payload = json.dumps({"meta": meta, "offset": offset, "messages": messages},
                         ensure_ascii=False).replace("</", "<\\/")
    page = (template
            .replace("{{TITLE}}", html.escape(meta.get("title", "Chat replay")))
            .replace("{{VIDEO}}", html.escape(urllib.parse.quote(os.path.basename(video_file))) if video_file else "")
            .replace("{{DATA}}", payload))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
