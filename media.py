"""Windows media session (SMTC) bridge for the YouTube Music PWA, plus pure helpers.

The winrt calls live on one asyncio loop in a QThread; the UI only receives plain
snapshots through a Qt signal and sends commands back with `bridge.command(...)`.
"""
import asyncio
import time
from urllib.parse import urlparse, parse_qs

# SMTC reports the PWA as `Chrome._crx_<app id>` (the id arrives shortened); match the prefix.
SESSION_PREFIX = 'Chrome._crx_cinhimbnkk'


def pick_session(ids, playing=None, prefix=SESSION_PREFIX):
    """Index of the YouTube Music session among SMTC source ids, else None.

    Every app window has its own session; while switching two can exist, so a playing one wins.
    """
    matches = [i for i, value in enumerate(ids) if value and value.startswith(prefix)]
    playing = playing or [False]*len(ids)
    return next((i for i in matches if playing[i]), matches[0] if matches else None)


def progress(position, end, updated, now, playing):
    """Seconds played. Chrome only refreshes the timeline on events, so extrapolate while playing."""
    value = position + (max(0.0, now-updated) if playing and updated else 0.0)
    return max(0.0, min(end, value)) if end else max(0.0, value)


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f'{hours}:{rest//60:02d}:{rest%60:02d}' if hours else f'{rest//60}:{rest%60:02d}'


def playlist_id(url):
    """The `list=` id of a YouTube Music link, or None. Tracking params (`si=`) are dropped."""
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, ValueError):
        return None
    if not parsed.netloc.endswith(('youtube.com', 'youtu.be')):
        return None
    values = parse_qs(parsed.query).get('list')
    return values[0] if values and values[0].replace('-', '').replace('_', '').isalnum() else None


def watch_url(list_id):
    """A playlist `playlist?list=` page starts no media session; `watch?list=` does."""
    return f'https://music.youtube.com/watch?list={list_id}'


def focus_action(previous, current):
    """What the player should do when the Focus Timer's mode changes: 'pause', 'resume' or None."""
    if previous == current or previous is None:
        return None
    if previous == 'focus' and current in ('short', 'long'):
        return 'pause'
    if current == 'focus' and previous in ('short', 'long'):
        return 'resume'
    return None


def repeat_decision(last, track, pos, end, playing, armed, lead=1.2):
    """Repeat-one step. `last` = (title, artist, position, end) from the previous snapshot.

    'restart' `lead` seconds before the end; 'previous' when YouTube Music already moved on to the
    next song after the old one ran out; otherwise None.
    """
    if last:
        old_track, old_pos, old_end = tuple(last[:2]), last[2], last[3]
        if tuple(track) != old_track and old_end and old_pos >= old_end-3:
            return 'previous'
    if playing and end > 5 and pos >= end-lead and armed:
        return 'restart'
    return None


def _seconds(value):
    return value.total_seconds() if hasattr(value, 'total_seconds') else float(value or 0)


def _epoch(value):
    try:
        return value.timestamp() if value and value.year > 1601 else 0.0
    except (AttributeError, OverflowError, OSError, ValueError):
        return 0.0


try:
    from PySide6.QtCore import QThread, Signal
except ImportError:  # pure helpers stay importable without Qt
    QThread, Signal = object, None


class MediaBridge(QThread):
    snapshot = Signal(dict)

    def __init__(self, interval=0.5):
        super().__init__()
        self.interval = interval
        self.loop = None
        self.session = None
        self.track = None
        self.track_at = self.cover_at = 0.0
        self.cover = b''

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.main())
        self.loop.run_forever()     # stop() ends it; run_until_complete would raise on stop

    def stop(self):
        if self.loop:
            def shutdown():
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                self.loop.call_soon(self.loop.stop)
            self.loop.call_soon_threadsafe(shutdown)

    async def main(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
        manager = None
        while True:
            try:
                if manager is None:
                    manager = await Manager.request_async()
                self.snapshot.emit(await self.read(manager))
            except Exception as error:  # Also retry Windows media service startup/restarts.
                manager = None
                self.session = None
                self.snapshot.emit({'ok': False, 'error': type(error).__name__})
            await asyncio.sleep(self.interval)

    async def read(self, manager):
        sessions = list(manager.get_sessions())
        ids = [s.source_app_user_model_id for s in sessions]
        playing = [int(s.get_playback_info().playback_status) == 4 for s in sessions]
        index = pick_session(ids, playing)
        if index is None:
            self.session = None
            return {'ok': False, 'others': []}
        session = self.session = sessions[index]
        others = []   # (title, playing) of the other YouTube Music windows, e.g. the old one while switching
        for i, other in enumerate(sessions):
            if i != index and pick_session([ids[i]]) == 0:
                try:
                    others.append(((await other.try_get_media_properties_async()).title, playing[i]))
                except Exception:
                    pass
        props = await session.try_get_media_properties_async()
        info = session.get_playback_info()
        timeline = session.get_timeline_properties()
        track = (props.title, props.artist)
        now = time.time()
        # Chrome first reports its own icon and swaps in the artwork a moment later: keep
        # re-reading the cover for a few seconds after every track change.
        if track != self.track:
            self.track, self.track_at = track, now
        if self.cover_at < self.track_at or (now-self.track_at < 15 and now-self.cover_at >= 1.5):
            self.cover, self.cover_at = await self.read_cover(props.thumbnail), now
        controls = info.controls
        return {'ok': True, 'title': props.title or '', 'artist': props.artist or '',
                'playing': int(info.playback_status) == 4, 'status': int(info.playback_status),
                'position': _seconds(timeline.position), 'end': _seconds(timeline.end_time),
                'updated': _epoch(timeline.last_updated_time), 'cover': self.cover,
                'can_next': bool(controls.is_next_enabled), 'can_previous': bool(controls.is_previous_enabled),
                'can_seek': bool(controls.is_playback_position_enabled), 'read_at': time.time(), 'others': others}

    @staticmethod
    async def read_cover(reference):
        if reference is None:
            return b''
        from winrt.windows.storage.streams import Buffer, InputStreamOptions
        try:
            stream = await reference.open_read_async()
            buffer = Buffer(stream.size)
            await stream.read_async(buffer, stream.size, InputStreamOptions.READ_AHEAD)
            return bytes(memoryview(buffer))
        except Exception:
            return b''

    def command(self, name, value=None):
        """Thread-safe: 'toggle', 'play', 'pause', 'next', 'previous', 'seek' (seconds)."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.run_command(name, value), self.loop)

    async def run_command(self, name, value):
        session = self.session
        if session is None:
            return False
        calls = {'toggle': session.try_toggle_play_pause_async, 'play': session.try_play_async,
                 'pause': session.try_pause_async, 'next': session.try_skip_next_async,
                 'previous': session.try_skip_previous_async}
        try:
            if name == 'seek':
                return await session.try_change_playback_position_async(int(max(0, value)*10_000_000))
            return await calls[name]()
        except Exception:
            return False
