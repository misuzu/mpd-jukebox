import logging
import unittest
from dataclasses import replace

from mpd_jukebox import jukebox as core
from mpd_jukebox.jukebox import (
    CancelTimer,
    Connected,
    DatabaseChanged,
    Enqueue,
    Enqueued,
    Log,
    Persist,
    Play,
    PlayerChanged,
    QueueChanged,
    RecordPlay,
    RefillDone,
    RefillFailed,
    Restart,
    SetTimer,
    StartRefill,
    State,
    TimerFired,
    Validate,
    Validated,
    update,
)
from mpd_jukebox.model import Config, Entry, Mode, PlayState, Status

NOW = 1000
CFG = Config(queue_length=2)
PLAYING = Status(
    PlayState.PLAY,
    queue_length=2,
    song_id=1,
    song_uri='cur.mp3',
    elapsed=30,
    duration=200,
    crossfade=5,
)
STOPPED = Status(PlayState.STOP, queue_length=0)


def entries(n, prefix='s'):
    return tuple(Entry(f'{prefix}{i}', i, f'v{i}') for i in range(n))


def of_type(effects, kind):
    return [e for e in effects if isinstance(e, kind)]


class HelpersTestCase(unittest.TestCase):
    def test_add_count(self):
        self.assertIsNone(core.add_count(CFG, 3))
        self.assertEqual(core.add_count(CFG, 2), 1)
        self.assertEqual(core.add_count(CFG, 0), 2)
        big = Config(queue_length=500)
        self.assertEqual(core.add_count(big, 0), 99)

    def test_needs_topup(self):
        self.assertTrue(core.needs_topup(Config(), 9))
        self.assertFalse(core.needs_topup(Config(), 10))
        album = Config(mode=Mode.ALBUM)
        self.assertTrue(core.needs_topup(album, 4))
        self.assertFalse(core.needs_topup(album, 5))

    def test_queue_size(self):
        self.assertEqual(core.queue_size(Config(), Mode.SONG), 100)
        self.assertEqual(core.queue_size(Config(), Mode.ALBUM), 25)

    def test_jukebox_timer(self):
        state = State()
        self.assertEqual(
            core.jukebox_timer(CFG, state, PLAYING),
            SetTimer(core.JUKEBOX_TIMER, 200 - (30 + 5 + 10)),
        )
        near_end = replace(PLAYING, elapsed=190)
        self.assertEqual(
            core.jukebox_timer(CFG, state, near_end),
            CancelTimer(core.JUKEBOX_TIMER),
        )
        self.assertEqual(
            core.jukebox_timer(CFG, state, STOPPED),
            CancelTimer(core.JUKEBOX_TIMER),
        )
        self.assertEqual(
            core.jukebox_timer(CFG, replace(state, halted=True), PLAYING),
            CancelTimer(core.JUKEBOX_TIMER),
        )

    def test_scrobble_timer(self):
        self.assertEqual(
            core.scrobble_timer(PLAYING), SetTimer(core.SCROBBLE_TIMER, 70)
        )
        long_song = replace(PLAYING, duration=600, elapsed=40)
        self.assertEqual(
            core.scrobble_timer(long_song), SetTimer(core.SCROBBLE_TIMER, 200)
        )
        past = replace(PLAYING, elapsed=150)
        self.assertEqual(
            core.scrobble_timer(past), CancelTimer(core.SCROBBLE_TIMER)
        )
        self.assertEqual(
            core.scrobble_timer(STOPPED), CancelTimer(core.SCROBBLE_TIMER)
        )


