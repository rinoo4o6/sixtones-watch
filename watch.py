#!/usr/bin/env python3
"""YouTubeチャンネルの全動画に付いた「投稿主自身のコメント」を検知して ntfy に通知する。

allThreadsRelatedToChannelId でチャンネル全体の新着コメントを新しい順に取得し、
前回チェックした時刻までさかのぼって、投稿主(=チャンネル)のコメントだけ通知する。
(返信コメントは対象外)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))    # 1回の実行で読む最大ページ数(1ページ=100スレッド)
OVERLAP_MIN = int(os.environ.get("OVERLAP_MIN", "10"))  # 取りこぼし防止のため、前回時刻より少し前まで読む
NOTIFIED_KEEP = 500


def yt(path, **params):
    params["key"] = os.environ["YT_API_KEY"]
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"YouTube API {e.code}: {e.read().decode(errors='replace')[:300]}")


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def resolve_channel_id(state):
    if os.environ.get("CHANNEL_ID"):
        return os.environ["CHANNEL_ID"]
    if state.get("channel_id"):
        return state["channel_id"]
    handle = os.environ.get("CHANNEL_HANDLE")
    if not handle:
        sys.exit("CHANNEL_ID か CHANNEL_HANDLE を設定してください")
    d = yt("channels", part="snippet", forHandle=handle)
    if not d.get("items"):
        sys.exit("チャンネルが見つかりません")
    it = d["items"][0]
    print(f"監視チャンネル: {it['snippet']['title']} ({it['id']})")
    return it["id"]


def fetch_new_threads(channel_id, since):
    """since(datetime)以降のスレッドを新しい順に返す。sinceがNoneなら1ページのみ。"""
    out, token = [], None
    for _ in range(1 if since is None else MAX_PAGES):
        params = dict(part="snippet", allThreadsRelatedToChannelId=channel_id,
                      order="time", maxResults=100, textFormat="plainText")
        if token:
            params["pageToken"] = token
        d = yt("commentThreads", **params)
        items = d.get("items", [])
        out += items
        token = d.get("nextPageToken")
        if not token or not items:
            break
        if since and parse(items[-1]["snippet"]["topLevelComment"]["snippet"]["publishedAt"]) < since:
            break
    return out


def video_title(vid):
    try:
        d = yt("videos", part="snippet", id=vid)
        return d["items"][0]["snippet"]["title"]
    except Exception:
        return vid


def notify(title, text, url):
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    payload = {"topic": os.environ["NTFY_TOPIC"], "title": title[:200],
               "message": text[:300] or "(本文なし)", "click": url, "tags": ["speech_balloon"]}
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    req = urllib.request.Request(server, json.dumps(payload).encode(), headers, method="POST")
    urllib.request.urlopen(req, timeout=30).close()


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    init = "last_ts" not in state
    state.setdefault("notified", [])
    channel_id = state["channel_id"] = resolve_channel_id(state)

    since = None if init else parse(state["last_ts"]) - timedelta(minutes=OVERLAP_MIN)
    threads = fetch_new_threads(channel_id, since)
    if not threads:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
        return

    newest = max(t["snippet"]["topLevelComment"]["snippet"]["publishedAt"] for t in threads)
    failed = False
    if init:
        print("初回: 既存コメントは通知せず、基準時刻だけ記録しました")
    else:
        for t in reversed(threads):  # 古い順に通知
            c = t["snippet"]["topLevelComment"]
            s = c["snippet"]
            if (s.get("authorChannelId") or {}).get("value") != channel_id or c["id"] in state["notified"]:
                continue
            if parse(s["publishedAt"]) < since:
                continue
            vid = s.get("videoId", "")
            try:
                notify(f"公式がコメント: {video_title(vid)}", s.get("textOriginal", ""),
                       f"https://www.youtube.com/watch?v={vid}&lc={c['id']}")
                state["notified"].append(c["id"])
                print(f"通知: {vid} / {s.get('textOriginal', '')[:40]}")
            except Exception as e:
                failed = True
                print(f"[notify失敗] {e}", file=sys.stderr)

    if not failed:  # 失敗時は基準時刻を進めず、次回再試行(重複はnotifiedで防止)
        state["last_ts"] = max(newest, state.get("last_ts", ""))
    state["notified"] = state["notified"][-NOTIFIED_KEEP:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
