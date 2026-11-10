#!/usr/bin/env python3
"""지정한 원본의 목록과 CSV 표를 만든다. Python 3.8+ 표준 라이브러리만 쓴다."""

import argparse
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unicodedata
from urllib.parse import quote


OWNER = "hykitf.make_data_guide.v1"
OUTPUT = Path("docs/data-guide/_generated")
KIT_FILES = {"AGENTS.md", "apply.sh", "make_data_guide.py", "trace_tools.py", ".gitignore", ".gitattributes"}
KIT_DOCS = {"PLAN.md", "QUESTIONS.md", "CAUSES.md", "STATUS.md", "WORKLOG.md", "HOW-IT-WORKS.md", "HANDOFF.md"}
CACHES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
AUTO_ENCODINGS = ("utf-8-sig", "cp949")  # 한글 엑셀에서 저장한 CSV는 보통 CP949다.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")  # Markdown 표로 되살릴 수 없는 제어 문자(탭·줄바꿈 제외)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def has_link(path):
    return any(linked(p) for p in (path, *path.parents))


def shown(text):
    """파일 이름의 깨진 바이트를 \\xNN 글자로 바꿔 기록·저장 중에 멈추지 않게 한다."""
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "surrogatepass")
    return data.decode("utf-8", "backslashreplace")


def relative(path, root):
    return shown(Path(os.path.relpath(path, root)).as_posix())


def excluded(path, root, given=False):
    if any(part in CACHES for part in path.parts):
        return True
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    if len(parts) == 1 and parts[0] in KIT_FILES:
        return True
    # 작업 폴더 바로 아래 analysis·tests·out·logs는 훑을 때만 뺀다. --source로 직접 지정하면 읽는다.
    if not given and len(parts) == 1 and parts[0] in {"analysis", "tests", "out", "logs"}:
        return True
    return len(parts) >= 2 and parts[0] == "docs" and (parts[1] in {"data-guide", "guide"} or parts[1] in KIT_DOCS)


def discover(sources, root):
    records, paths, seen = [], [], set()
    # 직접 지정한 원본은 링크여도 따라간다. 폴더를 훑다가 만난 링크는 읽지 않는다.
    pending = [(Path(os.path.abspath(s)), True) for s in sources]
    while pending:
        path, given = pending.pop()
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            name = relative(path, root)
        except ValueError:
            records.append({"path": shown(path.as_posix()), "status": "error", "reason": "작업 폴더와 다른 드라이브는 지원하지 않음"})
            continue
        if not given and linked(path):
            records.append({"path": name, "status": "skipped", "reason": "심볼릭 링크·정션은 읽지 않음"})
        elif excluded(path, root, given):
            records.append({"path": name, "status": "skipped", "reason": "키트·생성물·테스트·캐시 제외"})
        else:
            try:
                if path.is_dir():
                    pending.extend((child, False) for child in sorted(path.iterdir(), reverse=True))
                elif path.is_file():
                    paths.append(path)
                else:
                    records.append({"path": name, "status": "error", "reason": "파일이 없거나 일반 파일이 아님"})
            except OSError:
                records.append({"path": name, "status": "error", "reason": "파일 목록을 읽지 못함"})
    return sorted(paths), records


class DataError(Exception):
    def __init__(self, reason, skipped=False):
        self.reason, self.skipped = reason, skipped


def read_csv(path, args):
    with path.open("rb") as stream:
        raw = stream.read(args.max_bytes + 1)
    if len(raw) > args.max_bytes:
        raise DataError("크기 제한 초과: --max-bytes 범위를 확인", True)
    for encoding in (AUTO_ENCODINGS if args.encoding == "auto" else (args.encoding,)):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            pass
    else:
        raise DataError("문자 인코딩을 읽지 못함: --encoding 확인")
    notes = []
    if args.encoding == "auto" and encoding != AUTO_ENCODINGS[0]:
        notes.append("UTF-8이 아니어서 CP949로 읽음: 한글이 깨지지 않았는지 확인")
    csv.field_size_limit(args.max_bytes)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=args.delimiter, strict=True)
    try:
        header = next(reader, None)
        if not header:
            raise DataError("빈 CSV 또는 머리글 없음")
        blank = [i for i, name in enumerate(header) if not name.strip()]
        for number, i in enumerate(blank, 1):
            header[i] = f"(이름 없음 {number})"
        if blank:
            notes.append(f"이름이 빈 열 {len(blank)}개를 (이름 없음 N)으로 표시")
        repeated = sorted({name for name in header if header.count(name) > 1})
        if repeated:
            notes.append("같은 열 이름이 여러 번 나옴(가리면 모두 가림): " + ", ".join(repeated))
        rows, blank_lines = [], 0
        for row in reader:
            if not row:
                blank_lines += 1
                continue
            if len(rows) >= args.max_rows:
                raise DataError("행 수 제한 초과: --max-rows 범위를 확인", True)
            if len(row) != len(header):
                raise DataError(f"기록 {len(rows) + 1}(물리 줄 {reader.line_num}): 머리글과 열 수가 다름")
            rows.append(row)
    except csv.Error:
        raise DataError(f"CSV 문법 오류: 물리 줄 {reader.line_num}") from None
    if blank_lines:
        notes.append(f"빈 줄 {blank_lines}개는 기록으로 세지 않음" + ("(열이 하나라 빈 값일 수 있음)" if len(header) == 1 else ""))
    for number, row in enumerate([header, *rows]):
        found = next((m for m in map(CONTROL.search, row) if m), None)
        if found:
            where = "머리글" if number == 0 else f"기록 {number}"
            raise DataError(f"{where}: 제어 문자 U+{ord(found.group()):04X}는 표로 옮기지 않음")
    return raw, encoding, header, rows, notes


