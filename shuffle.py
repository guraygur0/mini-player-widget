"""The widget's own shuffle for "playlist loop": never in order, never a repeat within a round.

Rules, applied in this order:
1. A round plays every track exactly once (a "bag"); a new round refills the bag.
2. The most recent tracks (about a third of the playlist, at most 60) are held back even
   across the round boundary, so a song never comes straight back.
3. Weighted pick among what is left: rarely played tracks weigh more, the artist that just
   played weighs much less, and a random jitter keeps two rounds from ever lining up.
"""
import random


def cooldown(count):
    return max(1, min(60, count//3))


def pick_next(tracks, state, rng=random):
    """Choose the next track id and update `state` in place.

    tracks: [{'id', 'artist'}]; state: {'bag': [...], 'recent': [...], 'plays': {id: n}}.
    """
    ids = [t['id'] for t in tracks]
    if not ids:
        return None
    known = set(ids)
    artist = {t['id']: (t.get('artist') or '').lower() for t in tracks}
    bag = [i for i in state.get('bag', []) if i in known]
    if not bag:
        bag = list(ids)
    recent = [i for i in state.get('recent', []) if i in known]
    held = set(recent[-cooldown(len(ids)):])
    choices = [i for i in bag if i not in held] or [i for i in ids if i not in held] or bag
    plays = state.get('plays', {})
    last_artist = artist.get(recent[-1]) if recent else None
    weights = []
    for i in choices:
        w = 1.0/(1+plays.get(i, 0))**0.7
        if last_artist and artist.get(i) == last_artist:
            w *= 0.12
        weights.append(w*rng.uniform(0.6, 1.4))
    choice = rng.choices(choices, weights=weights, k=1)[0]
    if choice in bag:
        bag.remove(choice)
    state['bag'] = bag
    state['recent'] = (recent + [choice])[-200:]
    state['plays'] = dict(plays, **{choice: plays.get(choice, 0)+1})
    return choice
