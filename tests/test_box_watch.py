"""'Live' must reflect a box that actually answers, not just that we're sending."""
import tempfile
import time
import unittest
from pathlib import Path

from scenebuilder.node import ArtNetController
from scenebuilder.server import App
from scenebuilder.simulator import demo_box
from scenebuilder.store import Store, new_box, new_float


class BoxWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.ctl = ArtNetController(port=0).start()
        self.app = App(self.store, self.ctl)
        self.app.engine.BOX_CHECK_S = 0.3
        self.app.engine.BOX_LOST_S = 1.5
        self.sim = demo_box(port=0).start()

    def tearDown(self):
        self.app.engine.set_active(None)
        self.sim.stop()
        self.ctl.stop()
        self.store.close()
        self.tmp.cleanup()

    def _float(self, port):
        fl = new_float(name="Test")
        b = new_box(ip="127.0.0.1", udp_port=port)
        fl["boxes"] = [b]
        self.store.put_float(fl)
        return fl

    def _wait_state(self, want, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            st = [b["state"] for b in self.app.engine.status()["boxes"]]
            if st and all(x == want for x in st):
                return True
            time.sleep(0.1)
        return False

    def test_answering_box_is_ok(self):
        fl = self._float(self.sim.port)
        self.app.engine.set_active(fl["id"])
        self.assertTrue(self._wait_state("ok"), self.app.engine.status()["boxes"])

    def test_unplugged_box_goes_down_while_still_sending(self):
        fl = self._float(self.sim.port)
        self.app.engine.set_active(fl["id"])
        self.assertTrue(self._wait_state("ok"))
        self.sim.stop()                       # cable pulled
        self.assertTrue(self._wait_state("down"), self.app.engine.status()["boxes"])
        self.assertTrue(self.app.engine.status()["output"])  # still sending, but not claiming the box is there

    def test_never_answering_box_reads_checking_then_down(self):
        fl = self._float(9)  # nothing listens here
        self.app.engine.set_active(fl["id"])
        self.assertEqual(self.app.engine.status()["boxes"][0]["state"], "checking")
        self.assertTrue(self._wait_state("down"))


if __name__ == "__main__":
    unittest.main()