def profile(path, root, args):
    record = {"path": relative(path, root), "status": "unsupported", "tables": []}
    try:
        record["bytes"] = path.stat().st_size
        if path.suffix.lower() != ".csv":
            record["reason"] = "CSV 외 형식: 내용은 별도로 확인"
            return record
        raw, encoding, header, rows, notes = read_csv(path, args)
        seen = set()
        duplicates = 0
        for row in rows:
            key = tuple(row)
            duplicates += key in seen
            seen.add(key)
        record.update(status="inventoried", encoding=encoding, delimiter=args.delimiter, notes=notes,
                      columns=header, rows=len(rows), sha256=digest(raw),
                      empty_counts=[sum(row[i] == "" for row in rows) for i in range(len(header))],
                      duplicate_rows=duplicates,
                      masked_columns=[name for name in header if name in args.mask_column])
    except DataError as error:
        record.update(status="skipped" if error.skipped else "error", reason=error.reason, incomplete=True)
    except OSError:
        record.update(status="error", reason="파일을 읽지 못함")
    return record


HEAD_BYTES = 65536
DATE_LIKE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}:\d{2}")
NUMBER_LIKE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def value_like(field):
    """열 이름이 아니라 값처럼 보이는 칸: 날짜·시각이 들어 있거나, 흔한 구분 기호로 나눈 조각 중 숫자만 있는 것이 있다."""
    return bool(DATE_LIKE.search(field)) or any(NUMBER_LIKE.fullmatch(part.strip()) for part in re.split(r"[,;\t|]", field))


def listed(path, root, args):
    """--list-only 원본: 크기와 CSV 첫 줄의 열 이름만 적고 값은 읽지 않는다.
    첫 줄이 머리글이 아닌 것 같으면(숫자·날짜 칸, 빈 칸) 이름 대신 칸 수만 적는다."""
    record = {"path": relative(path, root), "status": "listed", "tables": [], "reason": "목록만(--list-only): 표를 만들지 않음"}
    try:
        record["bytes"] = path.stat().st_size
        if path.suffix.lower() != ".csv":
            return record
        with path.open("rb") as stream:
            head = stream.read(HEAD_BYTES)
        text = None
        for encoding in (AUTO_ENCODINGS if args.encoding == "auto" else (args.encoding,)):
            try:
                text = head.decode(encoding)
                break
            except UnicodeDecodeError as error:
                if len(head) == HEAD_BYTES and error.start > len(head) - 4:  # 읽은 끝에서 글자가 잘린 경우
                    text = head[:error.start].decode(encoding)
                    break
        lines = (text or "").splitlines()
        if not lines or (len(head) == HEAD_BYTES and len(lines) < 2):
            record["header_note"] = "첫 줄을 읽지 못함(인코딩 또는 64KB보다 긴 줄)"
            return record
        fields = next(csv.reader([lines[0]], delimiter=args.delimiter), [])
        if not fields or any(not f.strip() or f.lstrip().startswith("#") or len(f) > 64 or value_like(f) or CONTROL.search(f) for f in fields):
            record["header_note"] = f"칸 {len(fields)}개. 첫 줄이 열 이름이 아닐 수 있어 내용을 적지 않음"
        else:
            record["header"] = fields
    except OSError:
        record.update(status="error", reason="파일을 읽지 못함")
    return record


def under(path, roots):
    return any(path == r or r in path.parents for r in roots)


