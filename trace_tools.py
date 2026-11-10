#!/usr/bin/env python3
"""센서 기록(시계열) CSV를 묶음별로 요약하고, 관리도(SPC) 한계와 규칙 신호를 계산하고, CSV 하나를 SVG 그래프로 그린다.
Python 3.8+ 표준 라이브러리만 쓴다.

요약: python3 trace_tools.py summarize --source data/trace --group chamber,wafer_id,step --value vibration --over 3.0 --time time --out docs/data-guide/_extra/vib_summary.csv
      (묶음에 웨이퍼가 없으면 --segment wafer_id를 줘서 웨이퍼가 바뀔 때 앞부분 빼기와 연속 횟수를 새로 센다)
관리도: python3 trace_tools.py spc --source data/metrology.csv --value depth --group chamber --order measured_at --baseline-until 2026-10-24 --ewma 0.2 --out out/spc_depth.csv
      (한계 폭은 --k 3~6, 경고와 정지 두 단계는 --stop-k, 연속 N점 확인은 --confirm, 웨이퍼 식별 열 남기기는 --keep)
      (그림은 plot --source out/spc_depth.csv --x point --y value --y lcl --y ucl --y center --where chamber=B)
그림: python3 trace_tools.py plot --source docs/data-guide/_extra/vib_summary.csv --x first_time --y p95 --series chamber --where step=MAIN_ETCH --hline 3.0 --out out/fig/p95.svg
"""

import argparse
from array import array
import csv
from datetime import datetime, timedelta, timezone
import html
import math
from pathlib import Path
import re
import shlex
import sys


MAX_SERIES = 8
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STAT_COLUMNS = ["rows", "blank_or_text", "n", "min", "mean", "std", "p50", "p95", "max",
                "over_count", "longest_over_run", "longest_same_run", "same_value"]
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TIME_FORMATS = ["%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y%m%d %H%M%S", "%Y%m%d%H%M%S"]


def command_line(argv):
    return shlex.join(["python3", "trace_tools.py", *(sys.argv[1:] if argv is None else argv)])


def natural(path):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.as_posix())]


def csv_files(sources):
    found = []
    for source in sources:
        path = Path(source)
        if path.is_dir():
            found.extend(sorted((p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".csv"), key=natural))
        elif path.is_file():
            found.append(path)
        else:
            raise SystemExit(f"중단: 원본을 찾지 못함: {source}")
    if not found:
        raise SystemExit("중단: CSV 파일이 없음")
    return found


def parse_where(items):
    rules = []
    for item in items:
        if "=" not in item:
            raise SystemExit(f"중단: --where는 열=값 형식: {item}")
        rules.append(tuple(item.split("=", 1)))
    return rules


