"""Find, hide, show and launch the YouTube Music Chrome app window (Win32, no focus stealing)."""
import ctypes
import subprocess
from ctypes import wintypes

CHROME_PROXY = r'C:\Program Files\Google\Chrome\Application\chrome_proxy.exe'
APP_ID = 'cinhimbnkkaeohfgghhklpknlkffjgod'
SW_HIDE, SW_SHOWNOACTIVATE, WM_CLOSE = 0, 4, 0x0010
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80000, 0x20, 0x80, 0x40000
STEALTH = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
PRESENT_SIZE = (520, 420)   # small: Chrome paints less, and it sits in a corner nobody looks at


def is_ytm_window(class_name, title):
    """The installed PWA window; a normal browser tab ends with '- Google Chrome'."""
    return (class_name.startswith('Chrome_WidgetWin') and 'YouTube Music' in title
            and not title.endswith('Google Chrome'))


def _user32():
    u = ctypes.WinDLL('user32', use_last_error=True)
    u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u.FindWindowExW.restype = wintypes.HWND
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetShellWindow.restype = wintypes.HWND
    for name, args in [('GetWindowTextLengthW', [wintypes.HWND]), ('IsWindowVisible', [wintypes.HWND]),
                       ('GetWindowTextW', [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
                       ('GetClassNameW', [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
                       ('ShowWindow', [wintypes.HWND, ctypes.c_int]),
                       ('FindWindowExW', [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]),
                       ('PostMessageW', [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
                       ('GetWindowLongPtrW', [wintypes.HWND, ctypes.c_int]),
                       ('SetWindowLongPtrW', [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]),
                       ('SetLayeredWindowAttributes', [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]),
                       ('SetWindowPos', [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]),
                       ('GetForegroundWindow', []), ('SetForegroundWindow', [wintypes.HWND]),
                       ('GetWindowThreadProcessId', [wintypes.HWND, ctypes.c_void_p]),
                       ('AttachThreadInput', [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]),
                       ('GetShellWindow', []), ('IsWindow', [wintypes.HWND]),
                       ('GetWindowRect', [wintypes.HWND, ctypes.c_void_p]),
                       ('SystemParametersInfoW', [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT])]:
        getattr(u, name).argtypes = args
    return u


class YTMWindows:
    def __init__(self):
        self.u = _user32()
        self.rects = {}        # hwnd -> the window's own size, put back before it is shown or closed
        self.debug = ''
        self.known = set()     # app windows seen with a YouTube Music title, kept while they exist

    def find(self):
        """[(hwnd, title, visible)] for every YouTube Music app window, hidden ones included."""
        found = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            length = self.u.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length+1)
            if length: self.u.GetWindowTextW(hwnd, title, length+1)
            if length or int(hwnd) in self.known:
                cls = ctypes.create_unicode_buffer(256); self.u.GetClassNameW(hwnd, cls, 256)
                # Once recognised, a window stays recognised: between songs Chrome briefly shows
                # another title, and "no app window" there started a needless new queue (seen live).
                if is_ytm_window(cls.value, title.value) or (int(hwnd) in self.known and cls.value.startswith('Chrome_WidgetWin')):
                    found.append((int(hwnd), title.value, bool(self.u.IsWindowVisible(hwnd))))
            return True
        callback = proto(visit)
        self.u.EnumWindows(callback, 0)
        self.known = {h for h, _, _ in found}
        return found

    def hide(self, keep=()):
        hidden = 0
        for hwnd, _, visible in self.find():
            if visible and hwnd not in keep:
                self.u.ShowWindow(hwnd, SW_HIDE); hidden += 1
        return hidden

    def show(self):
        for hwnd, _, _ in self.find():
            self.normal(hwnd)
            self.u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    def close(self, handles):
        for hwnd in handles:
            self.normal(hwnd)      # Chrome remembers the size a window closes with
            self.u.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    @staticmethod
    def launch(url):
        """Open `url` in the installed app. With the app already open Chrome adds a new window."""
        subprocess.Popen([CHROME_PROXY, '--profile-directory=Default', f'--app-id={APP_ID}',
                          f'--app-launch-url-for-shortcuts-menu-item={url}'],
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)

    def send_key(self, char, title_hint=''):
        """Type one YouTube Music shortcut (s = shuffle, r = repeat) into the app, hidden or not.

        SMTC has no shuffle/repeat for Chrome, so the player uses the page's own shortcuts.
        Goes to the window showing the current song when several are open.
        """
        windows = self.find()
        if not windows:
            return False
        hwnd = next((h for h, t, _ in windows if title_hint and title_hint in t), windows[0][0])
        target = self.u.FindWindowExW(hwnd, None, 'Chrome_RenderWidgetHostHWND', None) or hwnd
        vk = ord(char.upper())
        for message, wparam in ((WM_KEYDOWN, vk), (WM_CHAR, ord(char)), (WM_KEYUP, vk)):
            self.u.PostMessageW(target, message, wparam, 0)
        return True

    def stealth(self, hwnd, on=True):
        """Chrome defers the first playback in a window that was never shown. A shown but fully
        transparent, click-through, taskbar-less window counts as shown; the user sees nothing."""
        style = self.u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if on:
            self.u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (style | STEALTH) & ~WS_EX_APPWINDOW)
            self.u.SetLayeredWindowAttributes(hwnd, 0, 1, 2)        # LWA_ALPHA 1/255: invisible, yet Chrome still paints it (0 can leave the page blank)
            self.u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        else:
            self.u.ShowWindow(hwnd, SW_HIDE)
            self.u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style & ~STEALTH)

    def present(self, hwnd):
        """Keep the playing window visible to Chrome yet invisible to the user: 1/255 opacity,
        click-through, off the taskbar, small, topmost in a corner. Chrome throttles hidden and
        covered pages; song changes there took 1-4.2 s, in a visible page 1-2.1 s (measured)."""
        if hwnd not in self.rects:
            rect = wintypes.RECT()
            if self.u.GetWindowRect(hwnd, ctypes.byref(rect)):
                self.rects[hwnd] = (rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top)
        self.stealth(hwnd, True)
        area = wintypes.RECT(); self.u.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)   # SPI_GETWORKAREA
        w, h = PRESENT_SIZE
        self.u.SetWindowPos(hwnd, HWND_TOPMOST, area.right-w, area.bottom-h, w, h, SWP_NOACTIVATE)

    def normal(self, hwnd):
        """Undo present(): own size back, not topmost, normal styles, hidden."""
        rect = self.rects.pop(hwnd, None)
        style = self.u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if style & STEALTH:
            self.u.ShowWindow(hwnd, SW_HIDE)
            self.u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style & ~STEALTH)
        if rect:
            self.u.SetWindowPos(hwnd, HWND_NOTOPMOST, *rect, SWP_NOACTIVATE)
        else:
            self.u.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def on_top(self, hwnd):
        """Topmost without moving or resizing: a starting window behind a maximised app counts as
        covered, and Chrome holds a covered page's first paint. Resizing it this early broke the
        first load instead (measured), so size and place are left alone until it plays."""
        self.u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def nudge(self, hwnd):
        """Resize by a few pixels: Chrome rebuilds the page's drawing surface, which can wake a
        window whose first page stayed blank."""
        rect = wintypes.RECT()
        if self.u.GetWindowRect(hwnd, ctypes.byref(rect)):
            w, h = rect.right-rect.left, rect.bottom-rect.top
            self.u.SetWindowPos(hwnd, 0, 0, 0, w+(-8 if w > 400 else 8), h, SWP_NOMOVE | SWP_NOACTIVATE | 0x0004)

    def is_present(self, hwnd):
        return bool(self.u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT)

    def foreground(self):
        return int(self.u.GetForegroundWindow() or 0)

    # Shell surfaces the user may have clicked last; Windows refuses to hand focus to them.
    SHELL_CLASSES = ('Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'NotifyIconOverflowWindow',
                     'TopLevelWindowForOverflowXamlIsland', 'Windows.UI.Core.CoreWindow', 'XamlExplorerHostIslandWindow')

    def focusable(self, hwnd):
        if not hwnd or not self.u.IsWindow(hwnd) or not self.u.IsWindowVisible(hwnd):
            return False
        cls = ctypes.create_unicode_buffer(128); self.u.GetClassNameW(hwnd, cls, 128)
        return cls.value not in self.SHELL_CLASSES

    def give_focus(self, hwnd):
        """Run the hand-back on a fresh thread without windows. Attaching the widget's own GUI
        thread to Chrome's input and detaching again undid the switch (measured: it failed from
        inside the widget, worked from a windowless thread)."""
        import threading
        result = []
        def work():
            msg = wintypes.MSG()
            ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # give the thread a message queue
            result.append(self._give_focus(hwnd))
        worker = threading.Thread(target=work, daemon=True)
        worker.start(); worker.join(0.5)
        return bool(result and result[0])

    def _give_focus(self, hwnd):
        """Chrome activates every window it opens; hand the keyboard back to where the user was,
        or to the desktop when that is not possible. True when an app window no longer has it.

        SetForegroundWindow is refused to background processes unless their input is attached
        to the current foreground thread, so attach for the call.
        """
        current = self.foreground()
        for target in (hwnd if self.focusable(hwnd) else None, int(self.u.GetShellWindow() or 0)):
            if not target or target == current:
                continue
            mine = ctypes.windll.kernel32.GetCurrentThreadId()
            theirs = self.u.GetWindowThreadProcessId(current, None)
            attached = bool(theirs) and bool(self.u.AttachThreadInput(mine, theirs, True))
            try:
                # Windows lets the process that sent the last input move the foreground. Chrome took it
                # by opening a window; a zero-pixel mouse move hands that right to us without any effect
                # on screen (attaching input alone was refused: set=0 in the log).
                ctypes.windll.user32.mouse_event(0x0001, 0, 0, 0, 0)
                done = self.u.SetForegroundWindow(target)
                self.debug = f'target={target} attached={attached} set={done} err={ctypes.get_last_error()} fg_after={self.foreground()}'
            finally:
                if attached:
                    self.u.AttachThreadInput(mine, theirs, False)
            if self.foreground() != current:
                return True
        return self.foreground() != current

    def reload(self, hwnd):
        """Press the app window's own Reload button: Chrome's toolbar is visible to UI Automation
        even when the page is not. Fixes a first navigation that stayed blank."""
        try:
            import uiautomation as auto
            button = auto.ControlFromHandle(hwnd).ButtonControl(searchDepth=12, Name='Reload')
            if button.Exists(1):
                button.GetInvokePattern().Invoke()
                return True
        except Exception:
            pass
        return False
