"""Track list of a YouTube Music playlist: ids, titles and artists only (no audio).

Uses yt-dlp's flat-playlist mode, cached in state/ and refreshed in the background.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_AGE = 12*3600


def parse(data):
    """yt-dlp -J output -> [{'id', 'title', 'artist', 'duration'}], unavailable entries dropped."""
    tracks = []
    for entry in (data or {}).get('entries') or []:
        if entry and entry.get('id') and entry.get('title'):
            tracks.append({'id': entry['id'], 'title': entry['title'],
                           'artist': entry.get('channel') or entry.get('uploader') or '',
                           'duration': entry.get('duration') or 0})
    return tracks


def cache_path(state_dir, list_id):
    return Path(state_dir)/f'tracks-{list_id}.json'


def cached(state_dir, list_id):
    try:
        data = json.loads(cache_path(state_dir, list_id).read_text(encoding='utf-8'))
        return data.get('tracks', []), data.get('fetched', 0)
    except (OSError, ValueError, AttributeError):
        return [], 0


def fetch(state_dir, list_id, timeout=180):
    """Blocking: run yt-dlp and cache the result. Returns the tracks, or [] on failure."""
    exe = shutil.which('yt-dlp')
    cmd = [exe] if exe else [sys.executable, '-m', 'yt_dlp']
    try:
        out = subprocess.run(cmd + ['--flat-playlist', '--no-warnings', '-J',
                                    f'https://music.youtube.com/playlist?list={list_id}'],
                             capture_output=True, timeout=timeout,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        tracks = parse(json.loads(out.stdout.decode('utf-8', 'replace')))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if tracks:
        path = cache_path(state_dir, list_id); path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'fetched': time.time(), 'tracks': tracks}, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)
    return tracks


def stale(fetched, now=None):
    return (now or time.time()) - fetched > MAX_AGE


def list_from_location(location):
    """The temporary `list=` id in YouTube's watch_videos redirect, or None."""
    from urllib.parse import urlparse, parse_qs
    try:
        values = parse_qs(urlparse(location or '').query).get('list')
    except ValueError:
        return None
    return values[0] if values and values[0].startswith('TLGG') and values[0].replace('_', '').replace('-', '').isalnum() else None


def temp_list(ids, timeout=15):
    """Blocking: turn up to 50 video ids into a temporary YouTube list (TLGG...) in the given order.

    youtube.com/watch_videos answers with a redirect that carries the list id; YouTube Music
    plays that list in order and moves between its songs itself, without reloading the page.
    """
    import urllib.request, urllib.error

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    url = 'https://www.youtube.com/watch_videos?video_ids=' + ','.join(ids[:50])
    try:
        urllib.request.build_opener(Stay).open(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=timeout)
    except urllib.error.HTTPError as redirect:
        return list_from_location(redirect.headers.get('Location'))
    except (OSError, ValueError):
        return None
    return None


def _norm(text):
    return ''.join(ch for ch in (text or '').lower() if ch.isalnum())


def match_index(title, titles, start=0):
    """Where the song Windows reports sits in our queue. SMTC titles are often shorter than
    yt-dlp's ("Heyecandan" vs "Oğuzhan Koç - Heyecandan (Official Video)"), so match by
    containment, searching forward from `start` first. None when nothing matches."""
    key = _norm(title)
    if len(key) < 3:
        return None
    order = list(range(start, len(titles))) + list(range(0, start))
    for i in order:
        other = _norm(titles[i])
        if other and (key in other or other in key):
            return i
    return None


def ready_usable(ready, now=None):
    """A prepared queue can start at once when its temporary list exists and is from today
    (the TLGG id carries the date it was made) and at most 12 h old."""
    import datetime as dt
    if not isinstance(ready, dict) or not ready.get('ids') or not str(ready.get('list', '')).startswith('TLGG'):
        return False
    now = time.time() if now is None else now
    made = ready.get('made', 0)
    return 0 <= now-made <= 12*3600 and dt.date.fromtimestamp(made) == dt.date.fromtimestamp(now)
