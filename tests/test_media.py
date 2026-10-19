import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from media import pick_session, progress, fmt_time, playlist_id, watch_url, focus_action, repeat_decision  # noqa: E402
from ytm import is_ytm_window  # noqa: E402


class Media(unittest.TestCase):
    def test_pick_session_prefers_pwa(self):
        ids = ['Spotify.exe', 'Chrome', 'Chrome._crx_cinhimbnkkghhklpknlkffjgod']
        self.assertEqual(pick_session(ids), 2)
        self.assertIsNone(pick_session(['Chrome', None]))
        pwa = 'Chrome._crx_cinhimbnkkghhklpknlkffjgod'
        self.assertEqual(pick_session([pwa, 'x', pwa], [False, False, True]), 2)   # the playing window wins
        self.assertEqual(pick_session([pwa, pwa], [False, False]), 0)

    def test_progress_extrapolates_only_while_playing(self):
        self.assertEqual(progress(10, 200, 1000, 1005, True), 15)
        self.assertEqual(progress(10, 200, 1000, 1005, False), 10)
        self.assertEqual(progress(190, 200, 1000, 1100, True), 200)
        self.assertEqual(progress(5, 0, 0, 1000, True), 5)  # no timestamp -> no guess

    def test_fmt_time(self):
        self.assertEqual([fmt_time(s) for s in (0, 61.9, 264, 3725)], ['0:00', '1:01', '4:24', '1:02:05'])

    def test_playlist_id_drops_tracking(self):
        url = 'https://music.youtube.com/playlist?list=PL_TEST_PLAYLIST&si=aUWSHPrxijCEZRM5'
        self.assertEqual(playlist_id(url), 'PL_TEST_PLAYLIST')
        self.assertEqual(watch_url('PLx'), 'https://music.youtube.com/watch?list=PLx')
        for bad in ('https://evil.example/playlist?list=PLx', 'not a url', 'https://music.youtube.com/watch?v=abc',
                    'https://music.youtube.com/playlist?list=PL"x'):
            self.assertIsNone(playlist_id(bad), bad)

    def test_focus_action(self):
        self.assertEqual(focus_action('focus', 'short'), 'pause')
        self.assertEqual(focus_action('focus', 'long'), 'pause')
        self.assertEqual(focus_action('long', 'focus'), 'resume')
        self.assertIsNone(focus_action('focus', 'focus'))
        self.assertIsNone(focus_action(None, 'short'))  # first reading is not a transition

    def test_repeat_decision(self):
        song = ('Judas', 'Lady Gaga')
        self.assertEqual(repeat_decision(None, song, 257.2, 258, True, True), 'restart')
        self.assertIsNone(repeat_decision(None, song, 257.2, 258, True, False))   # already restarted
        self.assertIsNone(repeat_decision(None, song, 257.2, 258, False, True))   # paused: leave it
        self.assertIsNone(repeat_decision(None, song, 100, 258, True, True))
        self.assertIsNone(repeat_decision(None, song, 257.2, 258, True, True, lead=0.45))  # preloaded: wait longer
        self.assertEqual(repeat_decision(None, song, 257.6, 258, True, True, lead=0.45), 'restart')
        # YouTube Music got to the next song before the widget could rewind
        self.assertEqual(repeat_decision(('Judas', 'Lady Gaga', 256.5, 258), ('Next', 'X'), 0.3, 200, True, True), 'previous')
        # the user skipped mid-song: no bounce back
        self.assertIsNone(repeat_decision(('Judas', 'Lady Gaga', 80, 258), ('Next', 'X'), 0.3, 200, True, True))

    def test_ytm_window_filter(self):
        self.assertTrue(is_ytm_window('Chrome_WidgetWin_1', "YouTube Music - Example playlist"))
        self.assertTrue(is_ytm_window('Chrome_WidgetWin_1', 'YouTube Music'))
        self.assertFalse(is_ytm_window('Chrome_WidgetWin_1', 'YouTube Music - Google Chrome'))  # a normal browser tab
        self.assertFalse(is_ytm_window('Notepad', 'YouTube Music'))
        self.assertFalse(is_ytm_window('Chrome_WidgetWin_1', 'YouTube'))


if __name__ == '__main__':
    unittest.main()