def number(text):
    text = (text or "").strip()
    if not text or "_" in text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_time(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for form in TIME_FORMATS:
        try:
            return datetime.strptime(text, form)
        except ValueError:
            pass
    return None


def percentile(sorted_values, q):
    """선형 보간 백분위수(numpy 기본값과 같은 방식)."""
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * q
    low = math.floor(pos)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def tidy(value):
    if value is None:
        return ""
    return float(f"{value:.10g}") if isinstance(value, float) else value


class Stat:
    def __init__(self):
        self.rows = self.blank = self.over = 0
        self.values = array("d")
        self.best_over = self.best_same = 0
        self.same_value = None
        self.first_time = self.last_time = ""
        self.new_segment()

    def new_segment(self):
        self.run_over = self.run_same = 0
        self.last = None

    def add(self, text, over, under, when):
        self.rows += 1
        if when is not None:
            self.first_time = self.first_time or when
            self.last_time = when
        value = number(text)
        if value is None:
            self.blank += 1
            self.new_segment()
            return
        self.values.append(value)
        out = (over is not None and value > over) or (under is not None and value < under)
        self.over += out
        self.run_over = self.run_over + 1 if out else 0
        self.best_over = max(self.best_over, self.run_over)
        self.run_same = self.run_same + 1 if value == self.last else 1
        self.last = value
        if self.run_same > self.best_same:
            self.best_same, self.same_value = self.run_same, value

    def row(self):
        ordered = sorted(self.values)
        n = len(ordered)
        mean = sum(ordered) / n if n else None
        std = math.sqrt(sum((v - mean) ** 2 for v in ordered) / (n - 1)) if n > 1 else None
        values = [self.rows, self.blank, n, ordered[0] if n else None, mean, std,
                  percentile(ordered, 0.5), percentile(ordered, 0.95), ordered[-1] if n else None,
                  self.over, self.best_over, self.best_same, self.same_value if self.best_same > 1 else None]
        return {k: tidy(v) for k, v in zip(STAT_COLUMNS, values)}


def open_csv(path, args):
    if not path.is_file():
        raise SystemExit(f"중단: 파일을 찾지 못함: {path}")
    stream = path.open("r", encoding=args.encoding, newline="")
    return stream, csv.DictReader(stream, delimiter=args.delimiter)


def decode_failed(path, args):
    return SystemExit(f"중단: {path}를 {args.encoding}로 읽지 못함. 한글 엑셀 CSV면 --encoding cp949로 다시 실행")


def summarize(args, argv):
    groups = [g for g in args.group.split(",") if g]
    segments = [g for g in args.segment.split(",") if g]
    values = list(dict.fromkeys(args.value))
    clash = [g for g in groups if g in STAT_COLUMNS + ["value_col", "first_time", "last_time"]]
    if clash:
        raise SystemExit(f"중단: 묶는 열 이름이 결과 열 이름과 겹침: {', '.join(clash)}")
    rules = parse_where(args.where)
    files = csv_files(args.source)
    stats, state, seen_keys = {}, {}, {}
    seen_rows = kept_rows = 0
    for path in files:
        stream, reader = open_csv(path, args)
        try:
            with stream:
                header = reader.fieldnames or []
                needed = groups + segments + values + [name for name, _ in rules] + ([args.time] if args.time else [])
                missing = [name for name in needed if name not in header]
                if missing:
                    raise SystemExit(f"중단: {path}에 없는 열: {', '.join(missing)} (있는 열: {', '.join(header)})")
                for record in reader:
                    seen_rows += 1
                    if any(record[name] != value for name, value in rules):
                        continue
                    key = tuple(record[g] for g in groups)
                    seen_keys.setdefault(key, 0)
                    segment = tuple(record[g] for g in segments)
                    if key not in state or state[key][0] != segment:
                        state[key] = [segment, 0]
                        for name in values:
                            if (key, name) in stats:
                                stats[(key, name)].new_segment()
                    state[key][1] += 1
                    if state[key][1] <= args.skip_first:
                        continue
                    kept_rows += 1
                    seen_keys[key] += 1
                    when = record[args.time] if args.time else None
                    for name in values:
                        stats.setdefault((key, name), Stat()).add(record[name], args.over, args.under, when)
        except UnicodeDecodeError:
            raise decode_failed(path, args) from None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    columns = groups + ["value_col"] + (["first_time", "last_time"] if args.time else []) + STAT_COLUMNS
    rows = []
    for (key, name), stat in stats.items():
        row = dict(zip(groups, key), value_col=name)
        if args.time:
            row.update(first_time=stat.first_time, last_time=stat.last_time)
        row.update(stat.row())
        rows.append(row)
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    emptied = [k for k, kept in seen_keys.items() if kept == 0]
    limit = " / ".join(x for x in [f"초과 기준 > {args.over}" if args.over is not None else "",
                                    f"미만 기준 < {args.under}" if args.under is not None else ""] if x) or "없음"
    note = [f"# {out.name}", "", "trace_tools.py가 만든 요약표다. 이 설명 파일과 표는 코드로만 다시 만든다.", "",
            f"- 만든 명령: `{command_line(argv)}`",
            f"- 원본: CSV {len(files)}개 ({', '.join(p.as_posix() for p in files[:5])}{' 외' if len(files) > 5 else ''})",
            f"- 읽은 행 {seen_rows}, 거른 뒤 쓴 행 {kept_rows}, 묶음 {len(seen_keys)}개, 요약 행 {len(rows)}",
            f"- 묶는 열: {', '.join(groups) or '없음(전체 하나)'} / 구간 열: {', '.join(segments) or '없음'} / 센서: {', '.join(values)} / 거르기: {', '.join(args.where) or '없음'}",
            (f"- 구간마다 앞 {args.skip_first}행을 뺌(스텝 전환 등). 그래서 통째로 빠진 묶음 {len(emptied)}개"
             + (f": {', '.join('/'.join(k) for k in emptied[:10])}{' 외' if len(emptied) > 10 else ''}" if emptied else ""))
            if args.skip_first else "- 앞부분을 빼지 않음",
            f"- 기준: {limit} (모든 센서에 같은 기준. 센서마다 기준이 다르면 따로 실행)",
            "- 열 뜻: value_col 센서 열 이름, rows 행 수, blank_or_text 빈 값·숫자 아님, n 숫자 값 수, p50·p95 백분위수, over_count 기준 밖 값 수,",
            "  longest_over_run 기준 밖이 연속된 최대 행 수, longest_same_run 같은 값이 연속된 최대 행 수(값이 붙는 센서 고장 후보), same_value 그 값(2행 이상일 때)",
            "- 연속 횟수는 파일 안 행 순서를 따르고, 빈 값이나 구간 열 값이 바뀌면 끊는다. 시간순이 아니면 먼저 정렬한 파일로 다시 만든다."]
    Path(str(out) + ".md").write_text("\n".join(note) + "\n", encoding="utf-8")
    print(f"요약 {len(rows)}행 → {out.as_posix()} (설명: {out.as_posix()}.md) / 읽은 행 {seen_rows}, 쓴 행 {kept_rows}"
          + (f", 앞부분 빼기로 사라진 묶음 {len(emptied)}개" if emptied else ""))
    return 0


RULES = {"1": "한 점이 3시그마 밖", "2": "연속 3점 중 2점이 같은 쪽 2시그마 밖", "3": "연속 5점 중 4점이 같은 쪽 1시그마 밖",
         "4": "연속 8점이 중심선의 같은 쪽", "5": "연속 6점이 계속 오르거나 계속 내림"}
SPC_COLUMNS = ["point", "value", "in_baseline", "baseline_excluded", "center", "sigma", "lcl", "ucl", "z", "rules", "signal"]


def ordering(texts):
    """순서 열을 비교할 방법을 정한다: 모두 숫자면 숫자, 모두 시각이면 시각, 하나도 아니면 글자. 섞여 있으면 멈춘다."""
    numbers = [number(t) is not None for t in texts]
    if all(numbers):
        return number, "숫자"
    times = [parse_time(t) for t in texts]
    timed = [t is not None for t in times]
    if all(timed):
        if len({t.tzinfo is not None for t in times}) > 1:
            raise SystemExit("중단: 시간대가 있는 시각과 없는 시각이 섞여 있음. 한쪽으로 맞춘 사본을 쓴다")
        return parse_time, "시각"
    if any(numbers) or any(timed):
        good = numbers if sum(numbers) >= sum(timed) else timed
        bad = list(dict.fromkeys(t for t, ok in zip(texts, good) if not ok))[:3]
        raise SystemExit("중단: 순서 열에 숫자·시각으로 읽을 수 없는 값이 섞여 있어 순서를 정할 수 없음(예: "
                         + ", ".join(map(repr, bad)) + "). 그 행을 고친 사본을 쓰거나 --where로 뺀다")
    return str, "글자"


def cutoff(text, convert, kind, sample):
    """--baseline-until 값을 순서 열과 같은 방법으로 읽는다. 시각 열에 날짜만 주면 그날 끝까지 포함한다."""
    value = convert(text)
    if value is None:
        raise SystemExit(f"중단: --baseline-until 값 {text!r}을 순서 열({kind})처럼 읽을 수 없음")
    whole_day = kind == "시각" and bool(re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text.strip()))
    if kind == "시각":
        if value.tzinfo is None and sample.tzinfo is not None:
            value = value.replace(tzinfo=sample.tzinfo)
        elif value.tzinfo is not None and sample.tzinfo is None:
            raise SystemExit("중단: 자료의 시각에는 시간대가 없는데 --baseline-until에는 있음. 시간대를 빼고 준다")
        if whole_day:
            value += timedelta(days=1)
    return value, whole_day


