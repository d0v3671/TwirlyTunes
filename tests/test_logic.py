#!/usr/bin/env python3
"""Offline unit tests for voting and critical motor STOP cleanup."""
import unittest
from unittest.mock import Mock, patch
import numpy as np
from types import SimpleNamespace
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import main as app


class VoteTests(unittest.TestCase):
    def test_requires_ten_complete_windows(self):
        v = app.Votes(10, 6, 0.8)
        for i in range(9):
            self.assertFalse(v.add(.95))
        self.assertTrue(v.add(.95))

    def test_five_out_of_ten_does_not_trigger(self):
        v = app.Votes(10, 6, 0.8)
        hits = [0.9, 0.1] * 5
        self.assertFalse(any(v.add(x) for x in hits))
        self.assertEqual(v.positives, 5)

    def test_six_interspersed_positives_triggers(self):
        v = app.Votes(10, 6, .8)
        scores = [.85,.12,.91,.16,.82,.88,.05,.96,.11,.93]
        values = [v.add(x) for x in scores]
        self.assertFalse(any(values[:-1]))
        self.assertTrue(values[-1])
        self.assertEqual(v.positives, 6)

    def test_rolling_buffer_discards_old_scores(self):
        v = app.Votes(10, 6, .8)
        for x in [.05] * 5 + [.95] * 5:
            v.add(x)
        self.assertEqual(v.positives, 5)
        self.assertTrue(v.add(.91))
        self.assertEqual(v.count, 10)
        self.assertEqual(v.positives, 6)

    def test_equal_threshold_counts(self):
        v = app.Votes(10, 6, .8)
        for x in [.8]*9:
            self.assertFalse(v.add(x))
        self.assertTrue(v.add(.8))

    def test_unusable_prediction_rejected(self):
        v = app.Votes(10, 6, .8)
        for bad in [float('nan'), float('inf'), -.1, 1.1]:
            with self.assertRaises(ValueError):
                v.add(bad)


class CleanupTests(unittest.TestCase):
    def controller_with_mocks(self):
        s = {
            "lullaby_file": "lullaby.wav",
            "motor_keepalive_seconds": .5
        }
        c = app.Controller(s, {}, real_actions=True)
        c.motor = Mock()
        c.player = Mock()
        return c

    def test_motor_stop_if_player_start_fails(self):
        c = self.controller_with_mocks()
        c.player.play.side_effect = RuntimeError('Simulated ALSA failure')
        with self.assertRaisesRegex(RuntimeError, 'Simulated ALSA'):
            c.soothe(.5)
        self.assertEqual(c.motor.send.call_args.args[0], 'CRY')
        c.motor.stop.assert_called_once()
        c.player.stop.assert_called_once()

    def test_motor_stop_if_heartbeat_fails(self):
        c = self.controller_with_mocks()
        c.settings['motor_keepalive_seconds'] = .2
        c.player.exit_status.return_value = None
        c.motor.send.side_effect = [None, RuntimeError('Simulated USB failure')]
        with self.assertRaisesRegex(RuntimeError, 'Simulated USB'):
            c.soothe(.4)
        c.motor.stop.assert_called_once()
        c.player.stop.assert_called_once()

    def test_live_capture_voting_closes_mic_at_trigger(self):
        # Simulates a real stream with 2 chunks per window and ten scored windows.
        # Confirms that mixed positives trigger and microphone context has exited.
        scores = [.9, .2, .93, .1, .88, .91, .15, .92, .11, .96]
        flags = {"closed": False, "reads": 0}

        class FakeMic:
            def __init__(self, settings):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                flags["closed"] = True
            def read_step(self, deadline=None):
                flags["reads"] += 1
                return np.ones(8000, dtype=np.float32) * .01

        settings = {
            "vote_window": 10, "minimum_cry_votes": 6,
            "cry_probability_threshold": .8, "step_seconds": 1.0,
            "minimum_rms": 0.0, "log_every_n_windows": 100
        }
        cfg = {"window_seconds": 2, "sample_rate": 8000}
        controller = app.Controller(settings, cfg, real_actions=False)
        controller.classifier = Mock()
        controller.classifier.score.side_effect = scores
        with patch.object(app, "Microphone", FakeMic):
            fired = controller.listen_until_trigger()
        self.assertTrue(fired)
        self.assertTrue(flags["closed"])
        self.assertEqual(flags["reads"], 11)   # 1 priming chunk, then 10 predictions
        self.assertEqual(controller.total_windows, 10)

    def test_generated_lullaby_valid_wave(self):
        import wave
        path = Path(__file__).resolve().parent.parent / 'audio' / 'lullaby.wav'
        with wave.open(str(path), 'rb') as file:
            self.assertIn(file.getnchannels(), (1,2))
            self.assertGreaterEqual(file.getnframes() / file.getframerate(), 20)


if __name__ == '__main__':
    unittest.main(verbosity=2)