class RunTestCase(unittest.TestCase):
    def test_connect_with_empty_queue_refills(self):
        state, effects = update(CFG, State(), Connected(STOPPED, NOW))
        self.assertTrue(state.filling)
        self.assertEqual(
            of_type(effects, StartRefill),
            [StartRefill(Mode.SONG, (), 102, 2)],
        )

    def test_connect_with_long_enough_queue_does_nothing(self):
        state, effects = update(CFG, State(), Connected(PLAYING, NOW))
        self.assertFalse(state.filling)
        self.assertEqual(of_type(effects, StartRefill), [])
        self.assertEqual(of_type(effects, Enqueue), [])
        self.assertEqual(len(of_type(effects, SetTimer)), 2)

    def test_connect_resets_work_and_validates(self):
        state = State(
            filling=True,
            enqueuing=True,
            validating=True,
            jukebox_queue=entries(20),
        )
        state, effects = update(CFG, state, Connected(STOPPED, NOW))
        self.assertFalse(state.filling)
        self.assertEqual(of_type(effects, Enqueue)[0].entries, entries(2))
        # The rest is checked against the database.
        self.assertEqual(
            of_type(effects, Validate), [Validate(Mode.SONG, entries(20)[2:])]
        )
        self.assertTrue(state.validating)

    def test_queue_change_moves_entries(self):
        state = State(jukebox_queue=entries(20))
        short = replace(PLAYING, queue_length=1)
        state, effects = update(CFG, state, QueueChanged(short, NOW))
        self.assertEqual(state.jukebox_queue, entries(20)[1:])
        self.assertIn(Enqueue(Mode.SONG, entries(1), 1), effects)
        self.assertIn(Persist(entries(20)[1:]), effects)

    def test_queue_change_at_target_does_not_run(self):
        state = State(jukebox_queue=entries(20))
        state, effects = update(CFG, state, QueueChanged(PLAYING, NOW))
        self.assertEqual(of_type(effects, Enqueue), [])

    def test_player_change_never_runs(self):
        state = State(jukebox_queue=entries(20))
        state, effects = update(CFG, state, PlayerChanged(STOPPED, NOW))
        self.assertEqual(of_type(effects, Enqueue), [])
        self.assertEqual(state.status, STOPPED)

    def test_timer_adds_one_at_target(self):
        state = State(jukebox_queue=entries(20), status=PLAYING)
        event = TimerFired(core.JUKEBOX_TIMER, PLAYING, NOW)
        state, effects = update(CFG, state, event)
        self.assertIn(Enqueue(Mode.SONG, entries(1), 1), effects)

    def test_timer_above_target_does_nothing(self):
        long_queue = replace(PLAYING, queue_length=3)
        state = State(jukebox_queue=entries(20))
        event = TimerFired(core.JUKEBOX_TIMER, long_queue, NOW)
        state, effects = update(CFG, state, event)
        self.assertEqual(of_type(effects, Enqueue), [])

    def test_filling_enqueuing_and_halted_block_runs(self):
        for state in (
            State(filling=True),
            State(enqueuing=True, jukebox_queue=entries(20)),
            State(halted=True),
        ):
            new, effects = update(CFG, state, QueueChanged(STOPPED, NOW))
            self.assertEqual(of_type(effects, StartRefill), [])
            self.assertEqual(of_type(effects, Enqueue), [])
            self.assertEqual(new.filling, state.filling)

    def test_album_mode_uses_album_sizes(self):
        cfg = replace(CFG, mode=Mode.ALBUM)
        _, effects = update(cfg, State(), Connected(STOPPED, NOW))
        self.assertEqual(
            of_type(effects, StartRefill),
            [StartRefill(Mode.ALBUM, (), 27, 2)],
        )


class RefillTestCase(unittest.TestCase):
    def test_done_with_add_enqueues(self):
        state = State(filling=True)
        state, effects = update(CFG, state, RefillDone(entries(102), 2, NOW))
        self.assertFalse(state.filling)
        self.assertEqual(state.jukebox_queue, entries(102)[2:])
        self.assertEqual(
            effects,
            (Enqueue(Mode.SONG, entries(2), 2), Persist(entries(102)[2:])),
        )

    def test_done_without_add_persists(self):
        state, effects = update(
            CFG, State(filling=True), RefillDone(entries(100), 0, NOW)
        )
        self.assertEqual(state.jukebox_queue, entries(100))
        self.assertEqual(effects, (Persist(entries(100)),))

    def test_failure_halts(self):
        state, effects = update(
            CFG, State(filling=True), RefillFailed('too few', NOW)
        )
        self.assertTrue(state.halted)
        self.assertFalse(state.filling)
        self.assertIn(CancelTimer(core.JUKEBOX_TIMER), effects)
        (log,) = of_type(effects, Log)
        self.assertEqual(log.level, logging.ERROR)
        self.assertIn('too few', log.message)
        # Nothing runs while halted, and the timer stays off.
        state, effects = update(CFG, state, QueueChanged(STOPPED, NOW))
        self.assertEqual(of_type(effects, StartRefill), [])
        _, effects = update(CFG, state, PlayerChanged(PLAYING, NOW))
        self.assertIn(CancelTimer(core.JUKEBOX_TIMER), effects)

    def test_restart_resumes(self):
        state = State(
            halted=True,
            jukebox_queue=entries(5),
            status=STOPPED,
        )
        state, effects = update(CFG, state, Restart(NOW))
        self.assertFalse(state.halted)
        self.assertEqual(effects[0], Persist(()))
        self.assertEqual(
            of_type(effects, StartRefill),
            [StartRefill(Mode.SONG, (), 102, 2)],
        )

    def test_restart_before_status(self):
        state, effects = update(CFG, State(halted=True), Restart(NOW))
        self.assertFalse(state.halted)
        self.assertEqual(effects, (Persist(()),))