def rule_hits(z, chosen, k=3.0):
    """점마다 걸린 규칙 번호를 돌려준다. z는 (값-중심)/시그마, k는 관리 한계 폭(규칙 1)."""
    hits = [[] for _ in z]

    def side(i, width):  # 같은 쪽으로 width시그마 밖이면 +1 또는 -1
        return 1 if z[i] > width else (-1 if z[i] < -width else 0)

    run = trend = 0
    for i in range(len(z)):
        if "1" in chosen and side(i, k):
            hits[i].append("1")
        for rule, width, need, window in (("2", 2, 2, 3), ("3", 1, 4, 5)):
            now = side(i, width)
            if rule in chosen and now and sum(side(j, width) == now for j in range(max(0, i - window + 1), i + 1)) >= need:
                hits[i].append(rule)
        now = side(i, 0)
        run = run + 1 if now and i and side(i - 1, 0) == now else (1 if now else 0)
        if "4" in chosen and run >= 8:
            hits[i].append("4")
        step = 0 if i == 0 else (1 if z[i] > z[i - 1] else (-1 if z[i] < z[i - 1] else 0))
        before = 0 if i < 2 else (1 if z[i - 1] > z[i - 2] else (-1 if z[i - 1] < z[i - 2] else 0))
        trend = trend + 1 if step and step == before else (1 if step else 0)
        if "5" in chosen and trend >= 5:
            hits[i].append("5")
    return hits


def chance(k):
    """값이 종 모양 분포일 때 정상인 한 점이 우연히 ±k시그마 밖에 나올 확률을 글로 적는다."""
    p = math.erfc(k / math.sqrt(2))
    return f"점마다 {p * 100:.2g}%(평균 {1 / p:,.0f}점에 한 번)" if p > 0 else "거의 0"


