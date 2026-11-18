import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from trainboard.config import Config
from trainboard.runs import Runs, series
from trainboard.scheduler import Scheduler
from trainboard.server import Handler, state_lock

DEMO = Path(__file__).resolve().parents[1] / "examples/train_demo.py"


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="trainboard test ")
        self.root = Path(self.temp.name)
        self.file = self.root / "panel.json"
        self.raw = dict(entries={"demo": dict(script=str(DEMO), args=["--run-dir", "{run_dir}"],
                                             parameters={"epochs": dict(type="int", default=2, min=1),
                                                         "delay": dict(type="float", default=.1, min=0)})})
        self.file.write_text(json.dumps(self.raw), encoding="utf8")
        self.cfg = Config(self.file)
        self.queue = None

    def tearDown(self):
        if self.queue:
            self.queue.close()
        self.temp.cleanup()

    def wait(self, condition, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.05)
        self.fail("Timed out")

    def test_arguments_and_spaces(self):
        _, opts = self.cfg.task({"entry": "demo", "epochs": 3})
        argv, cwd, env = self.cfg.command(dict(entry="demo", id="abc", run="run with spaces", options=opts))
        self.assertEqual(argv[0], sys.executable)
        self.assertIn(str(self.cfg.runs / "run with spaces"), argv)
        self.assertEqual(cwd, self.root)
        self.assertEqual(env["TRAINBOARD_JOB_ID"], "abc")

    def test_reject_invalid_parameters(self):
        for task in ({"entry": "bad"}, {"entry": "demo", "epochs": 1.5},
                     {"entry": "demo", "epochs": -1}, {"entry": "demo", "delay": float("nan")},
                     {"entry": "demo", "unknown": 1}):
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.cfg.task(task)

    def test_partial_jsonl_and_metric_aliases(self):
        p = self.cfg.runs / "history"
        p.mkdir()
        (p / "metrics.jsonl").write_text('{"epoch":1,"macro_auc":0.81,"val_loss":0.3}\n{"epoch":2,', encoding="utf8")
        self.assertEqual(series(p), [{"epoch": 1., "auc": .81, "valid_loss": .3}])

    def test_minimum_metric(self):
        self.raw.update(primary_metric="valid_loss", metric_mode="min")
        self.file.write_text(json.dumps(self.raw))
        cfg = Config(self.file)
        p = cfg.runs / "history"
        p.mkdir()
        (p / "history.csv").write_text("epoch,val_loss\n1,0.4\n2,0.2\n")
        r = Runs(cfg).describe(p)
        self.assertEqual((r["auc"], r["best_epoch"]), (.2, 2))

    def test_path_boundary(self):
        with self.assertRaises(ValueError):
            Runs(self.cfg).path("../")

    def test_serial_queue_and_metrics(self):
        self.queue = Scheduler(self.cfg)
        first = self.queue.add({"entry": "demo"})
        second = self.queue.add({"entry": "demo"})
        self.queue.switch(True)
        self.wait(lambda: all(j["state"] == "completed" for j in self.queue.state()["jobs"]))
        a, b = self.queue.state()["jobs"]
        self.assertEqual([a["id"], b["id"]], [first, second])
        self.assertLessEqual(a["ended"], b["started"])
        self.assertEqual(len(Runs(self.cfg).list()), 2)
        self.assertIn('"epoch": 2', self.queue.log_tail(second))

    def test_reorder_and_persist(self):
        self.queue = Scheduler(self.cfg)
        a = self.queue.add({"entry": "demo"})
        b = self.queue.add({"entry": "demo"})
        self.queue.queued_action(b, "up")
        self.queue.queued_action(a, "remove")
        self.assertEqual([j["id"] for j in self.queue.state()["jobs"]], [b])
        self.queue.close()
        self.queue = Scheduler(self.cfg)
        self.assertFalse(self.queue.enabled)
        self.assertEqual(self.queue.state()["jobs"][0]["id"], b)

    def test_failure_pauses(self):
        script = self.root / "failure.py"
        script.write_text("raise SystemExit(3)")
        self.cfg.entries["demo"]["script"] = str(script)
        self.queue = Scheduler(self.cfg)
        self.queue.add({"entry": "demo"})
        self.queue.add({"entry": "demo"})
        self.queue.switch(True)
        self.wait(lambda: self.queue.state()["jobs"][0]["state"] == "failed")
        self.assertFalse(self.queue.enabled)
        self.assertEqual(self.queue.state()["jobs"][1]["state"], "queued")

    def test_stop_pauses_next(self):
        self.queue = Scheduler(self.cfg)
        a = self.queue.add({"entry": "demo", "delay": 5})
        self.queue.add({"entry": "demo"})
        self.queue.switch(True)
        self.wait(lambda: self.queue.proc is not None)
        self.queue.stop(a)
        self.wait(lambda: self.queue.state()["jobs"][0]["state"] == "stopped")
        self.assertEqual(self.queue.state()["jobs"][1]["state"], "queued")

    def test_http_roles_and_origins(self):
        self.queue = Scheduler(self.cfg)
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.cfg, server.runs, server.queue = self.cfg, Runs(self.cfg), self.queue
            server.admin_token, server.view_token = "a" * 32, "v" * 32
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            def request(route, token="", body=None, origin=None):
                headers = {"X-Panel-Token": token, "Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                req = urllib.request.Request(url + route, headers=headers,
                                             data=json.dumps(body).encode() if body else None)
                return urllib.request.urlopen(req)
            try:
                with request("/api/catalog", server.view_token) as response:
                    self.assertTrue(json.load(response)["readonly"])
                for route, token, body, origin in [
                    ("/api/state", "", None, None),
                    ("/api/source?id=demo", server.view_token, None, None),
                    ("/api/control", server.view_token, {"action": "add"}, None),
                    ("/api/control", server.admin_token, {"action": "pause"}, "http://evil.example")]:
                    with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as e:
                        request(route, token, body, origin)
                    self.assertEqual(e.exception.code, 403)
                with request("/api/control", server.admin_token,
                             {"action": "add", "task": {"entry": "demo"}}) as response:
                    self.assertTrue(json.load(response)["ok"])
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