def enqueued(attempted, added, wanted, status):
    return Enqueued(attempted, added, wanted, status, NOW)


class EnqueuedTestCase(unittest.TestCase):
    def test_starts_playback(self):
        state = State(jukebox_queue=entries(50), enqueuing=True)
        state, effects = update(CFG, state, enqueued(2, 2, 2, STOPPED))
        self.assertIn(Play(), effects)
        self.assertFalse(state.enqueuing)

    def test_already_playing(self):
        state = State(jukebox_queue=entries(50))
        _, effects = update(CFG, state, enqueued(1, 1, 1, PLAYING))
        self.assertNotIn(Play(), effects)

    def test_failed_entries_are_replaced_by_the_next(self):
        state = State(jukebox_queue=entries(5), enqueuing=True)
        state, effects = update(CFG, state, enqueued(2, 1, 2, STOPPED))
        self.assertIn(Play(), effects)
        self.assertEqual(
            of_type(effects, Enqueue), [Enqueue(Mode.SONG, entries(1), 1)]
        )
        self.assertIn(Persist(entries(5)[1:]), effects)
        self.assertTrue(state.enqueuing)
        self.assertEqual(state.add_failures, 0)

    def test_all_failed_keeps_going_without_playing(self):
        state = State(jukebox_queue=entries(5))
        state, effects = update(CFG, state, enqueued(2, 0, 2, STOPPED))
        self.assertNotIn(Play(), effects)
        self.assertEqual(of_type(effects, Enqueue)[0].entries, entries(2))
        self.assertEqual(state.add_failures, 2)

    def test_empty_jukebox_queue_refills_the_rest(self):
        state = State()
        state, effects = update(CFG, state, enqueued(2, 1, 2, PLAYING))
        self.assertEqual(
            of_type(effects, StartRefill), [StartRefill(Mode.SONG, (), 101, 1)]
        )
        self.assertTrue(state.filling)
        self.assertFalse(state.enqueuing)

    def test_no_retry_while_filling(self):
        state = State(jukebox_queue=entries(5), filling=True)
        _, effects = update(CFG, state, enqueued(1, 0, 1, PLAYING))
        self.assertEqual(of_type(effects, Enqueue), [])
        self.assertEqual(of_type(effects, StartRefill), [])

    def test_endless_failures_halt(self):
        cfg = replace(CFG, queue_length_song=10)
        state = State(jukebox_queue=entries(50), add_failures=8)
        state, effects = update(cfg, state, enqueued(2, 0, 2, STOPPED))
        self.assertTrue(state.halted)
        self.assertIn('10 times in a row', of_type(effects, Log)[0].message)
        self.assertEqual(of_type(effects, Enqueue), [])
        self.assertIn(CancelTimer(core.JUKEBOX_TIMER), effects)
        # A restart starts counting again.
        state, _ = update(cfg, state, Restart(NOW))
        self.assertEqual(state.add_failures, 0)

    def test_success_resets_failures(self):
        state = State(jukebox_queue=entries(50), add_failures=5)
        state, _ = update(CFG, state, enqueued(1, 1, 1, PLAYING))
        self.assertEqual(state.add_failures, 0)

    def test_topup_when_short(self):
        state = State(jukebox_queue=entries(9))
        state, effects = update(CFG, state, enqueued(1, 1, 1, PLAYING))
        self.assertTrue(state.filling)
        self.assertEqual(
            of_type(effects, StartRefill),
            [StartRefill(Mode.SONG, entries(9), 100, 0)],
        )

    def test_no_topup_while_filling(self):
        state = State(jukebox_queue=entries(1), filling=True)
        _, effects = update(CFG, state, enqueued(1, 1, 1, PLAYING))
        self.assertEqual(of_type(effects, StartRefill), [])