def spc(args, argv):
    chosen = [] if args.rules.strip().lower() == "none" else [r for r in args.rules.split(",") if r]
    if any(r not in RULES for r in chosen):
        raise SystemExit(f"중단: --rules는 {','.join(RULES)} 중에서 쉼표로 고르거나 none")
    if args.ewma is not None and not 0 < args.ewma <= 1:
        raise SystemExit("중단: --ewma는 0보다 크고 1 이하(보통 0.1~0.3)")
    if args.baseline_until is not None and not args.order:
        raise SystemExit("중단: --baseline-until은 --order와 함께 쓴다")
    if args.usl is not None and args.lsl is not None and args.usl <= args.lsl:
        raise SystemExit("중단: 규격 상한(--usl)은 하한(--lsl)보다 커야 함")
    if args.k <= 0 or (args.stop_k is not None and args.stop_k <= args.k):
        raise SystemExit("중단: --k는 0보다 커야 하고, --stop-k는 --k보다 커야 함(경고 한계 안쪽, 정지 한계 바깥쪽)")
    if args.confirm is not None and args.confirm < 2:
        raise SystemExit("중단: --confirm은 2 이상(연속으로 한계 밖인 점 수)")
    k = args.k
    groups = [g for g in args.group.split(",") if g]
    has_spec = args.usl is not None or args.lsl is not None
    head = groups + ([args.order] if args.order else [])
    keep = [c for c in dict.fromkeys(args.keep) if c not in head]
    marks = chosen + (["C"] if args.confirm else []) + (["E"] if args.ewma else [])
    columns = (head + keep + SPC_COLUMNS[:-2] + [f"r{r}" for r in chosen] + (["rc"] if args.confirm else [])
               + (["ewma", "ewma_lcl", "ewma_ucl", "ewma_signal"] if args.ewma else [])
               + (["stop_lcl", "stop_ucl"] if args.stop_k else []) + (["out_of_spec"] if has_spec else [])
               + SPC_COLUMNS[-2:] + (["level"] if args.stop_k else []))
    clash = [c for c in head + keep if columns.count(c) > 1]
    if clash:
        raise SystemExit(f"중단: 묶는 열·순서 열·남길 열 이름이 서로 겹치거나 결과 열 이름과 겹침: {', '.join(dict.fromkeys(clash))}")
    where, skip = parse_where(args.where), parse_where(args.baseline_exclude)
    path = Path(args.source)
    stream, reader = open_csv(path, args)
    points, blank = {}, 0
    try:
        with stream:
            header = reader.fieldnames or []
            needed = head + keep + [args.value] + [name for name, _ in where + skip]
            missing = [name for name in needed if name not in header]
            if missing:
                raise SystemExit(f"중단: 없는 열: {', '.join(missing)} (있는 열: {', '.join(header)})")
            for r in reader:
                if any(r[name] != value for name, value in where):
                    continue
                value = number(r[args.value])
                order = (r[args.order] or "") if args.order else ""
                if value is None or (args.order and not order.strip()):
                    blank += 1
                    continue
                points.setdefault(tuple(r[g] or "" for g in groups), []).append(
                    (order, value, any(r[name] == text for name, text in skip), [r[c] or "" for c in keep]))
    except UnicodeDecodeError:
        raise decode_failed(path, args) from None
    if not points:
        raise SystemExit("중단: 거른 뒤 남은 숫자 값이 없음")
    convert, kind, until, whole_day = None, "파일 행 순서", None, False
    if args.order:
        convert, kind = ordering([p[0] for pts in points.values() for p in pts])
        if args.baseline_until is not None:
            sample = convert(next(iter(points.values()))[0][0])
            until, whole_day = cutoff(args.baseline_until, convert, kind, sample)
    capability = "Ppk" if args.sigma == "std" else "Cpk"
    kinds = ("base", "excluded", "check")
    rows, table, warnings = [], [], []
    for key, pts in points.items():
        keys = [convert(p[0]) for p in pts] if convert else None
        if keys:
            rank = sorted(range(len(pts)), key=lambda i: keys[i])
            pts, keys = [pts[i] for i in rank], [keys[i] for i in rank]
        n = len(pts)
        values = [p[1] for p in pts]
        if args.baseline_first:
            period = [i < args.baseline_first for i in range(n)]
        elif until is not None:
            period = [(key_ < until) if whole_day else (key_ <= until) for key_ in keys]
        else:
            period = [True] * n
        use = [period[i] and not pts[i][2] for i in range(n)]
        index = [i for i in range(n) if use[i]]
        base = [values[i] for i in index]
        moves = [abs(values[j] - values[i]) for i, j in zip(index, index[1:]) if j == i + 1]
        dropped = sum(period) - len(base)
        name = "/".join(part or "(빈 값)" for part in key).replace("|", "\\|") if groups else "전체"
        base_cell = f"{len(base)} (뺀 점 {dropped})" if dropped else str(len(base))
        heads = [dict(zip(head + keep, [*key, *([pts[i][0]] if args.order else []), *pts[i][3]]), point=i + 1, value=tidy(values[i]),
                      in_baseline=int(use[i]), baseline_excluded=int(period[i] and not use[i])) for i in range(n)]
        if len(base) < 2 or (args.sigma == "mr" and not moves):
            table.append(" | ".join([f"| {name}", base_cell, str(n), "기준으로 쓸 이웃한 점이 모자라 계산하지 않음",
                                     *["—"] * (9 if args.stop_k else 8)]) + " |")
            rows.extend(heads)
            continue
        center = sum(base) / len(base)
        spread = math.sqrt(sum((v - center) ** 2 for v in base) / (len(base) - 1))
        sigma = spread if args.sigma == "std" else sum(moves) / len(moves) / 1.128
        lcl, ucl = center - k * sigma, center + k * sigma
        if sigma > 0:
            z = [(v - center) / sigma for v in values]
            hits = rule_hits(z, chosen, k)
            beyond = [1 if x > k else (-1 if x < -k else 0) for x in z]
            stops = [args.stop_k is not None and abs(x) > args.stop_k for x in z]
        else:
            z = [None] * n
            beyond = [1 if v > center else (-1 if v < center else 0) for v in values]
            hits = [["1"] if "1" in chosen and b else [] for b in beyond]
            stops = [False] * n
            warnings.append(f"- {name}: 시그마가 0이다(기준으로 쓴 값이 모두 같음). 중심과 다른 점만 한계 밖으로 표시했고 규칙 2~5와 정지 단계는 계산하지 않았다. 값이 붙은 센서인지, 기준 기간이 맞는지 확인한다.")
        if len(base) < 20:
            warnings.append(f"- {name}: 기준으로 쓴 점이 {len(base)}개뿐이라 한계가 불안정하다(보통 20점 이상을 쓴다).")
        smooth, streak = center, 0
        counts = {(r, where_is): 0 for r in marks for where_is in kinds}
        signals, halted = dict.fromkeys(kinds, 0), dict.fromkeys(kinds, 0)
        first = ""
        for i, row in enumerate(heads):
            v = values[i]
            where_is = "base" if use[i] else ("excluded" if period[i] else "check")
            row.update(center=tidy(center), sigma=tidy(sigma), lcl=tidy(lcl), ucl=tidy(ucl), z=tidy(z[i]))
            fired = list(hits[i])
            row.update({f"r{r}": int(r in hits[i]) for r in chosen})
            if args.confirm:
                streak = streak + 1 if beyond[i] and i and beyond[i - 1] == beyond[i] else (1 if beyond[i] else 0)
                row["rc"] = int(streak >= args.confirm)
                if row["rc"]:
                    fired.append("C")
            if args.ewma:
                lam = args.ewma
                smooth = lam * v + (1 - lam) * smooth
                width = k * sigma * math.sqrt(lam / (2 - lam) * (1 - (1 - lam) ** (2 * (i + 1))))
                out = abs(smooth - center) > width + 1e-12 * max(1.0, abs(center))
                row.update(ewma=tidy(smooth), ewma_lcl=tidy(center - width), ewma_ucl=tidy(center + width), ewma_signal=int(out))
                if out:
                    fired.append("E")
            if has_spec:
                row["out_of_spec"] = int((args.usl is not None and v > args.usl) or (args.lsl is not None and v < args.lsl))
            row.update(rules=";".join(fired), signal=int(bool(fired)))
            if args.stop_k:
                row.update(stop_lcl=tidy(center - args.stop_k * sigma), stop_ucl=tidy(center + args.stop_k * sigma),
                           level="stop" if stops[i] else ("warn" if fired else ""))
                halted[where_is] += stops[i]
            for r in fired:
                counts[(r, where_is)] += 1
            if fired:
                signals[where_is] += 1
                if where_is == "check" and not first:
                    first = pts[i][0] or str(i + 1)
            rows.append(row)
        total = sum((v - center) ** 2 for v in base)
        skew = sum(((v - center) / spread) ** 3 for v in base) / len(base) if spread > 0 else 0.0
        lag = sum((values[i] - center) * (values[j] - center) for i, j in zip(index, index[1:]) if j == i + 1) / total if total > 0 else 0.0
        sides = [(args.usl - center) / (3 * sigma) if args.usl is not None and sigma > 0 else None,
                 (center - args.lsl) / (3 * sigma) if args.lsl is not None and sigma > 0 else None]
        cpk = min((s for s in sides if s is not None), default=None)
        cells = [name, base_cell, str(n), label(center), label(sigma), label(lcl), label(ucl),
                 " / ".join(str(signals[w]) for w in kinds), first or "없음",
                 ", ".join(f"{r}: {counts[(r, 'base')]}/{counts[(r, 'check')]}" for r in marks) or "—",
                 *([" / ".join(str(halted[w]) for w in kinds)] if args.stop_k else []),
                 f"{skew:.2f} / {lag:.2f}", "" if cpk is None else f"{cpk:.2f}"]
        table.append("| " + " | ".join(cells) + " |")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)
    if args.baseline_first:
        basis = f"묶음마다 순서상 앞 {args.baseline_first}점"
    elif until is not None:
        basis = f"{args.order} 값이 {args.baseline_until} 이하인 점" + ("(날짜만 줘서 그날 끝까지 포함)" if whole_day else "")
    else:
        basis = "모든 점(기준 기간과 확인 기간을 나누지 않음. 규칙을 평가하려면 --baseline-first나 --baseline-until로 나눈다)"
    titles = ["묶음", "기준 점 수", "전체 점 수", "중심", "시그마", "아래 한계", "위 한계", "신호 점 수(기준 / 뺀 점 / 확인 기간)",
              "확인 기간 첫 신호", "규칙별 신호(기준/확인)", *(["정지 점 수(기준 / 뺀 점 / 확인 기간)"] if args.stop_k else []),
              "치우침 / 앞뒤 닮음", capability]
    note = [f"# {out.name}", "", "trace_tools.py spc가 만든 관리도 계산표다. 이 설명 파일과 표는 코드로만 다시 만든다.", "",
            f"- 만든 명령: `{command_line(argv)}`",
            f"- 원본: {path.as_posix()} / 값 열: {args.value} / 묶는 열: {', '.join(groups) or '없음'} / 거르기: {', '.join(args.where) or '없음'}",
            f"- 순서: {args.order or '파일 행 순서'}" + (f" ({kind}로 읽어 정렬. 값이 같으면 파일 행 순서)" if args.order else ""),
            f"- 쓴 점 {len(rows)}개, 값이나 순서가 비어서 뺀 행 {blank}개",
            f"- 기준 기간(중심선과 관리 한계를 계산하는 점): {basis}",
            f"- 기준 기간에서 뺀 점: {', '.join(args.baseline_exclude) or '없음'} (한계 계산에서만 빼고 관리도에는 남긴다)",
            "- 시그마: " + ("기준 점의 표준편차" if args.sigma == "std" else "기준 점 중 바로 이웃한 두 점 차이의 평균 ÷ 1.128 (서서히 움직이는 변화에 덜 부풀려짐)"),
            f"- 관리 한계(경고): 중심 ± {k:g}시그마. 정상인 점이 우연히 벗어날 확률은 {chance(k)}. 규격 한계(제품이 지켜야 하는 값)와 다르다.",
            *([f"- 정지 한계: 중심 ± {args.stop_k:g}시그마. 우연히 벗어날 확률은 {chance(args.stop_k)}. level 열이 stop이면 정지 한계 밖, warn이면 그 밖의 신호"] if args.stop_k else []),
            *[f"- 규칙 {r}: {RULES[r]}" + (f" → 여기서는 {k:g}시그마 밖" if r == "1" and k != 3 else "") for r in chosen],
            *([f"- 규칙 C: 연속 {args.confirm}점이 같은 쪽 관리 한계 밖(한 번 튄 것은 넘기고 이어질 때만 신호)"] if args.confirm else []),
            *([f"- 규칙 E: EWMA(최근 값에 가중치 {args.ewma}를 준 이동 평균)가 자기 한계 밖. 작은 변화가 이어질 때 잡는다"] if args.ewma else []),
            *([f"- 규격: 상한 {args.usl}, 하한 {args.lsl}. {capability} = 중심에서 가까운 규격까지 거리 ÷ 3시그마(기준 점 기준)"] if has_spec else []),
            "", "| " + " | ".join(titles) + " |", "|" + "---|" * len(titles), *table, "", *warnings,
            "- 기준 점의 신호 수는 한계를 그 점들로 맞췄기 때문에 작게 나온다. 오경보는 확인 기간의 정상 점에서 난 신호로, 놓침은 확인 기간의 알려진 이상에 신호가 없었던 것으로 따로 센다.",
            "- 우연히 벗어날 확률은 값이 종 모양 분포에 가깝고 앞뒤 점이 서로 독립일 때의 계산이다. 치우침이 ±1을 넘거나 앞뒤 닮음(바로 앞 점과의 상관)이 0.5를 넘으면 실제 오경보는 더 많을 수 있다. 관리도가 여러 개면 우연한 신호도 그 수만큼 늘어난다.",
            "- 열 뜻: point 묶음 안 순번, in_baseline 한계 계산에 쓴 점이면 1, baseline_excluded 기준 기간이지만 뺀 점이면 1, z (값-중심)÷시그마, r1~r5·rc 규칙별 신호, rules 걸린 규칙, signal 하나라도 걸리면 1"]
    Path(str(out) + ".md").write_text("\n".join(note) + "\n", encoding="utf-8")
    flagged = sum(1 for r in rows if r.get("signal"))
    print(f"관리도 {len(rows)}점(묶음 {len(points)}개) → {out.as_posix()} (설명과 묶음별 한계: {out.as_posix()}.md) / 신호 {flagged}점, 뺀 행 {blank}"
          + (f", 확인할 점 {len(warnings)}건은 .md에" if warnings else ""))
    return 0


