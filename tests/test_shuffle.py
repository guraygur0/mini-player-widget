import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from shuffle import pick_next, cooldown  # noqa: E402
from playlist import parse, stale, list_from_location, match_index, ready_usable  # noqa: E402
import datetime


def playlist(n, artists=None):
    return [{'id': f't{i}', 'artist': (artists or ['a', 'b', 'c', 'd', 'e'])[i % len(artists or 'abcde')]} for i in range(n)]


class Shuffle(unittest.TestCase):
    def test_round_plays_every_track_once(self):
        tracks, state, rng = playlist(40), {}, random.Random(1)
        for _ in range(3):
            chosen = [pick_next(tracks, state, rng) for _ in range(40)]
            self.assertEqual(sorted(chosen), sorted(t['id'] for t in tracks))

    def test_not_in_order(self):
        tracks, state = playlist(50), {}
        chosen = [pick_next(tracks, state, random.Random(7)) for _ in range(50)]
        self.assertNotEqual(chosen, [t['id'] for t in tracks])
        ascending = sum(1 for a, b in zip(chosen, chosen[1:]) if int(b[1:]) == int(a[1:])+1)
        self.assertLess(ascending, 8)

    def test_no_quick_repeat_across_rounds(self):
        tracks, state, rng = playlist(30), {}, random.Random(3)
        history = [pick_next(tracks, state, rng) for _ in range(300)]
        gap = cooldown(30)
        for i, track in enumerate(history):
            self.assertNotIn(track, history[max(0, i-gap):i], f'{track} came back within {gap} picks')

    def test_same_artist_rarely_back_to_back(self):
        tracks = playlist(60, ['x', 'y', 'z'])
        artist = {t['id']: t['artist'] for t in tracks}
        state, rng = {}, random.Random(5)
        history = [pick_next(tracks, state, rng) for _ in range(600)]
        same = sum(1 for a, b in zip(history, history[1:]) if artist[a] == artist[b])
        self.assertLess(same/len(history), 0.15)   # uniform picking would give about 1/3

    def test_two_rounds_differ(self):
        tracks, state, rng = playlist(25), {}, random.Random(11)
        first = [pick_next(tracks, state, rng) for _ in range(25)]
        second = [pick_next(tracks, state, rng) for _ in range(25)]
        self.assertNotEqual(first, second)

    def test_removed_tracks_drop_out_and_tiny_playlists_work(self):
        state = {'bag': ['gone', 't1'], 'recent': ['gone']}
        self.assertEqual(pick_next(playlist(2), state, random.Random(0)) in ('t0', 't1'), True)
        self.assertNotIn('gone', state['bag'] + state['recent'])
        self.assertEqual(pick_next([{'id': 'solo'}], {}, random.Random(0)), 'solo')
        self.assertIsNone(pick_next([], {}))

    def test_parse_drops_unavailable(self):
        data = {'entries': [{'id': 'a', 'title': 'Song', 'channel': 'Band', 'duration': 200},
                            {'id': 'b', 'title': None}, None, {'id': 'c', 'title': 'X', 'uploader': 'U'}]}
        self.assertEqual([(t['id'], t['artist']) for t in parse(data)], [('a', 'Band'), ('c', 'U')])
        self.assertTrue(stale(0, now=13*3600)); self.assertFalse(stale(0, now=3600))


class Queue(unittest.TestCase):
    def test_list_from_location(self):
        loc = 'https://www.youtube.com/watch?v=7jEIWg6zH_s&list=TLGGgI_r1FEes4oyODA5MjAyNg'
        self.assertEqual(list_from_location(loc), 'TLGGgI_r1FEes4oyODA5MjAyNg')
        self.assertIsNone(list_from_location('https://www.youtube.com/watch?v=x&list=PLabc'))
        self.assertIsNone(list_from_location(None))
        self.assertIsNone(list_from_location('https://www.youtube.com/watch?list=TLGG"bad'))

    def test_match_index(self):
        titles = ['Karabiberim', 'Oğuzhan Koç - Heyecandan (Official Video)', 'One Dance (feat. Wizkid & Kyla)',
                  'Heyecandan (Akustik)', 'Waves (Robin Schulz Remix)']
        self.assertEqual(match_index('Heyecandan', titles), 1)
        self.assertEqual(match_index('Heyecandan', titles, start=2), 3)    # forward from the current spot first
        self.assertEqual(match_index('One Dance - Drake (Official Audio)', titles), None)  # different wording
        self.assertEqual(match_index('One Dance', titles), 2)
        self.assertIsNone(match_index('ab', titles))
        self.assertIsNone(match_index('Something else', titles))


    def test_ready_usable(self):
        noon = datetime.datetime(2026, 9, 29, 12, 0).timestamp()
        ready = {'ids': ['a', 'b'], 'list': 'TLGGxyz', 'made': noon}
        self.assertTrue(ready_usable(ready, noon + 3600))
        self.assertFalse(ready_usable(ready, noon + 13*3600))                      # too old
        self.assertFalse(ready_usable(dict(ready, made=noon-13*3600+1), noon))    # made yesterday
        self.assertFalse(ready_usable(dict(ready, list=''), noon))                # no list (offline when prepared)
        self.assertFalse(ready_usable(dict(ready, ids=[]), noon))
        self.assertFalse(ready_usable(None, noon))


if __name__ == '__main__':
    unittest.main()
