"""Synthetic CLI checks; never read exam data or import the converter's helpers."""

import csv
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1]
SCRIPT = KIT / "make_data_guide.py"


class MakeDataGuideCLI(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".make-data-guide-test-", dir=KIT))
        self.output = self.root / "docs/data-guide/_generated"

    def tearDown(self):
        target = self.root.resolve()
        if target == KIT.resolve() or KIT.resolve() not in target.parents:
            raise AssertionError("Refusing cleanup outside the test workspace")
        shutil.rmtree(target)

    def write_csv(self, path="data/input.csv", rows=None, encoding="utf-8"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding=encoding, newline="") as stream:
            csv.writer(stream).writerows(rows or [["id", "value"], ["001", "0"]])
        return target

    def run_cli(self, *args):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args], cwd=self.root, env=env,
            text=True, encoding="utf-8", capture_output=True, timeout=30,
        )

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def manifest(self):
        return json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))

    def pages(self):
        return sorted(p for p in self.output.rglob("*.md") if p.name != "INDEX.md")

    def all_output(self):
        return "\n".join(p.read_text(encoding="utf-8") for p in self.output.rglob("*") if p.is_file())

    def displayed_output(self):
        return html.unescape(self.all_output())

    @staticmethod
    def table_rows(text):
        """Read stored Markdown using an independent, tiny display-value oracle."""
        rows = []
        for line in text.splitlines():
            if not line.startswith("|") or re.fullmatch(r"[| :\-]+", line):
                continue
            cells = re.split(r"(?<!\\)\|", line.strip()[1:-1])
            decoded = []
            for cell in cells:
                cell = re.sub(r"<br\s*/?>", "\n", cell.strip(), flags=re.I)
                cell = re.sub(r"\\([\\|`*_{}\[\]()#+.!>-])", r"\1", cell)
                decoded.append(html.unescape(cell))
            rows.append(decoded)
        return rows

    def test_values_survive_and_original_is_unchanged(self):
        values = ["001", "", "0", "2026-01-02", 'a,"b"', "a|b", "first\nsecond", "<script>x</script>", "&lt;literal&gt;"]
        source = self.write_csv(rows=[["id", "empty", "zero", "date", "quote", "pipe", "lines", "html", "entity"], values])
        original = source.read_bytes()
        self.success(self.run_cli("--source", "data"))
        self.assertEqual(source.read_bytes(), original)
        pages = self.pages()
        self.assertTrue(pages)
        text = "\n".join(p.read_text(encoding="utf-8") for p in pages)
        rows = self.table_rows(text)
        self.assertTrue(any(row[-len(values):] == values for row in rows), rows)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<script>", self.all_output())

    def test_plain_text_stays_readable_and_edge_spaces_survive(self):
        values = ["김 철수", "order_id", "  lead", "trail  ", " ", "_x_", "a\u3000b", "$5", "~~x~~"]
        self.write_csv(rows=[[f"c{i}" for i in range(len(values))], values])
        self.success(self.run_cli("--source", "data"))
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.pages())
        rows = self.table_rows(text)
        self.assertTrue(any(row[-len(values):] == values for row in rows), rows)
        self.assertIn("| 김 철수 | order_id |", text)

    def test_mixed_encodings_are_detected_per_file(self):
        self.write_csv("data/utf.csv", [["id", "이름"], ["001", "유니코드"]], "utf-8")
        self.write_csv("data/legacy.csv", [["id", "메모"], ["002", "한글엑셀"]], "cp949")
        self.success(self.run_cli("--source", "data"))
        files = {item["path"]: item for item in self.manifest()["files"]}
        self.assertEqual(files["data/utf.csv"]["encoding"], "utf-8-sig")
        self.assertEqual(files["data/legacy.csv"]["encoding"], "cp949")
        self.assertIn("한글엑셀", self.displayed_output())
        self.assertIn("CP949", (self.output / "INDEX.md").read_text(encoding="utf-8"))

    def test_inventory_does_not_publish_values(self):
        self.write_csv(rows=[["id", "note", "blank"], ["001", "PRIVATE_SENTINEL_9281", ""], ["001", "PRIVATE_SENTINEL_9281", ""]])
        result = self.run_cli("--source", "data", "--inventory-only")
        self.success(result)
        self.assertNotIn("PRIVATE_SENTINEL_9281", result.stdout + result.stderr + self.displayed_output())
        self.assertFalse(self.pages())
        self.assertIn("input.csv", self.all_output())
        item = self.manifest()["files"][0]
        self.assertEqual(item["rows"], 2)
        self.assertEqual(item["empty_counts"], [0, 0, 2])
        self.assertEqual(item["duplicate_rows"], 1)
        self.assertEqual(item["status"], "inventoried")
        self.assertIn("CSV 구조만 확인 1개", result.stdout)

    def test_list_only_keeps_trace_in_index_without_values(self):
        self.write_csv("data/master.csv", [["id", "name"], ["001", "a"]])
        trace = self.write_csv("data/trace/day1.csv", [["time", "chamber", "vib"], *[[str(i), "A", "SECRET9"] for i in range(20)]])
        original = trace.read_bytes()
        self.success(self.run_cli("--source", "data/master.csv", "--list-only", "data/trace"))
        index = (self.output / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("data/trace/day1.csv", index)
        self.assertIn("목록만", index)
        self.assertIn("time, chamber, vib", index)
        self.assertNotIn("SECRET9", self.displayed_output())
        statuses = {f["path"]: f["status"] for f in self.manifest()["files"]}
        self.assertEqual(statuses["data/trace/day1.csv"], "listed")
        self.assertEqual(statuses["data/master.csv"], "ready")
        self.assertEqual(trace.read_bytes(), original)
        self.assertIn("--list-only data/trace", index)

    def test_list_only_never_writes_a_value_line(self):
        self.write_csv("data/trace/noheader.csv", [["2026-11-04 10:00:00", "A", "LOT_SECRET_77", "3.2"]])
        target = self.root / "data/trace/preamble.csv"
        target.write_text("# Tool ETCH01 Lot SECRET_LOT_88\ntime,v\n1,2\n", encoding="utf-8")
        self.write_csv("data/trace/semi.csv", [["time;chamber;v"], ["1;A;2"]])
        self.success(self.run_cli("--list-only", "data/trace", "--delimiter", ";"))
        output = self.displayed_output()
        self.assertNotIn("LOT_SECRET_77", output)
        self.assertNotIn("SECRET_LOT_88", output)
        self.assertIn("time, chamber, v", output)
        self.assertIn("목록만(--list-only)", output)
        self.success(self.run_cli("--list-only", "data/trace"))
        self.assertNotIn("LOT_SECRET_77", self.displayed_output())

    def test_source_or_list_only_is_required(self):
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)

    def test_mask_column_and_unknown_column(self):
        self.write_csv(rows=[["id", "EMAIL"], ["001", "secret@example.invalid"]])
        result = self.run_cli("--source", "data", "--mask-column", "EMAIL")
        self.success(result)
        self.assertNotIn("secret@example.invalid", result.stdout + result.stderr + self.displayed_output())
        self.assertIn("001", self.all_output())
        result = self.run_cli("--source", "data", "--mask-column", "missing_column")
        self.assertNotEqual(result.returncode, 0)

    def test_same_names_and_korean_paths_do_not_collide(self):
        self.write_csv("자료 하나/input.csv", [["id"], ["LEFT_001"]])
        self.write_csv("자료 둘/input.csv", [["id"], ["RIGHT_002"]])
        self.success(self.run_cli("--source", "자료 하나", "--source", "자료 둘"))
        pages = self.pages()
        self.assertEqual(len(pages), 2)
        self.assertNotEqual(pages[0].relative_to(self.output), pages[1].relative_to(self.output))
        self.assertTrue(all(p.name.startswith("input-") for p in pages), pages)
        self.assertIn("LEFT_001", self.displayed_output())
        self.assertIn("RIGHT_002", self.displayed_output())

    def test_split_and_rerun_do_not_publish_stale_pages(self):
        self.write_csv(rows=[["id"], *[[str(n)] for n in range(1001)]])
        self.success(self.run_cli("--source", "data"))
        old_pages = self.pages()
        self.assertEqual(len(old_pages), 2)
        self.write_csv(rows=[["id"], ["new_001"]])
        self.success(self.run_cli("--source", "data"))
        index = (self.output / "INDEX.md").read_text(encoding="utf-8")
        manifest = (self.output / "manifest.json").read_text(encoding="utf-8")
        for page in old_pages:
            if page.exists() and "new_001" not in html.unescape(page.read_text(encoding="utf-8")):
                self.assertNotIn(page.name, index)
                self.assertNotIn(page.name, manifest)

    def test_limits_do_not_claim_complete_conversion(self):
        self.write_csv(rows=[["id"], ["1"], ["2"], ["3"]])
        result = self.run_cli("--source", "data", "--max-rows", "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pages())
        self.write_csv(rows=[["id"], *[[str(n)] for n in range(10001)]])
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pages())
        item = self.manifest()["files"][0]
        self.assertNotEqual(item["status"], "ready")
        self.assertNotIn("rows", item, "A partial read must not claim an exact total")
        self.assertIn("input.csv", self.all_output())
        result = self.run_cli("--source", "data", "--max-bytes", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pages())

    def test_bad_width_and_encoding_are_reported(self):
        bad = self.root / "data/bad.csv"
        bad.parent.mkdir(parents=True)
        bad.write_text("id,value\n1,2,3\n", encoding="utf-8")
        (bad.parent / "broken.csv").write_bytes(b"id\n\xff\n")
        self.write_csv("data/good.csv")
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        content = self.all_output()
        for name in ["bad.csv", "broken.csv", "good.csv"]:
            self.assertIn(name, content)
        self.assertTrue(self.pages(), "Valid sibling should still be processed")

    def test_empty_and_header_only_do_not_crash(self):
        empty = self.root / "data/empty.csv"
        empty.parent.mkdir(parents=True)
        empty.write_bytes(b"")
        self.write_csv("data/header.csv", [["id", "value"]])
        result = self.run_cli("--source", "data")
        self.assertNotIn("Traceback", result.stderr)
        content = self.all_output()
        self.assertIn("empty.csv", content)
        self.assertIn("header.csv", content)

    def test_bom_and_explicit_cp949(self):
        self.write_csv("utf/input.csv", [["id", "이름"], ["001", "예시"]], "utf-8-sig")
        self.success(self.run_cli("--source", "utf"))
        self.assertNotIn("\ufeff", self.all_output())
        self.write_csv("legacy/input.csv", [["id", "메모"], ["002", "테스트"]], "cp949")
        self.success(self.run_cli("--source", "legacy", "--encoding", "cp949"))
        self.assertIn("테스트", self.all_output())

    def test_explicit_delimiter_and_unusual_headers(self):
        source = self.root / "delimited/values.csv"
        source.parent.mkdir(parents=True)
        source.write_text("id;value\n001;0\n", encoding="utf-8")
        self.success(self.run_cli("--source", "delimited", "--delimiter", ";"))
        self.assertEqual(self.manifest()["files"][0]["columns"], ["id", "value"])
        self.write_csv("odd/duplicate.csv", [["id", "id"], ["DUP_1", "DUP_2"]])
        self.write_csv("odd/blank.csv", [["", "name"], ["0", "BLANK_A"]])
        self.success(self.run_cli("--source", "odd", "--mask-column", "id"))
        files = {item["path"]: item for item in self.manifest()["files"]}
        self.assertTrue(all(item["status"] == "ready" for item in files.values()), files)
        self.assertEqual(files["odd/blank.csv"]["columns"], ["(이름 없음 1)", "name"])
        self.assertNotIn("DUP_1", self.displayed_output())
        self.assertNotIn("DUP_2", self.displayed_output())
        self.assertIn("BLANK_A", self.displayed_output())
        index = (self.output / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("같은 열 이름", index)
        self.assertIn("이름이 빈 열", index)

    def test_unsupported_and_excluded_folders(self):
        self.write_csv()
        (self.root / "data/photo.png").write_bytes(b"not a real image")
        (self.root / "README.md").write_text("Task material", encoding="utf-8")
        self.write_csv("data/tests/keep.csv", [["id"], ["INCLUDED_TESTS"]])
        self.write_csv("data/analysis/keep.csv", [["id"], ["INCLUDED_ANALYSIS"]])
        for folder in ["tests", "out", "analysis"]:
            self.write_csv(f"{folder}/excluded.csv", [["id"], ["EXCLUDED_SENTINEL"]])
        self.write_csv("data/node_modules/excluded.csv", [["id"], ["EXCLUDED_SENTINEL"]])
        result = self.run_cli("--source", ".")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("photo.png", self.all_output())
        self.assertIn("README.md", self.all_output())
        self.assertNotIn("EXCLUDED_SENTINEL", self.displayed_output())
        self.assertIn("INCLUDED_TESTS", self.displayed_output())
        self.assertIn("INCLUDED_ANALYSIS", self.displayed_output())

    def test_masking_removes_old_unmasked_pages_from_disk(self):
        self.write_csv(rows=[["EMAIL"], *[["OLD_PRIVATE_VALUE"] for _ in range(1001)]])
        self.success(self.run_cli("--source", "data"))
        self.assertIn("OLD_PRIVATE_VALUE", self.displayed_output())
        self.write_csv(rows=[["EMAIL"], ["NEW_PRIVATE_VALUE"]])
        self.success(self.run_cli("--source", "data", "--mask-column", "EMAIL"))
        self.assertNotIn("OLD_PRIVATE_VALUE", self.displayed_output())
        self.assertNotIn("NEW_PRIVATE_VALUE", self.displayed_output())

    def test_source_symlink_is_not_followed(self):
        source = self.write_csv("elsewhere/source.csv", [["id"], ["LINKED_SECRET"]])
        folder = self.root / "data"
        folder.mkdir()
        try:
            (folder / "link.csv").symlink_to(source)
        except OSError as error:
            self.skipTest(f"Symlink unavailable: {error}")
        result = self.run_cli("--source", "data")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("LINKED_SECRET", result.stdout + result.stderr + self.displayed_output())

    def test_output_symlink_is_refused(self):
        self.write_csv()
        target = self.root / "keep"
        target.mkdir()
        keep = target / "keep.txt"
        keep.write_text("Keep this", encoding="utf-8")
        self.output.parent.mkdir(parents=True)
        try:
            self.output.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink unavailable: {error}")
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(keep.read_text(encoding="utf-8"), "Keep this")
        self.assertEqual(list(target.iterdir()), [keep])

    @unittest.skipUnless(os.name == "nt", "Windows drive boundary regression")
    def test_missing_other_drive_does_not_abort_valid_source(self):
        import ctypes

        drives = ctypes.windll.kernel32.GetLogicalDrives()
        available = [chr(65 + bit) for bit in range(25, 1, -1) if not drives & (1 << bit)]
        if not available:
            self.skipTest("No unused drive letter")
        missing = f"{available[0]}:/hykit-test-missing.csv"
        self.write_csv()
        result = self.run_cli("--source", "data", "--source", missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        entries = self.manifest()["files"]
        self.assertTrue(any(item["path"] == "data/input.csv" and item["status"] == "ready" for item in entries))
        self.assertTrue(any(item["path"].endswith("hykit-test-missing.csv") and item["status"] == "error" for item in entries))

    def test_rerun_preserves_notes_and_refuses_manual_generated_changes(self):
        self.write_csv()
        note = self.root / "docs/data-guide/notes.md"
        note.parent.mkdir(parents=True)
        note.write_text("내 메모는 보존", encoding="utf-8")
        self.success(self.run_cli("--source", "data"))
        page = self.pages()[0]
        edited = page.read_text(encoding="utf-8") + "\nManual change\n"
        page.write_text(edited, encoding="utf-8")
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(page.read_text(encoding="utf-8"), edited)
        self.assertIn(page.name, result.stderr)
        self.assertEqual(note.read_text(encoding="utf-8"), "내 메모는 보존")

    def test_installer_copies_script_without_overwriting(self):
        bash = shutil.which("bash")
        if not bash and Path("C:/Program Files/Git/bin/bash.exe").is_file():
            bash = "C:/Program Files/Git/bin/bash.exe"
        if not bash:
            self.skipTest("Bash unavailable")
        (self.root / "AGENTS.md").write_text("Keep my rules", encoding="utf-8")
        result = subprocess.run([bash, str(KIT / "apply.sh")], cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "make_data_guide.py").read_bytes(), SCRIPT.read_bytes())
        self.assertTrue((self.root / "docs/data-guide/README.md").exists())
        for name in ("data-agent", "docs-agent", "causes", "spc", "predict", "run"):
            self.assertEqual((self.root / f"docs/guide/{name}.md").read_bytes(), (KIT / f"docs/guide/{name}.md").read_bytes())
        self.assertEqual(result.stdout.decode("utf-8").count("복사함"), 17)  # AGENTS.md는 원래 있어서 건너뜀
        self.run_cli("--source", ".", "--inventory-only")
        statuses = {item["path"]: item["status"] for item in self.manifest()["files"]}
        self.assertEqual(statuses.get("docs/guide"), "skipped")  # 안내 파일은 과제 자료 목록에 넣지 않는다
        self.assertFalse(any(path.startswith("docs/guide/") for path in statuses))
        (self.root / "make_data_guide.py").write_text("# Keep my tool\n", encoding="utf-8")
        result = subprocess.run([bash, str(KIT / "apply.sh")], cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "make_data_guide.py").read_text(encoding="utf-8"), "# Keep my tool\n")
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), "Keep my rules")

    def test_explicit_reserved_folder_and_linked_source_are_read(self):
        self.write_csv("logs/app.csv", [["id"], ["LOG_VALUE"]])
        self.success(self.run_cli("--source", "logs"))
        self.assertIn("LOG_VALUE", self.displayed_output())
        self.success(self.run_cli("--source", "."))
        self.assertNotIn("LOG_VALUE", self.displayed_output())
        real = self.write_csv("real/x.csv", [["id"], ["LINKED_DIR_VALUE"]]).parent
        link = self.root / "data"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                self.skipTest(f"Symlink unavailable: {error}")
            import _winapi
            _winapi.CreateJunction(str(real), str(link))
        self.success(self.run_cli("--source", "data"))
        self.assertIn("LINKED_DIR_VALUE", self.displayed_output())

    def test_blank_lines_are_skipped_and_reported(self):
        folder = self.root / "data"
        folder.mkdir()
        (folder / "blank.csv").write_bytes(b"id,value\r\n1,2\r\n\r\n3,4\r\n\r\n")
        (folder / "one.csv").write_bytes(b"name\r\nA\r\n\r\nC\r\n")
        self.success(self.run_cli("--source", "data"))
        files = {item["path"]: item for item in self.manifest()["files"]}
        self.assertEqual(files["data/blank.csv"]["rows"], 2)
        self.assertEqual(files["data/one.csv"]["rows"], 2)
        index = (self.output / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("빈 줄 2개", index)
        self.assertIn("빈 값일 수 있음", index)

    def test_control_characters_are_refused_with_reason(self):
        self.write_csv(rows=[["id", "note"], ["1", "a\x0bb"]])
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pages())
        self.assertIn("U+000B", self.manifest()["files"][0]["reason"])

    def test_refusals_name_the_file_and_bad_settings_do_not_crash(self):
        self.write_csv()
        self.success(self.run_cli("--source", "data"))
        stray = self.output / "book-sheet1.md"
        stray.write_text("extra", encoding="utf-8")
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("book-sheet1.md", result.stderr)
        stray.unlink()
        (self.output / "manifest.json").write_text('"x"', encoding="utf-8")
        result = self.run_cli("--source", "data")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        result = self.run_cli("--source", "data", "--delimiter", '"')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_undecodable_file_name_does_not_abort(self):
        self.write_csv("data/good.csv")
        try:
            (self.root / "data/note_\udcb0\udca1.txt").write_bytes(b"x")
        except (OSError, UnicodeError) as error:
            self.skipTest(f"Undecodable file names unavailable: {error}")
        result = self.run_cli("--source", "data", "--inventory-only")
        self.success(result)
        index = html.unescape((self.output / "INDEX.md").read_text(encoding="utf-8"))
        self.assertIn("note_\\xb0\\xa1.txt", index)


if __name__ == "__main__":
    unittest.main()