def axis(texts):
    """가로축 종류를 정하고 값을 숫자로 바꾼다. 시각은 첫 값의 시간대로 표시한다."""
    if all(number(t) is not None for t in texts):
        return "number", [number(t) for t in texts], None, None
    times = [parse_time(t) for t in texts]
    if all(t is not None for t in times):
        aware = {t.tzinfo is not None for t in times}
        if len(aware) > 1:
            raise SystemExit("중단: 시간대가 있는 시각과 없는 시각이 섞여 있음. 한쪽으로 맞춘 사본으로 그린다")
        zone = times[0].tzinfo or timezone.utc
        return "time", [(t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp() for t in times], zone, None
    order = {}
    for t in texts:
        order.setdefault(t, float(len(order)))
    return "category", [order[t] for t in texts], None, list(order)


def thin(points, limit):
    """점이 많으면 구간마다 최솟값·최댓값 점만 남겨 튐을 잃지 않는다."""
    if len(points) <= limit:
        return points
    size = math.ceil(len(points) / (limit // 2))
    kept = []
    for start in range(0, len(points), size):
        chunk = points[start:start + size]
        low = min(range(len(chunk)), key=lambda i: chunk[i][1])
        high = max(range(len(chunk)), key=lambda i: chunk[i][1])
        kept.extend(chunk[i] for i in sorted({low, high}))
    return kept


def nice_ticks(low, high, count=5):
    if low == high:
        low, high = low - 1, high + 1
    step = 10 ** math.floor(math.log10((high - low) / count))
    for factor in (1, 2, 2.5, 5, 10):
        if (high - low) / (step * factor) <= count:
            step *= factor
            break
    digits = max(0, 2 - math.floor(math.log10(step)))
    first, last = math.floor(low / step + 1e-9), math.ceil(high / step - 1e-9)
    return [round(i * step, digits) for i in range(first, last + 1)]


def label(value):
    return f"{value:.4g}" if abs(value) < 1e5 else f"{value:.3e}"


def time_format(span):
    return "%H:%M:%S" if span < 3600 else ("%m-%d %H:%M" if span < 30 * 86400 else "%Y-%m-%d")


def plot(args, argv):
    rules = parse_where(args.where)
    ys = list(dict.fromkeys(args.y))
    path = Path(args.source)
    stream, reader = open_csv(path, args)
    kept, blank_x = [], 0
    try:
        with stream:
            header = reader.fieldnames or []
            needed = [args.x] + ys + ([args.series] if args.series else []) + [name for name, _ in rules]
            missing = [name for name in needed if name not in header]
            if missing:
                raise SystemExit(f"중단: 없는 열: {', '.join(missing)} (있는 열: {', '.join(header)})")
            for r in reader:
                if any(r[name] != value for name, value in rules):
                    continue
                if not (r[args.x] or "").strip():
                    blank_x += 1
                    continue
                kept.append((r[args.x], r[args.series] if args.series else "", [r[name] for name in ys]))
    except UnicodeDecodeError:
        raise decode_failed(path, args) from None
    if not kept:
        raise SystemExit("중단: 거른 뒤 남은 행이 없음")
    kind, xs, zone, categories = axis([k[0] for k in kept])
    series, skipped = {}, 0
    for (_, group, texts), x in zip(kept, xs):
        for name, text in zip(ys, texts):
            key = " / ".join(part for part in [group, name if len(ys) > 1 else ""] if part) or name
            y = number(text)
            if y is None:
                skipped += 1
                continue
            series.setdefault(key, []).append((x, y))
    if not series:
        raise SystemExit("중단: 숫자인 y 값이 없음")
    if len(series) > MAX_SERIES:
        raise SystemExit(f"중단: 선이 {len(series)}개라 색으로 구분할 수 없음(최대 {MAX_SERIES}). --where로 나눠 여러 그림으로 그린다")
    for key in series:
        series[key] = thin(sorted(series[key]), args.max_points)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    title = args.title or f"{', '.join(ys)} vs {args.x}" + (f" ({', '.join(args.where)})" if args.where else "")
    if out.suffix.lower() == ".png":
        png(series, kind, zone, categories, title, ys, args, out)
    else:
        out.write_text(svg(series, kind, zone, categories, title, ys, args), encoding="utf-8")
    total = sum(len(p) for p in series.values())
    notes = [f"숫자 아닌 y {skipped}개 뺌" if skipped else "", f"가로축 빈 행 {blank_x}개 뺌" if blank_x else "",
             f"가로축은 글자 순서(시각·숫자로 못 읽음)" if kind == "category" else ""]
    print(f"그림 → {out.as_posix()} / 선 {len(series)}개, 점 {total}개" + "".join(f", {n}" for n in notes if n))
    return 0


def png(series, kind, zone, categories, title, ys, args, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise SystemExit("중단: matplotlib이 없어 PNG를 못 만듦. --out을 .svg로 바꾼다") from None
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for i, (key, points) in enumerate(series.items()):
        px = [datetime.fromtimestamp(p[0], zone) if kind == "time" else p[0] for p in points]
        style = dict(marker="o", linestyle="none", markersize=3) if args.kind == "scatter" else dict(linewidth=1.5)
        ax.plot(px, [p[1] for p in points], color=PALETTE[i], label=key, **style)
    for value in args.hline:
        ax.axhline(value, color="#52514e", linestyle="--", linewidth=1)
    if categories:
        step = max(1, math.ceil(len(categories) / 12))
        ax.set_xticks(list(range(len(categories)))[::step])
        ax.set_xticklabels(categories[::step], rotation=45, ha="right")
    ax.set_title(title)
    ax.set_xlabel(args.x)
    ax.set_ylabel(", ".join(ys))
    ax.grid(alpha=0.25)
    if len(series) > 1:
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=120)


def svg(series, kind, zone, categories, title, ys, args):
    width, height, left, right, top, bottom = 960, 440, 70, 190, 40, 70
    plot_w, plot_h = width - left - right, height - top - bottom
    xs = [p[0] for pts in series.values() for p in pts]
    values = [p[1] for pts in series.values() for p in pts] + list(args.hline)
    x_low, x_high = min(xs), max(xs)
    if x_low == x_high:
        x_low, x_high = x_low - 1, x_high + 1
    y_ticks = nice_ticks(min(values), max(values))
    y_low, y_high = min(y_ticks[0], min(values)), max(y_ticks[-1], max(values))

    def sx(x):
        return left + (x - x_low) / (x_high - x_low) * plot_w

    def sy(y):
        return top + (1 - (y - y_low) / (y_high - y_low)) * plot_h

    def e(text):
        return html.escape(CONTROL.sub("", str(text)))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">',
             f'<rect width="{width}" height="{height}" fill="#fcfcfb"/>',
             f'<text x="{left}" y="24" font-size="15" fill="#0b0b0b">{e(title)}</text>']
    for t in y_ticks:
        y = sy(t)
        parts.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e4e3df"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#52514e">{e(label(t))}</text>')
    if categories:
        step = max(1, math.ceil(len(categories) / 12))
        x_ticks = [(float(i), c) for i, c in enumerate(categories)][::step]
    elif kind == "time":
        form = time_format(x_high - x_low)
        x_ticks = [(x_low + (x_high - x_low) * i / 5) for i in range(6)]
        x_ticks = [(x, datetime.fromtimestamp(x, zone).strftime(form)) for x in x_ticks]
    else:
        x_ticks = [(t, label(t)) for t in nice_ticks(x_low, x_high) if x_low <= t <= x_high]
    for x, text in x_ticks:
        px = sx(x)
        parts.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{top + plot_h}" y2="{top + plot_h + 5}" stroke="#52514e"/>')
        parts.append(f'<text x="{px:.1f}" y="{top + plot_h + 18}" text-anchor="end" transform="rotate(-30 {px:.1f} {top + plot_h + 18})" fill="#52514e">{e(text)}</text>')
    parts.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{top + plot_h}" y2="{top + plot_h}" stroke="#52514e"/>')
    parts.append(f'<text x="{left + plot_w / 2}" y="{height - 8}" text-anchor="middle" fill="#52514e">{e(args.x)}</text>')
    parts.append(f'<text x="16" y="{top + plot_h / 2}" text-anchor="middle" transform="rotate(-90 16 {top + plot_h / 2})" fill="#52514e">{e(", ".join(ys))}</text>')
    for value in args.hline:
        y = sy(value)
        parts.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="#52514e" stroke-dasharray="6 4"><title>기준 {e(label(value))}</title></line>')
        parts.append(f'<text x="{left + plot_w - 4}" y="{y - 4:.1f}" text-anchor="end" fill="#52514e">기준 {e(label(value))}</text>')
    for i, (key, points) in enumerate(series.items()):
        color = PALETTE[i]
        if args.kind == "scatter" or len(points) == 1:
            for x, y in points:
                parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3" fill="{color}"><title>{e(key)}: {e(label(y))}</title></circle>')
        else:
            coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
            parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"><title>{e(key)}</title></polyline>')
        if len(series) > 1:
            ly = top + 10 + i * 20
            parts.append(f'<rect x="{left + plot_w + 16}" y="{ly - 8}" width="14" height="4" rx="2" fill="{color}"/>')
            parts.append(f'<text x="{left + plot_w + 36}" y="{ly}" fill="#0b0b0b">{e(key[:22])}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("summarize", help="묶음(예: 챔버×웨이퍼×스텝)마다 센서 값 요약표를 CSV로 만든다")
    s.add_argument("--source", action="append", required=True, help="CSV 파일 또는 폴더(아래 .csv 모두, 파일 이름의 숫자 순). 여러 번 지정 가능")
    s.add_argument("--group", default="", help="묶을 열 이름을 쉼표로. 예: chamber,wafer_id,step")
    s.add_argument("--segment", default="", help="이 열 값이 바뀌면 같은 묶음 안에서도 새 구간으로 보고 앞부분 빼기·연속 횟수를 새로 셈. 예: wafer_id")
    s.add_argument("--value", action="append", required=True, help="요약할 센서 열. 여러 번 지정 가능")
    s.add_argument("--over", type=float, help="이 값보다 크면 기준 밖으로 셈")
    s.add_argument("--under", type=float, help="이 값보다 작으면 기준 밖으로 셈")
    s.add_argument("--where", action="append", default=[], help="열=값 인 행만 씀. 여러 번 지정하면 모두 만족")
    s.add_argument("--skip-first", type=int, default=0, help="구간마다 앞의 N행을 뺌(스텝 시작 전환 구간)")
    s.add_argument("--time", help="시각 열. 지정하면 묶음의 처음·마지막 시각을 적음")
    s.add_argument("--out", required=True, help="요약 CSV 경로. 옆에 같은 이름 .md 설명 파일을 만든다")
    p = sub.add_parser("plot", help="CSV 하나를 선·점 그래프(SVG, matplotlib이 있으면 PNG도)로 그린다")
    p.add_argument("--source", required=True, help="CSV 파일 하나(원본 또는 summarize 결과)")
    p.add_argument("--x", required=True, help="가로축 열. 숫자·시각·글자 모두 가능. 빈 칸 행은 뺌")
    p.add_argument("--y", action="append", required=True, help="세로축 열. 여러 번 지정하면 선이 늘어남")
    p.add_argument("--series", help="이 열 값마다 선을 나눔. 예: chamber")
    p.add_argument("--where", action="append", default=[], help="열=값 인 행만 그림")
    p.add_argument("--hline", type=float, action="append", default=[], help="기준선(허용치 등)")
    p.add_argument("--kind", choices=["line", "scatter"], default="line")
    p.add_argument("--max-points", type=int, default=4000, help="선마다 이보다 많으면 구간 최솟값·최댓값만 남김")
    p.add_argument("--title")
    p.add_argument("--out", required=True, help=".svg(기본) 또는 .png(matplotlib 필요)")
    c = sub.add_parser("spc", help="관리도(SPC): 기준 기간으로 중심선·관리 한계를 계산하고 점마다 규칙 신호를 표시한다")
    c.add_argument("--source", required=True, help="CSV 파일 하나. 한 행이 한 점(웨이퍼·로트 측정값이나 summarize 결과)")
    c.add_argument("--value", required=True, help="관리할 값의 열")
    c.add_argument("--group", default="", help="관리도를 따로 그릴 묶음 열을 쉼표로. 예: chamber 또는 chamber,step")
    c.add_argument("--order", help="점의 순서를 정하는 열(시각·순번). 없으면 파일 행 순서")
    c.add_argument("--where", action="append", default=[], help="열=값 인 행만 씀")
    c.add_argument("--baseline-first", type=int, default=0, help="묶음마다 앞의 N점을 기준 기간으로 씀")
    c.add_argument("--baseline-until", help="순서 열 값이 이 값 이하인 점을 기준 기간으로 씀. 날짜만 주면 그날 끝까지. 예: 2026-10-24")
    c.add_argument("--baseline-exclude", action="append", default=[],
                   help="열=값 인 점은 한계 계산에서만 뺌(관리도에는 남김). 점검 직후·확인된 이상 점에 표시 열을 붙인 사본과 함께 쓴다. 여러 번 지정하면 하나라도 맞는 점을 뺌")
    c.add_argument("--sigma", choices=["mr", "std"], default="mr", help="mr(기본): 이웃한 두 점 차이로 추정. std: 표준편차")
    c.add_argument("--k", type=float, default=3.0, help="관리 한계 폭(시그마의 몇 배). 기본 3. 3~6을 바꿔 가며 오경보와 놓침을 비교한다")
    c.add_argument("--stop-k", type=float, help="정지 한계 폭(--k보다 큼). 주면 level 열에 warn/stop 두 단계를 적음. 예: --k 3 --stop-k 5")
    c.add_argument("--confirm", type=int, help="N: 연속 N점이 같은 쪽 관리 한계 밖일 때만 신호(규칙 C). 한 번 튄 것을 넘기려면 --rules none --confirm 2")
    c.add_argument("--keep", action="append", default=[], help="결과에 그대로 남길 열(웨이퍼·로트 식별값 등). 다른 자료와 맞춰 볼 때 쓴다. 여러 번 지정 가능")
    c.add_argument("--rules", default="1,2,3,4,5", help="쓸 규칙 번호를 쉼표로, 없으면 none. 규칙 2·3은 --k와 상관없이 2·1시그마 기준. " + " / ".join(f"{k} {v}" for k, v in RULES.items()))
    c.add_argument("--ewma", type=float, help="예: 0.2. 주면 EWMA(가중 이동 평균) 관리도와 규칙 E도 계산")
    c.add_argument("--usl", type=float, help="규격 상한. 주면 규격 밖 여부와 Cpk를 계산")
    c.add_argument("--lsl", type=float, help="규격 하한")
    c.add_argument("--out", required=True, help="점별 결과 CSV 경로. 옆에 묶음별 한계와 신호 수를 적은 .md를 만든다")
    for sp in (s, p, c):
        sp.add_argument("--encoding", default="utf-8-sig", help="기본 utf-8-sig. 한글 엑셀 CSV는 cp949")
        sp.add_argument("--delimiter", default=",", help="한 글자. 탭은 $'\\t'")
    args = parser.parse_args(argv)
    if len(args.delimiter) != 1 or args.delimiter in "\r\n\"":
        parser.error("구분자는 줄바꿈·큰따옴표가 아닌 한 글자여야 함")
    try:
        "".encode(args.encoding)
    except LookupError:
        parser.error("알 수 없는 인코딩")
    if args.command == "summarize":
        if args.skip_first < 0:
            parser.error("--skip-first는 0 이상")
        return summarize(args, argv)
    if args.command == "spc":
        if args.baseline_first < 0 or (args.baseline_first and args.baseline_until is not None):
            parser.error("--baseline-first(1 이상)와 --baseline-until 중 하나만 쓴다")
        return spc(args, argv)
    if args.max_points < 4:
        parser.error("--max-points는 4 이상")
    return plot(args, argv)


if __name__ == "__main__":
    sys.exit(main())