def cell(value):
    # 표 칸 구분·HTML·Markdown 문법이 되는 글자와 앞뒤 공백·제어 문자만 바꾼다.
    # 글자 사이 공백과 단어 안 밑줄은 그대로 두어 미리보기 없이 열어도 읽히게 한다.
    last = len(value) - 1
    out = []
    for i, ch in enumerate(value):
        inner = 0 < i < last
        if ch == "\n":
            out.append("<br>")
        elif (ch in "&<>|`*[]!\\~$" or unicodedata.category(ch) == "Cc"
              or (ch.isspace() and (ch != " " or not inner))
              or (ch == "_" and not (inner and value[i - 1].isalnum() and value[i + 1].isalnum()))):
            out.append(f"&#{ord(ch)};")
        else:
            out.append(ch)
    return "".join(out)


def read_saved_table(path):
    """실제 저장한 Markdown을 파싱한다. 생성기의 cell 함수를 재사용하지 않는다."""
    lines = path.read_text(encoding="utf-8").splitlines()
    table = [line for line in lines if line.startswith("| ")]
    if len(table) < 2:
        raise DataError("변환 확인 실패: 표 머리글 없음")
    decoded = []
    for line in [table[0], *table[2:]]:
        parts = line[1:-1].split("|")
        decoded.append([html.unescape(part.strip().replace("<br>", "\n")) for part in parts])
    return decoded


def save_text(stage, name, text):
    target = stage / name
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return digest(target.read_bytes())


def make_tables(path, record, args, stage, outputs):
    raw, _, header, rows, _ = read_csv(path, args)
    if digest(raw) != record["sha256"]:
        raise DataError("목록 확인 뒤 원본이 변경됨: 다시 실행")
    masked = {i for i, name in enumerate(header) if name in args.mask_column}
    stem = re.sub(r"[^\w-]+", "_", Path(record["path"]).stem).strip("_")[:40] or "data"
    identifier = f"{stem}-{digest(record['path'].encode('utf-8'))[:8]}"
    names = []
    for start in range(0, max(1, len(rows)), 1000):
        name = f"{identifier}-{start // 1000 + 1:03d}.md"
        values = [["가림" if i in masked else value for i, value in enumerate(row)] for row in rows[start:start + 1000]]
        first, last = (start + 1, start + len(values)) if values else (0, 0)
        source_link = quote(os.fsencode(Path(os.path.relpath(path, stage.parent / "_generated")).as_posix()), safe="/:")
        body = [f"<!-- {OWNER}: 자동 생성. 설명·메모는 이 폴더 밖에 작성. -->", "# 원본 표", "",
                f"- 원본: [{cell(record['path'])}]({source_link})",
                f"- 자료 기록: {first}~{last} / 전체 {len(rows)}행; 머리글 제외",
                f"- 원본 SHA-256: `{record['sha256']}`",
                f"- 가린 열: {', '.join(cell(header[i]) for i in sorted(masked)) or '없음'}", "",
                "| " + " | ".join(["기록 번호", *map(cell, header)]) + " |",
                "| " + " | ".join(["---"] * (len(header) + 1)) + " |"]
        expected = [[str(start + i + 1), *row] for i, row in enumerate(values)]
        body.extend("| " + " | ".join(map(cell, row)) + " |" for row in expected)
        outputs[name] = save_text(stage, name, "\n".join(body) + "\n")
        if read_saved_table(stage / name) != [["기록 번호", *header], *expected]:
            raise DataError("변환 확인 실패: 기록 수 또는 값 불일치")
        names.append(name)
    with path.open("rb") as stream:
        latest = stream.read(args.max_bytes + 1)
    if digest(latest) != record["sha256"]:
        raise DataError("표 생성 중 원본이 변경됨: 다시 실행")
    record.update(status="ready", tables=names, verification={"rows": len(rows), "values": "PASS"})


