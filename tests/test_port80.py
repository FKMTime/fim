import json
import threading
import unittest
from unittest import mock

import fim.docker as docker
import fim.port80 as port80
import fim.workers as workers


def ps_output(*states):
    return "\n".join(json.dumps({"Name": f"c{i}", "State": s, "Status": s}) for i, s in enumerate(states))


class FakeServer:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def serve_forever(self):
        pass

    def shutdown(self):
        pass

    def server_close(self):
        self.closed = True


class Port80TestCase(unittest.TestCase):
    def setUp(self):
        port80._port80_server = None
        port80._port80_running = False
        port80._port80_suspended = 0
        patches = [
            mock.patch.object(port80, "IS_ROOT", True),
            mock.patch.object(port80, "ThreadingHTTPServer", FakeServer),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        port80._port80_server = None
        port80._port80_running = False
        port80._port80_suspended = 0


class InstanceUpTests(unittest.TestCase):
    def check(self, states):
        with mock.patch.object(docker, "get_instances", return_value={"alpha": "/tmp/alpha"}), \
             mock.patch.object(docker, "run_cmd", return_value=(0, ps_output(*states))):
            return docker.any_instance_up(), docker.compose_status("alpha")[0]

    def test_restarting_container_counts_as_up(self):
        up, all_running = self.check(["running", "restarting", "running"])
        self.assertTrue(up)
        self.assertFalse(all_running)

    def test_all_exited_is_not_up(self):
        self.assertEqual(self.check(["exited", "exited"]), (False, False))

    def test_no_containers_is_not_up(self):
        self.assertEqual(self.check([]), (False, False))

    def test_compose_array_output(self):
        out = json.dumps([{"Name": "c0", "State": "exited"}, {"Name": "c1", "State": "running"}])
        with mock.patch.object(docker, "get_instances", return_value={"alpha": "/tmp/alpha"}), \
             mock.patch.object(docker, "run_cmd", return_value=(0, out)):
            self.assertTrue(docker.any_instance_up())


class UpdatePort80Tests(Port80TestCase):
    def test_holds_port_when_nothing_up(self):
        with mock.patch.object(port80, "any_instance_up", return_value=False):
            port80.update_port80()
        self.assertIsNotNone(port80._port80_server)

    def test_releases_port_when_instance_up(self):
        with mock.patch.object(port80, "any_instance_up", return_value=False):
            port80.update_port80()
        srv = port80._port80_server
        with mock.patch.object(port80, "any_instance_up", return_value=True):
            port80.update_port80()
        self.assertIsNone(port80._port80_server)
        self.assertTrue(srv.closed)

    def test_restarting_container_releases_port(self):
        with mock.patch.object(docker, "get_instances", return_value={"alpha": "/tmp/alpha"}), \
             mock.patch.object(docker, "run_cmd", return_value=(0, ps_output("running", "restarting"))):
            port80.update_port80()
        self.assertIsNone(port80._port80_server)

    def test_bind_failure_is_logged_not_raised(self):
        with mock.patch.object(port80, "ThreadingHTTPServer", side_effect=OSError("address already in use")), \
             mock.patch.object(port80, "any_instance_up", return_value=False), \
             mock.patch("builtins.print") as printed:
            port80.update_port80()
        self.assertIsNone(port80._port80_server)
        self.assertIn("address already in use", printed.call_args[0][0])

    def test_monitor_does_not_retake_port_while_released(self):
        with mock.patch.object(port80, "any_instance_up", return_value=False):
            port80.update_port80()
            with port80.port80_released():
                self.assertIsNone(port80._port80_server)
                port80.update_port80()
                self.assertIsNone(port80._port80_server)
            port80.update_port80()
        self.assertIsNotNone(port80._port80_server)


class ComposeUpReleasesPortTests(Port80TestCase):
    def setUp(self):
        super().setUp()
        with mock.patch.object(port80, "any_instance_up", return_value=False):
            port80.update_port80()
        self.assertIsNotNone(port80._port80_server)
        self.calls = []
        self.lock = threading.Lock()
        for p in [
            mock.patch.object(workers, "action_lock", self.lock),
            mock.patch.object(workers, "get_instances",
                              return_value={"alpha": "/tmp/alpha", "beta": "/tmp/beta"}),
            mock.patch.object(workers, "set_selected"),
            mock.patch.object(workers, "run_compose_live", side_effect=self.fake_compose),
            mock.patch.object(port80, "any_instance_up", return_value=False),
            mock.patch("fim.workers.progress_reset"),
            mock.patch("fim.workers.progress_stage"),
            mock.patch("fim.workers.progress_done"),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def fake_compose(self, args, cwd=None, stage_idx=0):
        # Simulate the monitor ticking while compose runs.
        port80.update_port80()
        self.calls.append((args[2], cwd, port80._port80_server is None))
        return 0, ""

    def test_start_action_releases_port(self):
        with mock.patch.object(workers, "get_selected", return_value="alpha"):
            workers.do_action_async("start", "alpha")
        self.assertEqual(self.calls, [("up", "/tmp/alpha", True)])

    def test_start_selected_instance_releases_port(self):
        with mock.patch.object(workers, "get_selected", return_value="alpha"):
            workers.do_switch_to("alpha")
        self.assertEqual(self.calls, [("up", "/tmp/alpha", True)])

    def test_switch_releases_port_before_up(self):
        with mock.patch.object(workers, "get_selected", return_value="alpha"):
            workers.do_switch_to("beta")
        self.assertEqual([c[:2] for c in self.calls], [("down", "/tmp/alpha"), ("up", "/tmp/beta")])
        self.assertTrue(self.calls[1][2])

    def test_pull_does_not_release_port(self):
        with mock.patch.object(workers, "get_selected", return_value="alpha"):
            workers.do_action_async("pull", "alpha")
        self.assertEqual(self.calls, [("pull", "/tmp/alpha", False)])


if __name__ == "__main__":
    unittest.main()
