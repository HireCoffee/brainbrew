from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo import create

spec = importlib.util.spec_from_file_location("brainbrew", Path(__file__).parents[1] / "tools/brainbrew.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class Publication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog, self.content = create(self.root / "demo")
        self.output = self.root / "site"
        self.now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)

    def build(self):
        return engine.stage(self.catalog, self.content, self.output, self.now)

    def edit(self, fn):
        data = yaml.safe_load(self.catalog.read_text())
        fn(data["courses"][0])
        self.catalog.write_text(yaml.safe_dump(data), encoding="utf-8")

    def test_release_boundary_and_timezones(self):
        session = {"starts_at": "2026-10-28T13:00:00+01:00", "ready": True}
        self.assertFalse(engine.released(session, datetime(2026, 10, 28, 11, 59, 59, tzinfo=timezone.utc)))
        self.assertTrue(engine.released(session, self.now))
        session["ready"] = False
        self.assertFalse(engine.released(session, self.now))
        session.update(ready=True, starts_at=None)
        self.assertFalse(engine.released(session, self.now))

    def test_future_content_and_notebook_outputs_are_absent(self):
        jobs = self.build()
        self.assertEqual(len(jobs), 1)
        for path in self.output.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("FUTURE_CONTENT_SENTINEL", text)
                self.assertNotIn("PRIVATE_OUTPUT_SENTINEL", text)
        self.assertFalse((self.output / "cours/introduction-au-mlops/session-02").exists())
        nb = json.loads((self.output / "cours/introduction-au-mlops/session-01/resources/exemple.ipynb").read_text())
        self.assertEqual(nb["cells"][0]["source"], ["print(2 * 3 + 1)"])
        self.assertEqual(nb["cells"][0]["outputs"], [])
        self.assertIsNone(nb["cells"][0]["execution_count"])

    def test_locked_sessions_do_not_need_private_files(self):
        self.edit(lambda c: [s.update(ready=False) for s in c["sessions"]])
        jobs = engine.stage(self.catalog, self.root / "absent", self.output, self.now)
        self.assertEqual(jobs, [])

    def test_due_missing_content_fails(self):
        with self.assertRaises(ValueError):
            engine.stage(self.catalog, self.root / "absent", self.output, self.now)

    def test_naive_dates_fail(self):
        self.edit(lambda c: c["sessions"][0].update(starts_at="2026-10-28T12:00:00"))
        with self.assertRaises(ValueError):
            self.build()

    def test_missing_ready_flag_fails(self):
        self.edit(lambda c: c["sessions"][0].pop("ready"))
        with self.assertRaises(ValueError):
            self.build()

    def test_duplicate_ids_fail(self):
        self.edit(lambda c: c["sessions"].append(dict(c["sessions"][0])))
        with self.assertRaises(ValueError):
            self.build()

    def test_path_traversal_and_symlinks_fail(self):
        for path in ("../teacher.txt", "/etc/passwd", ".git/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                engine.private_file(self.content / "student", path)
        target = self.content / "teacher.txt"
        target.write_text("PRIVATE", encoding="utf-8")
        (self.content / "student/link.txt").symlink_to(target)
        with self.assertRaises(ValueError):
            engine.private_file(self.content / "student", "link.txt")

    def test_pdf_resources_fail(self):
        base = self.content / "student/introduction-au-mlops/session-01"
        (base / "resources/precomputed.PDF").write_text("not a real PDF")
        (base / "resources.yml").write_text("resources:\n  - path: precomputed.PDF\n")
        with self.assertRaises(ValueError):
            self.build()

    def test_teacher_content_is_not_copied(self):
        teacher = self.content / "teacher"
        teacher.mkdir()
        (teacher / "oral.md").write_text("TEACHER_ONLY_SENTINEL")
        self.build()
        self.assertFalse(any("TEACHER_ONLY_SENTINEL" in p.read_text() for p in self.output.rglob("*") if p.is_file()))

    def test_staging_cannot_reuse_stale_output(self):
        self.build()
        with self.assertRaises(ValueError):
            self.build()

    def test_second_course_has_independent_schedule(self):
        data = yaml.safe_load(self.catalog.read_text())
        data["courses"].append({"id": "autre-cours", "title": "Autre cours", "description": "Un autre programme",
                                "timezone": "Africa/Porto-Novo", "sessions": [
            {"id": "session-01", "title": "Introduction", "ready": False, "starts_at": None}]})
        self.catalog.write_text(yaml.safe_dump(data))
        self.build()
        self.assertTrue((self.output / "cours/autre-cours/index.qmd").exists())


if __name__ == "__main__":
    unittest.main()