def owned_state(out):
    manifest = out / "manifest.json"
    if not manifest.exists():
        stray = sorted(shown(p.name) for p in out.iterdir())
        if stray:
            raise DataError(f"출력 폴더에 소유 기록(manifest.json) 없이 파일이 있음: {', '.join(stray[:5])}. 폴더 밖으로 옮기거나 지운 뒤 다시 실행")
        return {}
    if linked(manifest):
        raise DataError("manifest가 링크임: 덮어쓰지 않음")
    try:
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        checksum = previous.pop("checksum")
        if previous["owner"] != OWNER or digest(canonical(previous)) != checksum:
            raise ValueError()
        outputs = previous["outputs"]
        if not isinstance(outputs, dict):
            raise ValueError()
        for name, sha in outputs.items():
            if Path(name).name != name or name in {"manifest.json", ".", ".."}:
                raise ValueError()
            target = out / name
            if linked(target) or (target.exists() and digest(target.read_bytes()) != sha):
                raise DataError(f"자동 생성 파일에 수정이 있음: {name}. 남길 메모는 폴더 밖 문서로 옮기고 이 파일을 지운 뒤 다시 실행")
        unknown = sorted({p.name for p in out.iterdir()} - set(outputs) - {"manifest.json"})
        if unknown:
            raise DataError(f"출력 폴더에 등록되지 않은 파일이 있음: {', '.join(map(shown, unknown[:5]))}. 폴더 밖으로 옮기거나 지운 뒤 다시 실행")
        return outputs
    except (KeyError, ValueError, TypeError, AttributeError, OSError):
        raise DataError("소유 기록(manifest.json)이 손상되거나 수정됨: 덮어쓰지 않음. _generated/ 폴더를 지운 뒤 다시 실행") from None


