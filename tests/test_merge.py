"""Import merges another computer's floats without ever deleting or silently overwriting."""
import copy
import tempfile
import unittest
from pathlib import Path

from scenebuilder.store import Store, new_float, new_fixture


def make(code, name, n=2):
    fl = new_float(code, name)
    for i in range(n):
        fl["fixtures"].append(new_fixture(label="%s-%d" % (code, i), variant="TW", box_id=fl["boxes"][0]["id"]))
    return fl


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.a, self.b = make("A", "Alpha"), make("B", "Bravo")
        self.store.put_float(self.a)
        self.store.put_float(self.b)
        self.theirs = {"floats": copy.deepcopy(self.store.data["floats"])}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ids(self):
        return [f["id"] for f in self.store.data["floats"]]

    def test_identical_is_noop(self):
        plan = self.store.plan_merge(self.theirs)
        self.assertEqual((len(plan["new"]), len(plan["conflicts"]), len(plan["identical"])), (0, 0, 2))

    def test_new_float_added_and_local_only_kept(self):
        c = make("C", "Charlie")
        incoming = {"floats": [c]}                     # their file lacks A and B entirely
        r = self.store.apply_merge(incoming, {})
        self.assertEqual(r, {"added": 1, "replaced": 0})
        self.assertEqual(len(self.ids()), 3)             # A and B still here

    def test_conflict_defaults_to_mine(self):
        self.theirs["floats"][0]["fixtures"][0]["address"] = 99
        plan = self.store.plan_merge(self.theirs)
        self.assertEqual(len(plan["conflicts"]), 1)
        r = self.store.apply_merge(self.theirs, {})       # no choice made
        self.assertEqual(r["replaced"], 0)
        self.assertIsNone(self.store.get_float(self.a["id"])["fixtures"][0]["address"])

    def test_conflict_theirs_replaces_only_that_float(self):
        self.theirs["floats"][0]["fixtures"][0]["address"] = 99
        self.store.apply_merge(self.theirs, {self.a["id"]: "theirs"})
        self.assertEqual(self.store.get_float(self.a["id"])["fixtures"][0]["address"], 99)
        self.assertEqual(self.store.get_float(self.b["id"])["name"], "Bravo")

    def test_bad_file_rejected(self):
        with self.assertRaises(ValueError):
            self.store.plan_merge({"nope": 1})
        with self.assertRaises(ValueError):
            self.store.apply_merge({"floats": [{"id": "x", "boxes": [], "fixtures": [{"variant": "ZZ"}], "looks": []}]}, {})
        self.assertEqual(len(self.ids()), 2)


if __name__ == "__main__":
    unittest.main()


class StampAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_edit_stamps_float_and_merge_ignores_stamps(self):
        fl = make("A", "Alpha")
        self.store.put_float(fl)
        self.assertGreater(fl["updated"], 0)
        self.assertTrue(fl["edited_on"])
        twin = copy.deepcopy(fl)
        twin["updated"] = 1  # same content, different stamp: not a conflict
        plan = self.store.plan_merge({"floats": [twin]})
        self.assertEqual(len(plan["identical"]), 1)

    def test_plan_reports_which_copy_is_newer(self):
        fl = make("A", "Alpha")
        self.store.put_float(fl)
        theirs = copy.deepcopy(fl)
        theirs["fixtures"][0]["address"] = 7
        theirs["updated"] = fl["updated"] + 100
        c = self.store.plan_merge({"floats": [theirs]})["conflicts"][0]
        self.assertGreater(c["theirs"]["updated"], c["mine"]["updated"])

    def test_backup_copy_written_and_settings_not_in_project(self):
        mirror = Path(self.tmp.name) / "mirror"
        mirror.mkdir()
        self.store.save_settings(mirror_dir=str(mirror), network_interface="10.0.0.5")
        self.store.put_float(make("A", "Alpha"))
        self.store.flush()
        files = list(mirror.glob("DMX Scene Builder - *.json"))
        self.assertEqual(len(files), 1)
        import json as _j
        data = _j.loads(files[0].read_text())
        self.assertEqual(len(data["floats"]), 1)
        self.assertNotIn("network_interface", data)   # per-computer settings never travel with the project

    def test_missing_backup_folder_reports_error_not_crash(self):
        self.store.save_settings(mirror_dir=str(Path(self.tmp.name) / "nope"))
        self.store.put_float(make("A", "Alpha"))
        self.store.flush()
        self.assertIn("not found", self.store.mirror_error or "")
        self.assertTrue(self.store.path.exists())      # main save still happened
