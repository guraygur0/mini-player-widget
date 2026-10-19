"""Mini Player Widget: a desktop card that drives the hidden YouTube Music app through SMTC."""
import json
import os
import sys
import threading
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QPoint, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QImage, QFontDatabase, QCursor, QPen
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QInputDialog, QToolTip
from card import (BLUE, TEXT, MUTED, CANVAS_W, RIGHT, MENU_STYLE, draw_text, draw_card, draw_circle,
                  glyph_pen, clamp_to_screen, blurred_backdrop)
from media import MediaBridge, progress, fmt_time, playlist_id, watch_url, focus_action, repeat_decision
from playlist import match_index
import volume as chrome_volume
import playlist as tracklist
from shuffle import pick_next

ROOT = Path(__file__).resolve().parent
STATE = ROOT/'state'/'player.json'
USAGE_WINDOW = ROOT.parent/'usage-widget'/'state'/'window.json'   # read only: match the set's size
FOCUS_STATE = ROOT.parent/'focus-widget'/'state'/'focus.json'     # read only: pause on breaks
MODES = ('off', 'shuffle', 'one')   # the mode button cycles through these
QUEUE = 50                          # songs per shuffled queue (YouTube's temporary lists take 50)
DEFAULT_PLAYLISTS = [{'name': 'Your liked songs', 'id': 'LM'}]
H = 300   # playlist + volume row removed from the compact layout; both live in the right-click menu


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Player(QWidget):
    queue_ready = Signal(list, str, str)
    ready_built = Signal(list, str, str)

    def __init__(self, preview=None):
        super().__init__()
        self.preview = preview
        self.ui = read_json(STATE)
        self.options = self.ui  # desktop_layer reads options['topmost']
        playlists = [p for p in self.ui.get('playlists', []) if isinstance(p, dict) and playlist_id(watch_url(str(p.get('id', ''))))]
        self.ui['playlists'] = playlists or [dict(p) for p in DEFAULT_PLAYLISTS]
        if not isinstance(self.ui.get('current'), int) or not 0 <= self.ui['current'] < len(self.ui['playlists']):
            self.ui['current'] = 0
        scale = self.ui.get('scale', read_json(USAGE_WINDOW).get('scale', .65))
        self.scale = scale if isinstance(scale, (int, float)) and .4 <= scale <= 1.5 else .65
        self.data = {'ok': False}
        self.cover_bytes = b''
        self.cover = QImage()
        self.backdrop = QImage()
        self.backdrop_key = None
        self.hover = self.pressed = self.offset = None
        self.dragging_seek = False
        self.seek_preview = None
        self.pending = None          # playlist launch in progress
        self.focus_mode = None
        self.paused_by_focus = False
        self.last_track = None       # (title, artist, position, end) from the previous snapshot
        self.repeat_armed = True
        if self.ui.pop('repeat_one', False) and self.ui.get('mode') not in MODES:
            self.ui['mode'] = 'one'
        if self.ui.get('mode') not in MODES:
            self.ui['mode'] = 'off'
        self.tracks, self.shuffle_state = [], {}
        self.fetching = False
        self.queue = None            # {'ids', 'titles', 'list', 'index'}: the shuffled order YouTube Music is playing
        self.building = False
        self.shown_title = None
        self.clock = {'pos': 0.0, 'at': 0.0, 'playing': False, 'since': 0.0}   # our own count since the song began
        self.durations = {}
        self.guard = None            # hand the keyboard back after Chrome activates a window
        self.present_hwnd = None     # the playing app window, kept visible-to-Chrome but unseen
        self.user_fg = 0
        self.queue_ready.connect(self.on_queue_ready)
        self.ready_built.connect(self.on_ready_built)
        self.preparing = False
        self.started_at = time.time()
        self.setWindowTitle('Mini Player')
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        if self.ui.get('topmost'):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(round(CANVAS_W*self.scale), round(H*self.scale))
        self.buttons = {'cover': QRectF(29,84,132,132), 'previous': QRectF(181,154,48,48), 'play': QRectF(239,146,64,64),
                        'next': QRectF(313,154,48,48), 'repeat': QRectF(373,158,40,40), 'window': QRectF(485,158,40,40),
                        'seek': QRectF(29,226,496,30)}
        area = QApplication.primaryScreen().availableGeometry()
        pos = self.ui.get('pos')
        if isinstance(pos, list) and len(pos) == 2 and all(isinstance(n, int) for n in pos): self.move(*pos)
        else:
            # Top-right, mirroring the AI limit card on the left: same top edge, same side margin.
            usage = read_json(USAGE_WINDOW)
            ux, uy = usage.get('x'), usage.get('y')
            margin = ux-area.left() if isinstance(ux, int) and 0 <= ux-area.left() <= 400 else 40
            top = uy if isinstance(uy, int) and area.top() <= uy <= area.top()+400 else area.top()+70
            self.move(area.right()-self.width()+1-margin, top)
        clamp_to_screen(self)
        self.layer = self.bridge = self.windows = None
        if preview is None:
            from desktop_layer import DesktopLayer
            from ytm import YTMWindows
            self.layer = DesktopLayer(self)
            self.windows = YTMWindows()
            self.bridge = MediaBridge(); self.bridge.snapshot.connect(self.receive); self.bridge.start()
            self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(250)
            self.slow = QTimer(self); self.slow.timeout.connect(self.slow_tick); self.slow.start(2000)
            self.fast = QTimer(self); self.fast.timeout.connect(self.launch_tick)
            self.load_tracks()
            self.slow_tick()
            self.log(f'widget started (app windows open: {len(self.app_windows())}, tracks cached: {len(self.tracks)})')
        for screen in QApplication.screens(): screen.availableGeometryChanged.connect(lambda *_: clamp_to_screen(self))

    def log(self, message):
        path = ROOT/'state'/'player.log'
        try:
            if path.exists() and path.stat().st_size > 200_000:
                path.replace(path.with_suffix('.log.1'))      # keep one old file, never grow without end
            with open(path, 'a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + message + '\n')
        except OSError:
            pass

    # --- state ---
    @property
    def playlist(self):
        return self.ui['playlists'][self.ui['current']]

    def save(self):
        if self.preview is not None: return
        self.ui.update(pos=[self.x(), self.y()], scale=self.scale,
                       topmost=bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        try:
            STATE.parent.mkdir(exist_ok=True)
            tmp = STATE.with_suffix('.tmp'); tmp.write_text(json.dumps(self.ui, ensure_ascii=False), encoding='utf-8'); os.replace(tmp, STATE)
        except OSError:
            pass

    def receive(self, data):
        self.data = data
        cover = data.get('cover', b'')
        if cover != self.cover_bytes:
            self.cover_bytes = cover
            self.cover = QImage.fromData(cover) if cover else QImage()
            self.backdrop_key = None
        if self.seek_preview and time.time() > self.seek_preview[1]:
            self.seek_preview = None
        now = time.time(); c = self.clock
        if c['playing']: c['pos'] += now-c['at']
        c['at'], c['playing'] = now, bool(data.get('playing'))
        if data.get('ok') and not self.pending and data.get('title') != self.shown_title:
            self.shown_title = data.get('title'); self.clock.update(pos=0.0, since=now)
            self.track_changed(self.shown_title)
        self.advance_launch()

    # --- timers ---
    def tick(self):
        now = time.time()
        if self.windows:
            found = self.windows.find()
            handles = [h for h, _, _ in found]
            fg = self.windows.foreground()
            if fg and fg not in handles:
                if self.windows.focusable(fg): self.user_fg = fg
            elif fg and not self.ui.get('show_window') and (
                    self.windows.is_present(fg) or not any(h == fg and visible for h, _, visible in found)):
                if not self.windows.give_focus(self.user_fg):    # an unseen window must never hold the keyboard
                    self.log(f'focus hand-back failed: fg={fg} visible={bool(self.windows.u.IsWindowVisible(fg))} '
                             f'present={self.windows.is_present(fg)} alive={bool(self.windows.u.IsWindow(fg))} '
                             f'user_fg={self.user_fg} pending={bool(self.pending)} | {self.windows.debug}')
            if not self.ui.get('show_window') and not self.pending:
                # Never while a start is under way: hiding the new window in the ~30 ms before
                # launch_tick recognises it stops Chrome's first paint (the blank-page stalls).
                self.windows.hide(keep=[self.present_hwnd] if self.present_hwnd else [])
                if self.present_hwnd in handles and not self.windows.is_present(self.present_hwnd) and not self.pending:
                    self.windows.present(self.present_hwnd)
            if self.present_hwnd and self.present_hwnd not in handles:
                self.present_hwnd = None
            if not self.present_hwnd and not self.pending and handles:
                self.present_hwnd = self.current_window(found)   # e.g. the widget restarted while music played
        if self.pending and now > self.pending['until']:
            self.finish_launch()
        self.keep_repeating()
        if self.near_end() and not self.fast.isActive(): self.fast.start(30)
        self.update()
        if self.layer: self.layer.maintain()

    def slow_tick(self):
        if time.time()-self.started_at > 4 and not self.pending and not self.building:
            self.prepare_queue()    # after start-up settles; offline it simply tries again later
        if self.ui.get('pause_on_break'):
            mode = read_json(FOCUS_STATE).get('mode')
            action = focus_action(self.focus_mode, mode)
            if action == 'pause' and self.data.get('playing'):
                self.bridge.command('pause'); self.paused_by_focus = True
            elif action == 'resume' and self.paused_by_focus:
                self.bridge.command('play'); self.paused_by_focus = False
            self.focus_mode = mode
        else:
            self.focus_mode = None

    # --- cold start of a playlist or queue (measurements in notes.md) ---
    # Chrome only starts playback in a window that was shown once, so the new window is shown
    # at 1/255 opacity and click-through; its first navigation sometimes stays blank, so it is
    # recovered by recover() if silent; a playing window asks "Leave site?" when closed, so it is paused
    # first; Chrome activates every window it opens, so the keyboard is handed back.
    def current_window(self, found):
        """The app window playing now: the only one, or the one whose title names the song."""
        song = (self.data.get('title') or '').lower()
        named = [h for h, title, _ in found if song and song in title.lower()]
        return (named or [h for h, _, _ in found])[0]

    def app_windows(self):
        return [h for h, _, _ in self.windows.find()] if self.windows else []

    def launch(self, url):
        if not self.windows: return
        if self.data.get('playing'): self.bridge.command('pause')
        old = self.app_windows()
        self.windows.launch(url)
        self.pending = {'old': old, 'new': None, 'closed': not old, 'at': time.time(), 'until': time.time()+45,
                        'asked': False, 'stage': 0, 'tries': 0, 'url': url, 'from': self.data.get('title'), 'first': time.time()}
        self.bridge.interval = .15      # notice the new song quickly; back to .5 once it plays
        self.last_track = None
        self.fast.start(30); self.update()

    def guard_focus(self, hwnd):
        self.guard = {'hwnds': ({*self.guard['hwnds']} if self.guard else set()) | {hwnd}, 'until': time.time()+2.5}
        self.windows.give_focus(self.user_fg)
        self.fast.start(30)

    def old_playing(self, p):
        d = self.data
        sessions = [(d.get('title'), d.get('playing'))] + list(d.get('others', [])) if d.get('ok') else list(d.get('others', []))
        return any(playing and title == p['from'] for title, playing in sessions) if p['from'] else any(pl for _, pl in sessions)

    def near_end(self):
        d = self.data
        end = self.duration()
        return bool(d.get('ok') and d.get('playing') and end > 5 and 0 <= end-self.position() < 3)

    def playing_new(self):
        p = self.pending
        return bool(p) and self.data.get('playing') and (self.data.get('title') != p['from'] or time.time()-p['at'] > 3)

    def launch_tick(self):
        now, p = time.time(), self.pending
        if p and p['new'] is None:
            new = [h for h in self.app_windows() if h not in p['old']]
            if new:
                p['new'] = new[0]; self.windows.stealth(new[0], True); self.windows.on_top(new[0]); self.guard_focus(new[0])
        if p and p['new'] and not p['closed']:
            # Never close a playing window: Chrome answers with a hidden "Leave site?" dialog that
            # freezes the page (audio keeps going, commands stop working).
            if not self.old_playing(p):
                self.windows.close(p['old']); p['closed'] = True
            elif now-p.get('paused_at', p['at']) > 2:
                self.bridge.command('pause'); p['paused_at'] = now
        if p: self.recover(p, now)
        near_end = self.near_end()
        if near_end: self.keep_repeating()
        g = self.guard
        if g and now < g['until']:
            if self.windows.foreground() in g['hwnds']:
                self.windows.give_focus(self.user_fg)
        else:
            self.guard = None
        if not (self.guard or self.pending or near_end): self.fast.stop()

    def recover(self, p, now):
        """Escalate while a cold start stays silent. A starting page often waits for a window event
        once the keyboard is handed back (measured: every start began ~0.7 s after a resize), so the
        invisible window is nudged at 0.6-3.6 s; then Reload at 7.5 s; then close it and open a
        new one at 12 s (twice)."""
        if self.playing_new(): return
        age = now-p['at']
        if p['new'] is None:
            if age > 8 and p['tries'] < 2: self.relaunch(p)        # the window never appeared
            return
        if not p['closed']: return
        steps = tuple((t, self.windows.nudge) for t in (0.6, 1.2, 1.8, 2.6, 3.6)) + ((7.5, self.windows.reload),)
        if p['stage'] < len(steps) and age > steps[p['stage']][0]:
            steps[p['stage']][1](p['new']); p['stage'] += 1
        elif p['stage'] >= len(steps) and age > 12 and p['tries'] < 2:
            self.relaunch(p)

    def relaunch(self, p):
        if p['new'] and not self.data.get('playing'):
            self.windows.close([p['new']])      # silent, so closing is safe
        p['old'] = [h for h in self.app_windows() if h != p['new']] + ([p['new']] if p['new'] else [])
        p.update(new=None, stage=0, tries=p['tries']+1, at=time.time(), asked=False)
        self.log(f"cold start retry {p['tries']}")
        self.windows.launch(p['url'])

    def advance_launch(self):
        p = self.pending
        if not p or not p['new'] or not p['closed']: return
        if any(h in p['old'] for h in self.app_windows()): return
        if self.playing_new():
            self.finish_launch()
        elif self.data.get('ok') and time.time()-p['at'] > 7 and not p['asked']:
            self.bridge.command('play'); p['asked'] = True

    def finish_launch(self):
        p, self.pending = self.pending, None
        if p:
            self.log(f"start {'ok' if self.data.get('playing') else 'GAVE UP'} after {time.time()-p['first']:.1f}s "
                     f"(retries {p['tries']}, stage {p['stage']}, window {'found' if p['new'] else 'never appeared'})")
        if self.bridge: self.bridge.interval = .5
        if p and p.get('new'):
            self.present_hwnd = p['new']
            if self.ui.get('show_window'): self.windows.show()
            else: self.windows.present(p['new'])
        self.last_track = None; self.shown_title = None; self.update()

    def start_playlist(self, index=None):
        if index is not None and index != self.ui['current']:
            self.ui['current'] = index; self.save(); self.load_tracks()
        if self.ui['mode'] == 'shuffle' and self.tracks:
            self.start_queue()
        else:
            self.queue = None; self.launch(watch_url(self.playlist['id']))

    # --- shuffle loop: our own order, played by YouTube Music as a temporary list ---
    # YouTube Music moves between the songs of a list by itself and prepares the next one, so
    # song ends are near-gapless and Next/Previous are its own (~1 s). A new queue, and with it
    # one cold start, is needed only every QUEUE songs.
    def shuffle_path(self):
        return ROOT/'state'/f'shuffle-{self.playlist["id"]}.json'

    def load_tracks(self):
        self.tracks, fetched = tracklist.cached(ROOT/'state', self.playlist['id'])
        self.shuffle_state = read_json(self.shuffle_path())
        queue = self.shuffle_state.get('queue')
        # a saved queue only matters while shuffling; otherwise it would trigger a needless restart
        self.queue = queue if isinstance(queue, dict) and queue.get('ids') and self.ui['mode'] == 'shuffle' else None
        if (not self.tracks or tracklist.stale(fetched)) and not self.fetching and self.preview is None:
            self.fetching = True
            list_id = self.playlist['id']
            def work():
                tracks = tracklist.fetch(ROOT/'state', list_id)
                self.fetching = False
                if tracks and list_id == self.playlist['id']: self.tracks = tracks
            threading.Thread(target=work, daemon=True).start()

    def song_url(self, video_id, list_id=None):
        return f'https://music.youtube.com/watch?v={video_id}&list={list_id or self.playlist["id"]}'

    def save_shuffle(self):
        try:
            tmp = self.shuffle_path().with_suffix('.tmp')
            tmp.write_text(json.dumps(self.shuffle_state), encoding='utf-8'); os.replace(tmp, self.shuffle_path())
        except OSError:
            pass

    def pick_queue(self):
        ids = []
        for _ in range(min(QUEUE, len(self.tracks))):
            choice = pick_next(self.tracks, self.shuffle_state)
            if choice and choice not in ids: ids.append(choice)
        self.save_shuffle()
        return ids

    def start_queue(self):
        if self.building or self.pending or not self.tracks: return
        list_id = self.playlist['id']
        ready = self.shuffle_state.pop('ready', None)
        if tracklist.ready_usable(ready):
            self.save_shuffle()
            self.on_queue_ready(ready['ids'], ready['list'], list_id)      # prepared in the background: no wait
            return
        ids = ready['ids'] if isinstance(ready, dict) and ready.get('ids') else self.pick_queue()   # stale list: same songs
        self.save_shuffle()
        self.building = True; self.update()
        threading.Thread(target=lambda: self.queue_ready.emit(ids, tracklist.temp_list(ids) or '', list_id), daemon=True).start()

    def prepare_queue(self):
        """Keep the next shuffled queue and its temporary list ready, so Play and every 50th song
        start without the ~0.7-1.5 s the list takes to make (measured)."""
        if (self.preparing or self.preview is not None or self.ui['mode'] != 'shuffle' or not self.tracks
                or tracklist.ready_usable(self.shuffle_state.get('ready'))):
            return
        ready = self.shuffle_state.get('ready')
        ids = ready['ids'] if isinstance(ready, dict) and ready.get('ids') else self.pick_queue()
        self.shuffle_state['ready'] = {'ids': ids, 'list': '', 'made': 0}; self.save_shuffle()
        self.preparing = True
        list_id = self.playlist['id']
        threading.Thread(target=lambda: self.ready_built.emit(ids, tracklist.temp_list(ids) or '', list_id), daemon=True).start()

    def on_ready_built(self, ids, temp, list_id):
        self.preparing = False
        ready = self.shuffle_state.get('ready')
        if list_id != self.playlist['id'] or not isinstance(ready, dict) or ready.get('ids') != ids:
            return                  # used or replaced meanwhile
        self.shuffle_state['ready'] = {'ids': ids, 'list': temp, 'made': time.time() if temp else 0}
        self.save_shuffle()

    def resume_queue(self, ids):
        """Carry on with the songs left in a queue that lost its temporary list (network trouble)."""
        if not ids:
            self.start_queue(); return
        self.building = True; self.update()
        list_id = self.playlist['id']
        threading.Thread(target=lambda: self.queue_ready.emit(ids, tracklist.temp_list(ids) or '', list_id), daemon=True).start()

    def on_queue_ready(self, ids, temp, list_id):
        self.building = False
        if list_id != self.playlist['id'] or not ids: return
        titles = {t['id']: t['title'] for t in self.tracks}
        self.queue = {'ids': ids, 'titles': [titles.get(i, '') for i in ids], 'list': temp, 'index': 0}
        self.shuffle_state['queue'] = self.queue; self.save_shuffle()
        # Without a temporary list (offline YouTube redirect) each song is started on its own.
        self.launch(self.song_url(ids[0], temp) if temp else self.song_url(ids[0]))

    def track_changed(self, title):
        q = self.queue
        if not q: return
        i = match_index(title, q['titles'], q['index'])
        if i is not None:
            q['index'] = i
        elif q['list']:
            q['index'] += 1        # a song YouTube Music titles differently, or its radio after the list
        if self.ui['mode'] == 'shuffle' and q['list'] and q['index'] >= len(q['ids']):
            self.start_queue()     # the list ran out and YouTube Music moved on to its own picks

    def continue_in_order(self):
        """Leaving shuffle: carry on in playlist order after the current song."""
        q, self.queue = self.queue, None
        order = [t['id'] for t in self.tracks]
        current = q['ids'][q['index']] if q and 0 <= q['index'] < len(q['ids']) else None
        if current in order:
            self.launch(self.song_url(order[(order.index(current)+1) % len(order)]))
        else:
            self.launch(watch_url(self.playlist['id']))

    # --- end of song: repeat one rewinds; shuffle only acts when its queue runs out ---
    def keep_repeating(self):
        d = self.data
        if not d.get('ok') or self.pending or self.building:
            return
        track = (d.get('title'), d.get('artist'))
        pos, end = self.position(), self.duration()
        mode, q = self.ui['mode'], self.queue
        # Repeat-one rewinds 1.2 s early; everything else presses Next 0.5 s before the end, because
        # YouTube Music's own end-of-song change took 2.0-4.3 s (median 2.36) against ~0.45 s for Next.
        # The last 3 s are checked every 30 ms (launch_tick), so the window is not missed.
        lead = 1.2 if mode == 'one' else 0.5
        action = repeat_decision(self.last_track, track, pos, end, d.get('playing'), self.repeat_armed, lead)
        if action:
            if mode == 'one':
                self.repeat_armed = False
                if action == 'previous': self.bridge.command('previous')   # YouTube Music got there first
                else: self.seek_to(0)
            elif mode == 'shuffle' and self.tracks and action == 'restart':
                self.repeat_armed = False
                if not q or q['index'] >= len(q['ids'])-1:
                    self.start_queue(); return
                if not q['list']:          # per-song fallback: try for a list again, else the next song alone
                    self.resume_queue(q['ids'][q['index']+1:]); return
                self.bridge.command('next')
            elif mode == 'off' and action == 'restart':
                self.repeat_armed = False
                if q and self.tracks:
                    self.continue_in_order(); return
                if d.get('can_next', True): self.bridge.command('next')
        if pos < end-5:
            self.repeat_armed = True
        self.last_track = (*track, pos, end)

    def cycle_mode(self, mode=None):
        self.ui['mode'] = mode or MODES[(MODES.index(self.ui['mode'])+1) % len(MODES)]
        self.repeat_armed = True; self.save(); self.update()

    # --- actions ---
    def toggle(self):
        # Windows keeps a closed window's session listed for a moment, and drops the live one for a
        # moment at every song change: only "no app window at all" means start fresh.
        windows = self.app_windows()
        if windows and self.data.get('ok'):
            want = not self.data.get('playing')
            self.bridge.command('toggle')
            # once seen: the first command after a restart did not land; say it again, explicitly
            QTimer.singleShot(1200, lambda: self.data.get('ok') and bool(self.data.get('playing')) != want
                              and self.bridge.command('play' if want else 'pause'))
        elif windows: self.retry('toggle')
        elif not self.pending and not self.building: self.start_playlist()

    def retry(self, name):
        QTimer.singleShot(400, lambda: self.data.get('ok') and self.bridge.command(name))

    def skip(self, name):
        if self.pending or self.building: return
        q, windows = self.queue, self.app_windows()
        if name == 'next' and self.ui['mode'] == 'shuffle' and self.tracks and (
                not windows or not q or not q['list'] or q['index'] >= len(q['ids'])-1):
            self.start_queue()             # nothing open, no queue yet, or it is on its last song
        elif self.data.get('ok'):
            self.bridge.command(name)
        elif windows:
            self.retry(name)               # the session is between songs

    def seek_to(self, seconds):
        if not self.data.get('ok') or not self.data.get('can_seek', True): return
        seconds = max(0, min(self.duration(), seconds))
        self.seek_preview = (seconds, time.time()+2.5, time.time())
        self.bridge.command('seek', seconds); self.update()

    def timeline_stale(self):
        """Chrome often keeps the previous song's timeline after a change (seen: 120.8/120.8 on a
        5-minute song), so a timeline older than the song itself is not trusted."""
        return bool(self.clock['since']) and self.data.get('updated', 0) < self.clock['since']-1.0

    def known_duration(self):
        title = self.data.get('title') or ''
        if title not in self.durations:
            titles = [t['title'] for t in self.tracks]
            q, i = self.queue, None
            if q and 0 <= q['index'] < len(q['ids']):
                by_id = {t['id']: t for t in self.tracks}
                track = by_id.get(q['ids'][q['index']])
                if track and match_index(title, [track['title']]) == 0: i = titles.index(track['title'])
            if i is None: i = match_index(title, titles)
            self.durations[title] = float(self.tracks[i].get('duration') or 0) if i is not None else 0.0
        return self.durations[title]

    def duration(self):
        d = self.data
        if not d.get('ok'): return 0
        return (self.known_duration() or 0) if self.timeline_stale() else d.get('end', 0)

    def position(self):
        d = self.data
        if self.seek_preview:
            target, _, at = (*self.seek_preview, 0)[:3]
            return target + (time.time()-at if at and d.get('playing') else 0)
        if not d.get('ok'): return 0
        if self.timeline_stale():
            c = self.clock
            pos = c['pos'] + (time.time()-c['at'] if c['playing'] else 0)
            end = self.duration()
            return min(pos, end) if end else pos
        return progress(d['position'], d['end'], d['updated'], time.time(), d['playing'])

    def toggle_window(self):
        self.ui['show_window'] = not self.ui.get('show_window'); self.save()
        if self.windows:
            if self.ui['show_window']: self.windows.show()
            elif self.present_hwnd: self.windows.present(self.present_hwnd)
            else: self.windows.hide()
        self.update()

    # --- painting ---
    def paintEvent(self, event):
        d = self.data; ok = d.get('ok')
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        p.scale(self.width()/CANVAS_W, self.height()/H)
        draw_card(p, CANVAS_W, H, self.current_backdrop() if ok else None)
        self.note_icon(p, 29, 34)
        draw_text(p, (70,30,300,37), 'Mini Player', 25, bold=True)
        status = ('Retrying…' if self.pending and self.pending.get('tries') else 'Opening…') if self.pending or self.building else ('Playing' if d.get('playing') else 'Paused') if ok else 'Not playing'
        status = {'shuffle': 'Shuffle · ', 'one': 'Repeat one · '}.get(self.ui['mode'], '') + status
        draw_text(p, (29,35,496,25), status, 13, MUTED, align=RIGHT)
        # cover
        r = self.buttons['cover']; clip = QPainterPath(); clip.addRoundedRect(r, 22, 22)
        p.save(); p.setClipPath(clip)
        if ok and not self.cover.isNull():
            p.drawImage(r, self.cover)
        else:
            p.fillRect(r, QColor('#2c2c2c')); self.note_icon(p, r.center().x()-15, r.center().y()-14, QColor('#5a5a5a'))
        p.restore()
        p.setPen(QPen(QColor(255,255,255,28), 1)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawRoundedRect(r, 22, 22)
        # text
        title = d.get('title') if ok else 'Nothing playing'
        artist = d.get('artist') if ok else 'Press play · ' + self.playlist['name']
        draw_text(p, (181,86,300 if ok else 344,32), title or '—', 21, bold=True, elide=True)
        draw_text(p, (181,118,300 if ok else 344,24), artist or '', 16, MUTED, elide=True)
        # controls
        for key in ('previous', 'next'):
            enabled = ok and d.get('can_'+key, True)
            draw_circle(p, self.buttons[key], self.hover == key, enabled=enabled); self.skip_glyph(p, self.buttons[key], key == 'next', enabled)
        draw_circle(p, self.buttons['play'], self.hover == 'play', primary=True); self.play_glyph(p, self.buttons['play'], d.get('playing'))
        self.mode_button(p, self.buttons['repeat'], self.ui['mode'], self.hover == 'repeat')
        draw_circle(p, self.buttons['window'], self.hover == 'window'); self.window_glyph(p, self.buttons['window'], self.ui.get('show_window'))
        # progress
        end = self.duration() if ok else 0
        pos = self.position(); frac = pos/end if end else 0
        track = QRectF(29,236,496,10); p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor('#4b4b4d')); p.drawRoundedRect(track, 5, 5)
        if frac > 0:
            p.setBrush(BLUE); p.drawRoundedRect(QRectF(29,236,max(10,496*frac),10), 5, 5)
        if ok and (self.hover == 'seek' or self.dragging_seek):
            p.setBrush(TEXT); p.drawEllipse(QPointF(29+496*frac, 241), 8, 8)
        draw_text(p, (29,252,200,22), fmt_time(pos) if ok else '0:00', 13, MUTED)
        draw_text(p, (29,252,496,22), fmt_time(end) if ok else '0:00', 13, MUTED, align=RIGHT)
        p.end()

    def current_backdrop(self):
        dpr = 2.0 if self.preview is not None else max(1.0, self.devicePixelRatioF())
        size = (round(self.width()*dpr), round(self.height()*dpr))
        key = (self.cover.cacheKey(), size)
        if self.backdrop_key != key:
            self.backdrop = blurred_backdrop(self.cover, size[0], size[1], radius=size[0]*.09)
            self.backdrop_key = key
        return self.backdrop

    @staticmethod
    def note_icon(p, x, y, color=BLUE):
        p.save(); p.translate(x, y); p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
        p.drawEllipse(QRectF(4,16,11,9)); p.drawEllipse(QRectF(17,13,11,9))
        p.setPen(QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawLine(QPointF(13.5,20), QPointF(13.5,3)); p.drawLine(QPointF(26.5,17), QPointF(26.5,0))
        p.setPen(QPen(color, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)); p.drawLine(QPointF(13.5,3), QPointF(26.5,0))
        p.restore()

    @staticmethod
    def play_glyph(p, r, playing):
        c = r.center(); p.setBrush(TEXT)
        if playing:
            p.setPen(Qt.PenStyle.NoPen)
            for dx in (-7, 3): p.drawRoundedRect(QRectF(c.x()+dx, c.y()-10, 5, 20), 1.5, 1.5)
        else:
            tri = QPainterPath(); tri.moveTo(c.x()-6, c.y()-11); tri.lineTo(c.x()+11, c.y()); tri.lineTo(c.x()-6, c.y()+11); tri.closeSubpath()
            p.setPen(glyph_pen(width=2)); p.drawPath(tri)

    @staticmethod
    def skip_glyph(p, r, forward, enabled):
        c = r.center(); d = 1 if forward else -1
        p.setPen(glyph_pen(enabled, 1.6)); p.setBrush(TEXT if enabled else QColor('#5c5c5c'))
        tri = QPainterPath(); tri.moveTo(c.x()-5*d, c.y()-7); tri.lineTo(c.x()+4*d, c.y()); tri.lineTo(c.x()-5*d, c.y()+7); tri.closeSubpath()
        p.drawPath(tri); p.setPen(glyph_pen(enabled, 2.4)); p.drawLine(QPointF(c.x()+7*d, c.y()-7), QPointF(c.x()+7*d, c.y()+7))

    @staticmethod
    def mode_button(p, r, mode, hover):
        on = mode != 'off'
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#17445b') if on else QColor('#484848' if hover else '#383838')); p.drawEllipse(r)
        c = r.center(); color = QColor('#68c7ff') if on else TEXT
        p.setPen(QPen(color, 1.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if mode == 'shuffle':
            # two crossing paths with arrowheads
            a = QPainterPath(); a.moveTo(c.x()-10, c.y()-6); a.cubicTo(c.x()-2, c.y()-6, c.x()+1, c.y()+6, c.x()+9, c.y()+6)
            b = QPainterPath(); b.moveTo(c.x()-10, c.y()+6); b.cubicTo(c.x()-2, c.y()+6, c.x()+1, c.y()-6, c.x()+9, c.y()-6)
            p.drawPath(a); p.drawPath(b)
            for y in (-6, 6):
                p.drawLine(QPointF(c.x()+6, c.y()+y-3), QPointF(c.x()+9.5, c.y()+y)); p.drawLine(QPointF(c.x()+9.5, c.y()+y), QPointF(c.x()+6, c.y()+y+3))
            return
        loop = QPainterPath(); loop.moveTo(c.x()+5, c.y()-6); loop.lineTo(c.x()-5, c.y()-6)
        loop.arcTo(QRectF(c.x()-11, c.y()-6, 12, 12), 90, 180); loop.lineTo(c.x()+5, c.y()+6)
        loop.arcTo(QRectF(c.x()-1, c.y()-6, 12, 12), 270, 180); p.drawPath(loop)
        p.drawLine(QPointF(c.x()+1, c.y()-9.5), QPointF(c.x()+4.5, c.y()-6)); p.drawLine(QPointF(c.x()+4.5, c.y()-6), QPointF(c.x()+1, c.y()-2.5))
        if mode == 'one':
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(color); p.drawEllipse(QPointF(c.x()+12, c.y()-12), 6.5, 6.5)
            draw_text(p, (c.x()+6, c.y()-19, 12, 14), '1', 10, QColor('#10202b'), True, Qt.AlignmentFlag.AlignHCenter)

    @staticmethod
    def window_glyph(p, r, shown):
        c = r.center(); p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(glyph_pen(width=1.8))
        p.drawRoundedRect(QRectF(c.x()-9, c.y()-7, 18, 14), 3, 3); p.drawLine(QPointF(c.x()-9, c.y()-3), QPointF(c.x()+9, c.y()-3))
        if not shown:
            p.setPen(QPen(QColor('#ff8a80'), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)); p.drawLine(QPointF(c.x()-11, c.y()+9), QPointF(c.x()+11, c.y()-9))

    # --- input ---
    def local(self, pos):
        return QPointF(pos.x()*CANVAS_W/self.width(), pos.y()*H/self.height())

    def hit(self, pos):
        point = self.local(pos)
        return next((k for k, r in self.buttons.items() if r.contains(point)), None)

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton: return
        self.pressed = key = self.hit(e.position())
        point = self.local(e.position())
        if key == 'seek' and self.data.get('ok'):
            self.dragging_seek = True
        elif key is None and not self.ui.get('position_locked', False):
            self.offset = e.globalPosition().toPoint()-self.pos()

    def mouseMoveEvent(self, e):
        point = self.local(e.position())
        if self.offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint()-self.offset); return
        if self.dragging_seek:
            end = self.duration(); self.seek_preview = (max(0, min(1, (point.x()-29)/496))*end, time.time()+60); self.update(); return
        key = self.hit(e.position())
        if key != self.hover:
            self.hover = key; self.update()
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor if key and key != 'cover' else Qt.CursorShape.ArrowCursor))
            self.setToolTip({'window': 'Show / hide YouTube Music', 'repeat': 'Mode: off → shuffle loop → repeat one (R)',
                             'cover': 'Double-click: show / hide YouTube Music'}.get(key, ''))

    def leaveEvent(self, e):
        if self.hover: self.hover = None; self.update()

    def mouseReleaseEvent(self, e):
        key = self.hit(e.position())
        if self.dragging_seek:
            self.dragging_seek = False
            self.seek_to(max(0, min(1, (self.local(e.position()).x()-29)/496))*self.duration())
        elif self.pressed and key == self.pressed:
            {'play': self.toggle, 'previous': lambda: self.skip('previous'), 'next': lambda: self.skip('next'),
             'window': self.toggle_window, 'repeat': self.cycle_mode}.get(key, lambda: None)()
        elif self.offset is not None:
            clamp_to_screen(self); self.save()
        self.pressed = self.offset = None

    def mouseDoubleClickEvent(self, e):
        if self.hit(e.position()) == 'cover': self.toggle_window()

    def keyPressEvent(self, e):
        k = Qt.Key
        actions = {k.Key_Space: self.toggle, k.Key_N: lambda: self.skip('next'), k.Key_P: lambda: self.skip('previous'),
                   k.Key_Right: lambda: self.seek_to(self.position()+10), k.Key_Left: lambda: self.seek_to(self.position()-10),
                   k.Key_V: self.toggle_window, k.Key_R: self.cycle_mode}
        if e.key() in actions: actions[e.key()]()
        else: super().keyPressEvent(e)

    # --- menus ---
    def playlist_menu(self, parent=None):
        menu = QMenu(parent or self); menu.setStyleSheet(MENU_STYLE)
        for i, item in enumerate(self.ui['playlists']):
            act = menu.addAction(item['name'], lambda i=i: self.start_playlist(i))
            act.setCheckable(True); act.setChecked(i == self.ui['current'])
        menu.addSeparator()
        menu.addAction('Add playlist from clipboard…', self.add_playlist)
        remove = menu.addAction('Remove this playlist', self.remove_playlist); remove.setEnabled(len(self.ui['playlists']) > 1)
        return menu

    def add_playlist(self):
        pid = playlist_id(QApplication.clipboard().text())
        if not pid:
            QToolTip.showText(QCursor.pos(), 'Copy a YouTube Music playlist link first'); return
        if any(p['id'] == pid for p in self.ui['playlists']): return
        name, ok = QInputDialog.getText(self, 'New playlist', 'Name:', text=f'Playlist {len(self.ui["playlists"])+1}')
        if ok and name.strip():
            self.ui['playlists'].append({'name': name.strip()[:60], 'id': pid}); self.save(); self.update()

    def remove_playlist(self):
        if len(self.ui['playlists']) > 1:
            self.ui['playlists'].pop(self.ui['current']); self.ui['current'] = 0; self.save(); self.update()

    def toggle_position_lock(self):
        self.ui['position_locked'] = not self.ui.get('position_locked', False)
        self.offset = None
        self.save()

    def contextMenuEvent(self, e):
        menu = QMenu(self); menu.setStyleSheet(MENU_STYLE); ok = self.data.get('ok')
        menu.addAction(('Pause' if self.data.get('playing') else 'Play') + '\tSpace', self.toggle)
        menu.addAction('Next\tN', lambda: self.skip('next')).setEnabled(bool(ok))
        menu.addAction('Previous\tP', lambda: self.skip('previous')).setEnabled(bool(ok))
        menu.addSeparator()
        sub = self.playlist_menu(menu); sub.setTitle('Playlists'); menu.addMenu(sub)
        vol = menu.addMenu('Volume (Chrome)'); level = chrome_volume.get()
        for pct in (100, 80, 60, 40, 20):
            act = vol.addAction(f'{pct}%', lambda v=pct/100: chrome_volume.set(v)); act.setCheckable(True)
            act.setChecked(level is not None and abs(level-pct/100) < .1)
        modes = menu.addMenu('Mode')
        for key, label in (('off', 'In order'), ('shuffle', 'Shuffle loop (never in order)'), ('one', 'Repeat this song')):
            act = modes.addAction(label, lambda k=key: self.cycle_mode(k)); act.setCheckable(True); act.setChecked(self.ui['mode'] == key)
        show = menu.addAction('Show YouTube Music\tV', self.toggle_window); show.setCheckable(True); show.setChecked(bool(self.ui.get('show_window')))
        brk = menu.addAction('Pause music during Focus breaks', self.toggle_break_pause); brk.setCheckable(True); brk.setChecked(bool(self.ui.get('pause_on_break')))
        lock = menu.addAction('Lock position', self.toggle_position_lock); lock.setCheckable(True)
        lock.setChecked(bool(self.ui.get('position_locked', False)))
        sizes = menu.addMenu('Size')
        usage = read_json(USAGE_WINDOW).get('scale')
        match = usage if isinstance(usage, (int, float)) and .4 <= usage <= 1.5 else .65
        for name, scale in [('Small', .65), (f'Match AI limit card ({round(CANVAS_W*match)} px)', match),
                            ('Medium', .82), ('Large', 1.0), ('Extra large', 1.2)]:
            act = sizes.addAction(name, lambda s=scale: self.set_size(s)); act.setCheckable(True)
            act.setChecked(abs(scale-self.scale) < .01 and not any(x.isChecked() for x in sizes.actions()))
        top = menu.addAction('Always on top', self.toggle_top); top.setCheckable(True)
        top.setChecked(bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
        menu.addSeparator(); menu.addAction('Quit', self.close)
        menu.exec(e.globalPos())

    def toggle_break_pause(self):
        self.ui['pause_on_break'] = not self.ui.get('pause_on_break'); self.focus_mode = None; self.save()

    def set_size(self, scale):
        self.scale = scale; self.setFixedSize(round(CANVAS_W*scale), round(H*scale)); clamp_to_screen(self); self.save(); self.update()

    def toggle_top(self):
        on = not bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on); self.show(); self.save()

    def closeEvent(self, e):
        self.save()
        if self.windows:       # no unmanaged invisible topmost window may outlive the widget
            held = self.windows.foreground() in self.app_windows()
            for hwnd in self.app_windows():
                if self.windows.is_present(hwnd): self.windows.normal(hwnd)
            if held: self.windows.give_focus(self.user_fg)
        if self.bridge: self.bridge.stop(); self.bridge.wait(1500)
        e.accept()


def single_instance():
    """A Windows named mutex: the OS releases it when the process ends. The old lock file stayed
    behind a killed widget and, once its process id was reused, kept every new start away."""
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, r'Local\GurOS-MiniPlayer')
    return handle if handle and ctypes.get_last_error() != 183 else None   # 183: already exists


def main():
    preview = sys.argv[sys.argv.index('--preview')+1:] if '--preview' in sys.argv else None
    if preview is not None: os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    app = QApplication(sys.argv[:1]); app.setApplicationName('Mini Player')
    for name in ('segoeui.ttf', 'seguisb.ttf', 'seguiemj.ttf'):
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/'+name)
    (ROOT/'state').mkdir(exist_ok=True)
    instance = single_instance() if preview is None else True
    if not instance: return            # another Mini Player is running
    widget = Player(preview)
    if preview is not None:
        # --preview out.png [cover.png]: render a sample song, or the empty state without a cover.
        if len(preview) > 1:
            widget.receive({'ok': True, 'title': 'Adventure of a Lifetime', 'artist': 'Coldplay', 'playing': True,
                            'position': 141, 'end': 264, 'updated': 0, 'cover': Path(preview[1]).read_bytes()})
            widget.hover = 'seek'; widget.ui['mode'] = preview[2] if len(preview) > 2 else 'shuffle'
        img = QImage(widget.width()*2, widget.height()*2, QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(2); img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img); widget.render(painter, QPoint()); painter.end()
        assert img.save(preview[0]); return
    app.aboutToQuit.connect(widget.save)
    target = widget.pos()
    widget.show()
    QTimer.singleShot(500, lambda: (widget.move(target), clamp_to_screen(widget), widget.save()))
    app.exec()


if __name__ == '__main__':
    main()