def build_index(records, warnings, command, args):
    labels = {"ready": "표 변환 확인", "inventoried": "구조 확인", "listed": "목록만", "unsupported": "별도 확인 필요", "skipped": "건너뜀", "error": "오류"}
    lines = [f"<!-- {OWNER}: 자동 생성 -->", "# 자료 목록", "",
             "원본의 의미·요구사항 충족·사람 검수 여부는 이 도구가 판정하지 않는다.",
             f"- 실행: `{command}`", f"- 모드: {'목록 확인만' if args.inventory_only else ('목록만(--list-only)' if not args.source else 'CSV 표 생성')}",
             f"- 읽기 설정: 인코딩 {cell('파일마다 UTF-8, 안 되면 CP949' if args.encoding == 'auto' else args.encoding)}, 구분자 {cell(repr(args.delimiter))}",
             "- 빈 값은 길이 0인 값, 중복은 첫 행 이후 완전히 같은 기록 수다.",
             "- 원본 기록 번호는 CSV 머리글을 뺀 순서이며 물리 줄 번호와 다를 수 있다.", "",
             "| 원본 | 크기(byte) | 상태 | 기록 수 | 결과·이유 |", "| --- | --- | --- | --- | --- |"]
    for r in records:
        links = ", ".join(f"[표 {i + 1}]({name})" for i, name in enumerate(r.get("tables", [])))
        lines.append(f"| {cell(r['path'])} | {r.get('bytes', '—')} | {labels[r['status']]} | {r.get('rows', '미확인')} | {links or cell(r.get('reason', '목록 확인'))} |")
    for r in records:
        if "columns" in r:
            lines.extend(["", f"## {cell(r['path'])}",
                          f"- 기록 {r['rows']}개, 열 {len(r['columns'])}개, 중복 기록 {r['duplicate_rows']}개, 인코딩 {r['encoding']}", "",
                          "| 열 이름 | 빈 값 수 |", "| --- | --- |"])
            lines.extend(f"| {cell(name)} | {count} |" for name, count in zip(r["columns"], r["empty_counts"]))
    heads = [r for r in records if r.get("header") or r.get("header_note")]
    if heads:
        lines.extend(["", "## 목록만 남긴 CSV의 머리글", "", "값은 읽지 않았다. 내용은 별도 요약으로 확인한다.", ""])
        lines.extend(f"- {cell(r['path'])}: " + (cell(r["header_note"]) if "header_note" in r else
                     f"열 {len(r['header'])}개 — {', '.join(cell(h) for h in r['header'][:30])}{' 외' if len(r['header']) > 30 else ''}") for r in heads)
    if warnings:
        lines.extend(["", "## 확인할 점", *[f"- {cell(w)}" for w in warnings]])
    return "\n".join(lines) + "\n"


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("1 이상의 수를 입력")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], help="확인할 원본 파일·폴더. 여러 번 지정 가능")
    parser.add_argument("--list-only", action="append", default=[], help="목록과 크기(CSV는 머리글)만 남기고 값은 읽지 않을 파일·폴더. 센서 기록처럼 큰 자료. 여러 번 지정 가능")
    parser.add_argument("--inventory-only", action="store_true", help="목록·CSV 구조만 기록, 원본 값 표는 만들지 않음")
    parser.add_argument("--encoding", default="auto", help="기본 auto: 파일마다 UTF-8을 먼저, 안 되면 CP949로 읽음")
    parser.add_argument("--delimiter", default=",")
    parser.add_argument("--mask-column", action="append", default=[], help="표에서 값을 가릴 열 이름. 정확한 이름, 여러 번 지정")
    parser.add_argument("--max-rows", type=positive, default=10000)
    parser.add_argument("--max-bytes", type=positive, default=10485760)
    args = parser.parse_args(argv)
    if not args.source and not args.list_only:
        parser.error("--source 또는 --list-only가 하나 이상 필요")
    if len(args.delimiter) != 1 or args.delimiter in "\r\n\0\"":
        parser.error("구분자는 줄바꿈·큰따옴표가 아닌 한 글자여야 함")
    try:
        if args.encoding != "auto":
            "".encode(args.encoding)
    except LookupError:
        parser.error("알 수 없는 인코딩")
    root = Path.cwd().resolve()
    out = root / OUTPUT
    stage = None
    try:
        if has_link(out):
            raise DataError("출력 경로에 링크가 있음: 중단")
        out.mkdir(parents=True, exist_ok=True)
        old_outputs = owned_state(out)
        paths, records = discover(args.source + args.list_only, root)
        list_roots = [Path(os.path.abspath(s)) for s in args.list_only]
        by_path = {}
        for path in paths:
            record = listed(path, root, args) if under(path, list_roots) else profile(path, root, args)
            records.append(record)
            by_path[record["path"]] = path
        records.sort(key=lambda r: r["path"])
        known_columns = {name for r in records for name in r.get("columns", [])}
        missing_masks = set(args.mask_column) - known_columns
        warnings = []
        if missing_masks:
            warnings.append(shown("지정한 가림 열을 찾지 못함. 열 이름을 확인할 때까지 모든 표 생성 중지: " + ", ".join(sorted(missing_masks))))
        warnings.extend(f"{r['path']}: {note}" for r in records for note in r.get("notes", []))
        outputs = {}
        stage = Path(tempfile.mkdtemp(prefix=".make-data-guide-", dir=out.parent))
        if not args.inventory_only and not missing_masks:
            for record in records:
                if record["status"] != "inventoried":
                    continue
                before = set(outputs)
                try:
                    make_tables(by_path[record["path"]], record, args, stage, outputs)
                except (DataError, OSError) as error:
                    record.update(status="error", reason=error.reason if isinstance(error, DataError) else "표를 저장하거나 원본을 다시 읽지 못함", tables=[])
                    for name in set(outputs) - before:
                        (stage / name).unlink(missing_ok=True)
                        del outputs[name]
        command = shown(shlex.join(["python3", "make_data_guide.py", *(sys.argv[1:] if argv is None else argv)]))
        outputs["INDEX.md"] = save_text(stage, "INDEX.md", build_index(records, warnings, command, args))
        manifest = {"schema": 1, "owner": OWNER, "files": records, "warnings": warnings,
                    "config": {"inventory_only": args.inventory_only, "list_only": [shown(s) for s in args.list_only], "encoding": args.encoding, "delimiter": args.delimiter,
                               "mask_columns": [shown(name) for name in args.mask_column], "max_rows": args.max_rows, "max_bytes": args.max_bytes},
                    "outputs": outputs}
        manifest["checksum"] = digest(canonical(manifest))
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if owned_state(out) != old_outputs:
            raise DataError("실행 중 출력 소유 기록이 바뀜: 중단")
        for name in [*outputs, "manifest.json"]:
            os.replace(stage / name, out / name)
        for name in set(old_outputs) - set(outputs):
            (out / name).unlink(missing_ok=True)
        failures = sum(r["status"] == "error" or r.get("incomplete", False) for r in records) + bool(missing_masks)
        ready = sum(r["status"] == "ready" for r in records)
        inventoried = sum(r["status"] == "inventoried" for r in records)
        unsupported = sum(r["status"] == "unsupported" for r in records)
        only_listed = sum(r["status"] == "listed" for r in records)
        excluded_count = sum(r["status"] == "skipped" and not r.get("incomplete", False) for r in records)
        print(f"목록 {len(records)}개 / CSV 표 {ready}개 / CSV 구조만 확인 {inventoried}개 / 목록만 {only_listed}개 / 별도 확인 {unsupported}개 / 제외·링크 {excluded_count}개 / 오류·한도·설정 {failures}개")
        print(f"결과: {OUTPUT.as_posix()}/INDEX.md")
        return 1 if failures else 0
    except (DataError, OSError) as error:
        print("중단: " + (error.reason if isinstance(error, DataError) else "파일 접근·저장 실패"), file=sys.stderr)
        return 1
    finally:
        if stage is not None and stage.resolve().parent == out.parent.resolve() and stage.name.startswith(".make-data-guide-"):
            for child in stage.iterdir():
                child.unlink()
            stage.rmdir()


if __name__ == "__main__":
    sys.exit(main())
