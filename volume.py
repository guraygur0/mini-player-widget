"""Volume of Chrome's audio sessions (the PWA plays through Chrome's shared audio process).

This is Chrome-wide: a video in a normal Chrome tab follows the same level.
"""


def _sessions():
    try:
        from pycaw.pycaw import AudioUtilities
        return [s for s in AudioUtilities.GetAllSessions()
                if s.Process and s.Process.name().lower() == 'chrome.exe']
    except Exception:
        return []


def get():
    """0..1, or None when Chrome has no audio session yet."""
    levels = [s.SimpleAudioVolume.GetMasterVolume() for s in _sessions()]
    return max(levels) if levels else None


def set(level):
    level = max(0.0, min(1.0, level))
    done = False
    for session in _sessions():
        try:
            session.SimpleAudioVolume.SetMasterVolume(level, None); done = True
        except Exception:
            pass
    return done
