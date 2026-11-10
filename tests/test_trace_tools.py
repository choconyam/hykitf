"""Synthetic CLI checks for trace_tools.py; expected numbers are counted by hand below."""

import csv
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


KIT = Path(__file__).resolve().parents[1]
SCRIPT = KIT / "trace_tools.py"


class TraceToolsCLI(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".trace-tools-test-", dir=KIT))
        (self.root / "data").mkdir()
        # 챔버 A 웨이퍼 1: STEP1 값 9,1,1 / STEP2 값 5,5,5,빈칸,6 ; 챔버 B 웨이퍼 2: STEP1 값 2,4,4
        rows = [["time", "chamber", "wafer", "step", "vib"],
                ["2026-10-20 00:00:00", "A", "1", "STEP1", "9"],
                ["2026-10-20 00:00:01", "A", "1", "STEP1", "1"],
                ["2026-10-20 00:00:01", "B", "2", "STEP1", "2"],
                ["2026-10-20 00:00:02", "A", "1", "STEP1", "1"],
                ["2026-10-20 00:00:02", "B", "2", "STEP1", "4"],
                ["2026-10-20 00:00:03", "A", "1", "STEP2", "5"],
                ["2026-10-20 00:00:03", "B", "2", "STEP1", "4"],
                ["2026-10-20 00:00:04", "A", "1", "STEP2", "5"],
                ["2026-10-20 00:00:05", "A", "1", "STEP2", "5"],
                ["2026-10-20 00:00:06", "A", "1", "STEP2", ""],
                ["2026-10-20 00:00:07", "A", "1", "STEP2", "6"]]
        with (self.root / "data/day1.csv").open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(rows)

    def tearDown(self):
        target = self.root.resolve()
        if target == KIT.resolve() or KIT.resolve() not in target.parents:
            raise AssertionError("Refusing cleanup outside the test workspace")
        shutil.rmtree(target)

    def run_cli(self, *args):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], cwd=self.root, env=env,
                              text=True, encoding="utf-8", capture_output=True, timeout=30)

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def summary(self, path="out/s.csv"):
        with (self.root / path).open(encoding="utf-8", newline="") as stream:
            return {(r["chamber"], r["wafer"], r["step"]): r for r in csv.DictReader(stream)}

    def test_summary_counts_runs_and_blanks(self):
        original = (self.root / "data/day1.csv").read_bytes()
        self.ok(self.run_cli("summarize", "--source", "data", "--group", "chamber,wafer,step", "--value", "vib",
                             "--over", "4.5", "--time", "time", "--out", "out/s.csv"))
        rows = self.summary()
        a2 = rows[("A", "1", "STEP2")]
        self.assertEqual((a2["rows"], a2["blank_or_text"], a2["n"]), ("5", "1", "4"))
        self.assertEqual((a2["over_count"], a2["longest_over_run"]), ("4", "3"))  # 빈칸이 연속을 끊는다
        self.assertEqual((a2["longest_same_run"], a2["same_value"]), ("3", "5.0"))
        self.assertEqual((a2["min"], a2["max"], a2["mean"]), ("5.0", "6.0", "5.25"))
        self.assertEqual((a2["first_time"], a2["last_time"]), ("2026-10-20 00:00:03", "2026-10-20 00:00:07"))
        a1 = rows[("A", "1", "STEP1")]
        self.assertEqual((a1["over_count"], a1["p50"], a1["max"]), ("1", "1.0", "9.0"))
        b1 = rows[("B", "2", "STEP1")]
        self.assertEqual((b1["over_count"], b1["longest_same_run"]), ("0", "2"))
        self.assertEqual((self.root / "data/day1.csv").read_bytes(), original)
        note = (self.root / "out/s.csv.md").read_text(encoding="utf-8")
        self.assertIn("summarize --source data", note)
        self.assertIn("읽은 행 11", note)

    def test_skip_first_and_where(self):
        self.ok(self.run_cli("summarize", "--source", "data/day1.csv", "--group", "chamber,wafer,step", "--value", "vib",
                             "--where", "chamber=A", "--skip-first", "1", "--over", "4.5", "--out", "out/s.csv"))
        rows = self.summary()
        self.assertEqual(set(rows), {("A", "1", "STEP1"), ("A", "1", "STEP2")})
        self.assertEqual(rows[("A", "1", "STEP1")]["over_count"], "0")  # 첫 행 9를 뺐다
        self.assertEqual(rows[("A", "1", "STEP2")]["rows"], "4")

    def test_missing_column_is_named(self):
        result = self.run_cli("summarize", "--source", "data", "--value", "pressure", "--out", "out/s.csv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pressure", result.stdout + result.stderr)

    def test_plot_svg_is_valid_and_has_series(self):
        self.ok(self.run_cli("summarize", "--source", "data", "--group", "chamber,wafer,step", "--value", "vib",
                             "--time", "time", "--out", "out/s.csv"))
        self.ok(self.run_cli("plot", "--source", "out/s.csv", "--x", "first_time", "--y", "max", "--series", "chamber",
                             "--hline", "4.5", "--out", "out/fig/max.svg"))
        tree = ET.parse(self.root / "out/fig/max.svg")
        text = ET.tostring(tree.getroot(), encoding="unicode")
        self.assertIn("기준 4.5", text)
        self.assertIn(">A<", text)
        self.assertIn(">B<", text)
        self.ok(self.run_cli("plot", "--source", "data/day1.csv", "--x", "step", "--y", "vib", "--kind", "scatter",
                             "--where", "chamber=A", "--out", "out/fig/raw.svg"))
        ET.parse(self.root / "out/fig/raw.svg")

    def test_plot_thins_but_keeps_spike(self):
        with (self.root / "data/long.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["t", "v"])
            writer.writerows([i, 100 if i == 777 else 1] for i in range(5000))
        result = self.run_cli("plot", "--source", "data/long.csv", "--x", "t", "--y", "v", "--max-points", "200",
                              "--out", "out/fig/long.svg")
        self.ok(result)
        svg = (self.root / "out/fig/long.svg").read_text(encoding="utf-8")
        self.assertLess(svg.count(","), 400)
        self.assertIn(",40.0", svg)  # 튄 값(100)이 그림 맨 위(y=40) 점으로 남는다


    def write(self, name, rows, encoding="utf-8"):
        with (self.root / "data" / name).open("w", encoding=encoding, newline="") as stream:
            csv.writer(stream).writerows(rows)

    def test_segment_resets_skip_and_runs_per_wafer(self):
        # 묶음은 챔버×스텝, 웨이퍼마다 앞 2행(9,9)은 전환 구간. 웨이퍼 사이 연속은 이어지면 안 된다.
        rows = [["chamber", "wafer", "step", "v"]]
        for wafer in ("1", "2"):
            rows += [["A", wafer, "ETCH", v] for v in ("9", "9", "5", "5")]
        self.write("seg.csv", rows)
        self.ok(self.run_cli("summarize", "--source", "data/seg.csv", "--group", "chamber,step", "--segment", "wafer",
                             "--value", "v", "--skip-first", "2", "--over", "6", "--out", "out/seg.csv"))
        with (self.root / "out/seg.csv").open(encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual((row["rows"], row["over_count"], row["longest_same_run"]), ("4", "0", "2"))

    def test_small_values_are_not_rounded_to_zero(self):
        self.write("p.csv", [["g", "p"], ["A", "3e-8"], ["A", "4e-8"], ["A", "5e-8"]])
        self.ok(self.run_cli("summarize", "--source", "data/p.csv", "--group", "g", "--value", "p", "--out", "out/p.csv"))
        with (self.root / "out/p.csv").open(encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertAlmostEqual(float(row["min"]), 3e-8)
        self.assertAlmostEqual(float(row["mean"]), 4e-8)

    def test_long_format_sensor_column_is_kept(self):
        self.write("long.csv", [["chamber", "sensor", "value"], ["A", "vib", "1"], ["A", "temp", "50"]])
        self.ok(self.run_cli("summarize", "--source", "data/long.csv", "--group", "chamber,sensor", "--value", "value",
                             "--out", "out/l.csv"))
        with (self.root / "out/l.csv").open(encoding="utf-8", newline="") as stream:
            rows = {r["sensor"]: r["max"] for r in csv.DictReader(stream)}
        self.assertEqual(rows, {"vib": "1.0", "temp": "50.0"})
        result = self.run_cli("summarize", "--source", "data/long.csv", "--group", "max", "--value", "value", "--out", "out/x.csv")
        self.assertNotEqual(result.returncode, 0)

    def test_cp949_file_gives_a_hint_not_a_traceback(self):
        self.write("k.csv", [["챔버", "값"], ["가", "1"]], encoding="cp949")
        result = self.run_cli("summarize", "--source", "data/k.csv", "--value", "값", "--out", "out/k.csv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cp949", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.ok(self.run_cli("summarize", "--source", "data/k.csv", "--value", "값", "--encoding", "cp949", "--out", "out/k.csv"))

    def test_slash_times_and_blank_x_stay_a_time_axis_without_shift(self):
        rows = [["t", "v"], ["", "1"]] + [[f"2026/11/04 10:{m:02d}:00", str(m)] for m in range(30)]
        self.write("slash.csv", rows)
        result = self.run_cli("plot", "--source", "data/slash.csv", "--x", "t", "--y", "v", "--out", "out/fig/s.svg")
        self.ok(result)
        self.assertNotIn("글자 순서", result.stdout)
        self.assertIn("가로축 빈 행 1개", result.stdout)
        svg = (self.root / "out/fig/s.svg").read_text(encoding="utf-8")
        self.assertIn(">10:00:00<", svg)  # 컴퓨터 시간대로 옮겨지지 않는다
        self.write("z.csv", [["t", "v"], ["2026-11-04T10:00:00+09:00", "1"], ["2026-11-04T10:20:00+09:00", "2"]])
        self.ok(self.run_cli("plot", "--source", "data/z.csv", "--x", "t", "--y", "v", "--out", "out/fig/z.svg"))
        self.assertIn(">10:00:00<", (self.root / "out/fig/z.svg").read_text(encoding="utf-8"))


    # 관리도(SPC). 기준 6점 10,12,10,12,10,12 → 중심 11, 이웃 차이는 모두 2 → 시그마 2/1.128=1.77305,
    # 위 한계 11+3*1.77305=16.31915, 아래 한계 5.68085 (손 계산).
    BASE = ["10", "12", "10", "12", "10", "12"]

    def spc_rows(self, values, *extra):
        self.write("spc.csv", [["seq", "v"], *[[str(i + 1), v] for i, v in enumerate(values)]])
        self.ok(self.run_cli("spc", "--source", "data/spc.csv", "--value", "v", "--order", "seq",
                             "--baseline-first", "6", "--out", "out/spc.csv", *extra))
        with (self.root / "out/spc.csv").open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_spc_limits_and_rule_1(self):
        rows = self.spc_rows(self.BASE + ["16", "17"], "--rules", "1")
        self.assertEqual([r["in_baseline"] for r in rows], ["1"] * 6 + ["0", "0"])
        self.assertAlmostEqual(float(rows[0]["center"]), 11.0)
        self.assertAlmostEqual(float(rows[0]["sigma"]), 2 / 1.128, places=6)
        self.assertAlmostEqual(float(rows[0]["ucl"]), 16.31915, places=4)
        self.assertAlmostEqual(float(rows[0]["lcl"]), 5.68085, places=4)
        self.assertEqual([r["signal"] for r in rows], ["0"] * 7 + ["1"])  # 16은 한계 안, 17만 밖
        note = (self.root / "out/spc.csv.md").read_text(encoding="utf-8")
        self.assertIn("spc --source data/spc.csv", note)
        self.assertIn("| 전체 | 6 | 8 |", note)

    def test_spc_run_and_trend_rules(self):
        # 6번(12)부터 중심선 위로 8점이 이어지는 것은 13번 점에서다.
        rows = self.spc_rows(self.BASE + ["11.5"] * 8, "--rules", "4")
        self.assertEqual([r["point"] for r in rows if r["r4"] == "1"], ["13", "14"])
        # 7~12번이 11.0→11.5로 6점 연속 오름. 6점째인 12번에서 걸린다.
        rows = self.spc_rows(self.BASE + ["11.0", "11.1", "11.2", "11.3", "11.4", "11.5"], "--rules", "5")
        self.assertEqual([r["point"] for r in rows if r["r5"] == "1"], ["12"])

    def test_spc_ewma_catches_small_shift_that_rule_1_misses(self):
        # 14는 z=1.69라 3시그마 규칙에는 안 걸린다. EWMA(0.2)는 중심 11에서 시작해 기준 6점을 지나 11.081984가 되고,
        # 14가 네 번 이어진 10번 점에서 12.80478(중심+1.80478)이 되어 그 점의 한계 1.7627을 처음 넘는다(손 계산. 9번 점은 1.50598 < 1.7569).
        rows = self.spc_rows(self.BASE + ["14"] * 6, "--rules", "1", "--ewma", "0.2")
        self.assertEqual({r["r1"] for r in rows}, {"0"})
        self.assertEqual([r["point"] for r in rows if r["ewma_signal"] == "1"][0], "10")
        self.assertAlmostEqual(float(rows[9]["ewma"]), 12.80478, places=4)
        self.assertAlmostEqual(float(rows[9]["ewma_ucl"]) - 11, 1.7627, places=3)

    def test_spc_groups_order_baseline_until_and_spec(self):
        rows = [["day", "chamber", "depth"]]
        for i, day in enumerate(["2026-10-25", "2026-10-21", "2026-10-22", "2026-10-20", "2026-10-23", "2026-10-24"]):
            ordered = sorted(["2026-10-25", "2026-10-21", "2026-10-22", "2026-10-20", "2026-10-23", "2026-10-24"]).index(day)
            rows.append([day, "A", ["10", "12", "10", "12", "10", "12"][ordered]])
            rows.append([day, "B", ["10", "12", "10", "12", "30", "31"][ordered]])
        rows.append(["2026-10-26", "A", ""])
        self.write("m.csv", rows)
        self.ok(self.run_cli("spc", "--source", "data/m.csv", "--value", "depth", "--group", "chamber", "--order", "day",
                             "--baseline-until", "2026-10-23", "--usl", "20", "--out", "out/m.csv"))
        with (self.root / "out/m.csv").open(encoding="utf-8", newline="") as stream:
            out = list(csv.DictReader(stream))
        a = [r for r in out if r["chamber"] == "A"]
        b = [r for r in out if r["chamber"] == "B"]
        self.assertEqual([r["day"] for r in a], sorted(r["day"] for r in a))  # 날짜순으로 다시 놓임
        self.assertEqual([r["in_baseline"] for r in b], ["1", "1", "1", "1", "0", "0"])
        self.assertEqual({r["signal"] for r in a}, {"0"})
        self.assertEqual([r["signal"] for r in b], ["0", "0", "0", "0", "1", "1"])
        self.assertEqual([r["out_of_spec"] for r in b], ["0", "0", "0", "0", "1", "1"])
        note = (self.root / "out/m.csv.md").read_text(encoding="utf-8")
        self.assertIn("뺀 행 1개", note)
        self.assertIn("Cpk", note)

    def test_spc_constant_baseline_still_flags_and_warns(self):
        rows = self.spc_rows(["5", "5", "5", "5", "5", "5", "50", "5", "-40"], "--rules", "1,4")
        self.assertEqual([r["point"] for r in rows if r["signal"] == "1"], ["7", "9"])
        self.assertIn("시그마가 0", (self.root / "out/spc.csv.md").read_text(encoding="utf-8"))

    def test_spc_mixed_order_values_stop_instead_of_text_sorting(self):
        self.write("o.csv", [["seq", "v"], *[[s, "1"] for s in ("1", "2", "9", "10", "NA", "11")]])
        result = self.run_cli("spc", "--source", "data/o.csv", "--value", "v", "--order", "seq", "--out", "out/o.csv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NA", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_spc_baseline_exclude_keeps_point_on_chart_but_out_of_limits(self):
        # 3번 점 40은 확인된 이상(flag=1). 빼면 기준 점은 10,12,(40 뺌),10,12,10,12 → 중심 11.
        # 이웃 차이는 2번-4번 사이를 건너뛰어 (1,2) (4,5) (5,6) (6,7) 네 개, 모두 2 → 시그마 2/1.128.
        values = ["10", "12", "40", "10", "12", "10", "12", "11"]
        self.write("e.csv", [["seq", "v", "flag"], *[[str(i + 1), v, "1" if i == 2 else "0"] for i, v in enumerate(values)]])
        self.ok(self.run_cli("spc", "--source", "data/e.csv", "--value", "v", "--order", "seq", "--baseline-first", "7",
                             "--baseline-exclude", "flag=1", "--rules", "1", "--out", "out/e.csv"))
        with (self.root / "out/e.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 8)
        self.assertAlmostEqual(float(rows[0]["center"]), 11.0)
        self.assertAlmostEqual(float(rows[0]["sigma"]), 2 / 1.128, places=6)
        self.assertEqual((rows[2]["in_baseline"], rows[2]["baseline_excluded"], rows[2]["signal"]), ("0", "1", "1"))
        self.assertIn("| 전체 | 6 (뺀 점 1) | 8 |", (self.root / "out/e.csv.md").read_text(encoding="utf-8"))

    def test_spc_date_only_cutoff_includes_that_whole_day(self):
        rows = [["t", "v"], ["2026-10-23 08:00:00", "10"], ["2026-10-23 9:05", "12"], ["2026-10-23 23:59:59.5", "10"],
                ["2026-10-24 00:00:00", "12"]]
        self.write("d.csv", rows)
        self.ok(self.run_cli("spc", "--source", "data/d.csv", "--value", "v", "--order", "t",
                             "--baseline-until", "2026-10-23", "--out", "out/d.csv"))
        with (self.root / "out/d.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual([r["in_baseline"] for r in csv.DictReader(stream)], ["1", "1", "1", "0"])
        note = (self.root / "out/d.csv.md").read_text(encoding="utf-8")
        self.assertIn("그날 끝까지 포함", note)
        self.assertIn("3개뿐", note)

    def test_spc_limit_width_and_two_levels(self):
        # 시그마 1.77305. 17은 z=3.38, 18.2는 z=4.06, 20은 z=5.08 (손 계산).
        rows = self.spc_rows(self.BASE + ["17", "18.2"], "--rules", "1", "--k", "4")
        self.assertAlmostEqual(float(rows[0]["ucl"]), 11 + 4 * 2 / 1.128, places=4)  # 18.0922
        self.assertEqual([r["signal"] for r in rows], ["0"] * 7 + ["1"])  # 4시그마에서는 18.2만 걸린다
        self.assertIn("4시그마", (self.root / "out/spc.csv.md").read_text(encoding="utf-8"))
        rows = self.spc_rows(self.BASE + ["17", "20"], "--rules", "1", "--stop-k", "5")
        self.assertAlmostEqual(float(rows[0]["stop_ucl"]), 11 + 5 * 2 / 1.128, places=4)  # 19.8652
        self.assertEqual([r["level"] for r in rows], [""] * 6 + ["warn", "stop"])
        note = (self.root / "out/spc.csv.md").read_text(encoding="utf-8")
        self.assertIn("370점에 한 번", note)  # 3시그마의 우연한 신호
        self.assertIn("정지 점 수", note)

    def test_spc_confirm_needs_consecutive_points_on_one_side(self):
        # 한계 밖: 7번(+), 9번(+), 10번(+), 11번(-), 12번(-). 같은 쪽으로 2점 연속은 10번과 12번에서 완성된다.
        rows = self.spc_rows(self.BASE + ["17", "11", "17", "17.5", "5", "4"], "--rules", "none", "--confirm", "2")
        self.assertEqual([r["point"] for r in rows if r["rc"] == "1"], ["10", "12"])
        self.assertEqual([r["point"] for r in rows if r["signal"] == "1"], ["10", "12"])

    def test_spc_keep_carries_identifiers_through_sorting(self):
        self.write("w.csv", [["seq", "wafer", "v"], ["2", "W2", "12"], ["1", "W1", "10"], ["3", "W3", "10"]])
        self.ok(self.run_cli("spc", "--source", "data/w.csv", "--value", "v", "--order", "seq", "--keep", "wafer", "--out", "out/w.csv"))
        with (self.root / "out/w.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(r["seq"], r["wafer"], r["value"]) for r in rows], [("1", "W1", "10.0"), ("2", "W2", "12.0"), ("3", "W3", "10.0")])

    def test_spc_bad_options_are_explained(self):
        self.write("spc.csv", [["seq", "v"], ["1", "1"], ["2", "2"]])
        for extra in (["--rules", "9"], ["--baseline-until", "3"], ["--ewma", "2", "--order", "seq"],
                      ["--usl", "1", "--lsl", "2"], ["--order", "seq", "--baseline-until", "어제"],
                      ["--k", "0"], ["--stop-k", "2"], ["--confirm", "1"], ["--keep", "없는열"]):
            result = self.run_cli("spc", "--source", "data/spc.csv", "--value", "v", "--out", "out/x.csv", *extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