class ValidationTestCase(unittest.TestCase):
    def test_database_change_checks_the_queue(self):
        state = State(jukebox_queue=entries(20))
        state, effects = update(CFG, state, DatabaseChanged(NOW))
        self.assertEqual(effects, (Validate(Mode.SONG, entries(20)),))
        self.assertTrue(state.validating)

    def test_nothing_to_check(self):
        state, effects = update(CFG, State(), DatabaseChanged(NOW))
        self.assertEqual(effects, ())
        self.assertFalse(state.validating)

    def test_missing_entries_are_dropped(self):
        state = State(jukebox_queue=entries(20), validating=True)
        missing = frozenset({'s3', 's5', 'gone'})
        state, effects = update(CFG, state, Validated(missing, NOW))
        kept = tuple(e for e in entries(20) if e.key not in missing)
        self.assertEqual(state.jukebox_queue, kept)
        self.assertFalse(state.validating)
        self.assertIn(Persist(kept), effects)
        self.assertIn(
            'Dropped 2 picked entries', of_type(effects, Log)[0].message
        )

    def test_nothing_missing(self):
        state = State(jukebox_queue=entries(20), validating=True)
        state, effects = update(CFG, state, Validated(frozenset(), NOW))
        self.assertEqual(effects, ())

    def test_dropping_below_minimum_tops_up(self):
        state = State(jukebox_queue=entries(11), validating=True)
        state, effects = update(
            CFG, state, Validated(frozenset({'s0', 's1'}), NOW)
        )
        self.assertTrue(state.filling)
        self.assertEqual(len(of_type(effects, StartRefill)), 1)

    def test_change_during_a_check_checks_again(self):
        state = State(jukebox_queue=entries(20), validating=True)
        state, effects = update(CFG, state, DatabaseChanged(NOW))
        self.assertEqual(effects, ())
        self.assertTrue(state.revalidate)
        state, effects = update(CFG, state, Validated(frozenset({'s0'}), NOW))
        self.assertEqual(
            of_type(effects, Validate), [Validate(Mode.SONG, entries(20)[1:])]
        )
        self.assertTrue(state.validating)
        self.assertFalse(state.revalidate)

    def test_album_mode_waits_for_a_quiet_database(self):
        cfg = replace(CFG, mode=Mode.ALBUM, album_check_delay=30)
        state = State(jukebox_queue=entries(20))
        state, effects = update(cfg, state, DatabaseChanged(NOW))
        self.assertEqual(effects, (SetTimer(core.VALIDATE_TIMER, 30),))
        self.assertFalse(state.validating)
        # Another change pushes the timer back (SetTimer replaces it).
        _, effects = update(cfg, state, DatabaseChanged(NOW + 5))
        self.assertEqual(effects, (SetTimer(core.VALIDATE_TIMER, 30),))
        event = TimerFired(core.VALIDATE_TIMER, PLAYING, NOW + 35)
        state, effects = update(cfg, state, event)
        self.assertEqual(
            of_type(effects, Validate), [Validate(Mode.ALBUM, entries(20))]
        )
        self.assertTrue(state.validating)

    def test_change_during_a_refill_checks_its_picks(self):
        state = State(filling=True)
        state, effects = update(CFG, state, DatabaseChanged(NOW))
        self.assertEqual(effects, ())
        self.assertTrue(state.revalidate)
        state, effects = update(CFG, state, RefillDone(entries(102), 2, NOW))
        self.assertEqual(
            of_type(effects, Validate),
            [Validate(Mode.SONG, entries(102)[2:])],
        )


class ScrobbleTestCase(unittest.TestCase):
    def test_records_play(self):
        event = TimerFired(core.SCROBBLE_TIMER, PLAYING, NOW)
        _, effects = update(CFG, State(), event)
        self.assertIn(RecordPlay('cur.mp3', NOW, NOW - 30), effects)

    def test_skips_streams_and_stopped(self):
        stream = replace(PLAYING, song_uri='http://radio/stream')
        for status in (stream, STOPPED):
            event = TimerFired(core.SCROBBLE_TIMER, status, NOW)
            _, effects = update(CFG, State(), event)
            self.assertEqual(of_type(effects, RecordPlay), [])
