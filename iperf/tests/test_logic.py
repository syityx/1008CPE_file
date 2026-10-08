import copy
import unittest

from iperf.common.feedback import LossFeedback
from iperf.common.runner import client_command, load_config, parse_result, validate
from iperf.common.runtime import ROOT


class LogicTests(unittest.TestCase):
    def test_feedback_moves_away_from_loss_and_stays_bounded(self):
        for loss0, loss1, expected in ((0, 25, 0.9), (25, 0, 0.1)):
            controller = LossFeedback(warmup_epochs=0)
            for _ in range(20):
                controller.update({"lost_percent": loss0}, {"lost_percent": loss1})
            self.assertAlmostEqual(controller.ratio, expected)

    def test_warmup_and_equal_quality_hold_ratio(self):
        controller = LossFeedback(warmup_epochs=2)
        for _ in range(2):
            self.assertEqual(controller.update({"lost_percent": 0}, {"lost_percent": 25})["next_lan_ratio"], 0.5)
        self.assertGreater(controller.update({"lost_percent": 0}, {"lost_percent": 25})["next_lan_ratio"], 0.5)
        held = controller.ratio
        self.assertEqual(controller.update({"lost_percent": 10}, {"lost_percent": 10})["next_lan_ratio"], held)

    def test_instance_state_not_shared(self):
        first, second = LossFeedback(0), LossFeedback(0)
        first.update({"lost_percent": 0}, {"lost_percent": 40})
        self.assertEqual(second.ratio, 0.5)
        self.assertEqual(second.core.erro_pre, 0)

    def test_command_is_real_reverse_udp_and_rate_limited(self):
        cmd = client_command("iperf3", dict(sender_ip="192.0.2.1", port=5226, bind_ip="192.0.2.2"),
                             1000000, 5, 1200, 5000)
        for flag in ("-u", "-R", "-J", "-B", "-b"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-b") + 1], "1000000")

    def test_invalid_ports_lengths_and_rates(self):
        with self.assertRaises(ValueError):
            validate({})
        config = load_config("fixed", ROOT / "fixed" / "config.json")
        for key, value in (("total_bps", 2000001), ("duration_seconds", 0), ("datagram_bytes", 1500000)):
            bad = copy.deepcopy(config)
            bad[key] = value
            with self.assertRaises(ValueError):
                validate(bad)
        config["cpe"]["port"] = config["lan"]["port"]
        with self.assertRaises(ValueError):
            validate(config)

    def test_missing_or_sender_statistics_cannot_be_feedback(self):
        for data in ({"error": "unable to connect"}, {},
                     {"start": {"test_start": {"protocol": "UDP", "reverse": 1}},
                      "end": {"sum_received": {"sender": True}}}):
            with self.assertRaises(ValueError):
                parse_result(data)


if __name__ == "__main__":
    unittest.main()
