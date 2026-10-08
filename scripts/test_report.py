"""Run the tests and build a single HTML report you can open in a browser.

Usage (from the project folder):
    python -m poetry run python scripts/test_report.py                 # run all tests, write test-report.html
    python -m poetry run python scripts/test_report.py tests/test_prompts.py -k welcome    # extra args go to pytest
    python scripts/test_report.py --from-xml                           # no test run: just rebuild the page from test-results.xml
    add --open to open the page in your browser when it is done

Needs nothing but the standard library. The page is built from pytest's JUnit XML (``test-results.xml``).
"""
import argparse
import collections
import html
import os
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XML_PATH = ROOT / "test-results.xml"
HTML_PATH = ROOT / "test-report.html"

CSS = """
:root{--bg:#fff;--fg:#1c2230;--muted:#667085;--line:#e4e7ec;--card:#f8f9fb;--pass:#127a4a;--fail:#b42318;--skip:#a15c07}
@media (prefers-color-scheme:dark){:root{--bg:#10141c;--fg:#e6e9ef;--muted:#98a2b3;--line:#273042;--card:#171d29;--pass:#4ade80;--fail:#f87171;--skip:#fbbf24}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto}h1{margin:0 0 4px;font-size:22px}.sub{color:var(--muted);margin-bottom:20px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}.tile{flex:1;min-width:130px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}
.pass b{color:var(--pass)}.fail b{color:var(--fail)}.skip b{color:var(--skip)}
h2{font-size:17px;margin:28px 0 8px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--muted);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;display:flex;min-width:120px}.bar i{display:block;height:100%}
.bp{background:var(--pass)}.bf{background:var(--fail)}.bs{background:var(--skip)}
details{border:1px solid var(--line);border-radius:8px;margin:8px 0;background:var(--card)}summary{cursor:pointer;padding:9px 12px;font-weight:600}
details pre{margin:0;padding:10px 12px;overflow:auto;border-top:1px solid var(--line);font-size:12.5px;white-space:pre-wrap}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);color:var(--muted)}
.good{color:var(--pass)}.bad{color:var(--fail)}.warn{color:var(--skip)}
"""


def load(xml_path: Path):
    root = ET.parse(xml_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    cases = []
    for case in suite.iter("testcase"):
        classname = case.attrib.get("classname", "")
        parts = classname.split(".")
        module = parts[1] if len(parts) > 1 else (parts[0] or "(unknown)")
        problem = case.find("failure") if case.find("failure") is not None else case.find("error")
        skipped = case.find("skipped")
        status = "failed" if problem is not None else "skipped" if skipped is not None else "passed"
        detail = ""
        if problem is not None:
            detail = (problem.attrib.get("message", "") + "\n\n" + (problem.text or "")).strip()
        elif skipped is not None:
            detail = skipped.attrib.get("message", "").strip()
        cases.append({"module": module, "name": case.attrib.get("name", ""), "status": status,
                      "time": float(case.attrib.get("time", 0)), "detail": detail})
    return suite.attrib, cases


def tile(css, number, label):
    return f'<div class="tile {css}"><b>{number}</b><span>{label}</span></div>'


def bar(passed, failed, skipped):
    total = max(passed + failed + skipped, 1)
    width = lambda n: f"{n * 100 / total:.1f}%"  # noqa: E731
    return (f'<div class="bar"><i class="bp" style="width:{width(passed)}"></i>'
            f'<i class="bf" style="width:{width(failed)}"></i><i class="bs" style="width:{width(skipped)}"></i></div>')


def build_html(attrib, cases) -> str:
    counts = collections.Counter(c["status"] for c in cases)
    by_module = collections.defaultdict(collections.Counter)
    for c in cases:
        by_module[c["module"]][c["status"]] += 1

    failed = [c for c in cases if c["status"] == "failed"]
    skipped = [c for c in cases if c["status"] == "skipped"]
    verdict = ('<span class="bad">FAILED</span>' if failed
               else '<span class="warn">PASSED, but some tests were skipped</span>' if skipped
               else '<span class="good">ALL PASSED</span>')

    rows = "".join(
        f"<tr><td>{html.escape(m)}</td><td class='n good'>{c['passed']}</td><td class='n bad'>{c['failed']}</td>"
        f"<td class='n warn'>{c['skipped']}</td><td>{bar(c['passed'], c['failed'], c['skipped'])}</td></tr>"
        for m, c in sorted(by_module.items())
    )

    def block(title, items, empty):
        if not items:
            return f"<h2>{title}</h2><p class='sub'>{empty}</p>"
        body = "".join(
            f"<details {'open' if c['status'] == 'failed' else ''}><summary>{html.escape(c['module'])} · "
            f"{html.escape(c['name'])}</summary><pre>{html.escape(c['detail'] or '(no detail)')}</pre></details>"
            for c in items
        )
        return f"<h2>{title}</h2>{body}"

    skip_reasons = collections.Counter(c["detail"] or "(no reason given)" for c in skipped)
    reasons = "".join(
        f"<tr><td class='n warn'>{n}</td><td>{html.escape(reason)}</td></tr>" for reason, n in skip_reasons.most_common()
    )
    skip_section = (
        "<h2>Why tests were skipped</h2><table><tr><th>Tests</th><th>Reason</th></tr>" + reasons + "</table>"
        if skipped else ""
    )
    slowest = sorted(cases, key=lambda c: c["time"], reverse=True)[:8]
    slow_rows = "".join(
        f"<tr><td class='n'>{c['time']:.2f}s</td><td>{html.escape(c['module'])} · {html.escape(c['name'])}</td>"
        f"<td><span class='tag'>{c['status']}</span></td></tr>" for c in slowest
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test report</title><style>{CSS}</style></head><body><main>
<h1>Test report · {verdict}</h1>
<div class="sub">{datetime.now():%Y-%m-%d %H:%M} · ran in {float(attrib.get('time', 0)):.0f}s · source: test-results.xml</div>
<div class="tiles">{tile('', len(cases), 'tests')}{tile('pass', counts['passed'], 'passed')}{tile('fail', counts['failed'], 'failed')}{tile('skip', counts['skipped'], 'skipped')}</div>
{block('Failures', failed, 'None. Nothing failed.')}
{skip_section}
<h2>By test file</h2><table><tr><th>File</th><th>Passed</th><th>Failed</th><th>Skipped</th><th></th></tr>{rows}</table>
<h2>Slowest tests</h2><table>{slow_rows}</table>
{block('Skipped tests', skipped, 'Nothing was skipped.')}
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-xml", action="store_true", help="don't run the tests; rebuild the page from test-results.xml")
    parser.add_argument("--open", action="store_true", help="open the page in the browser afterwards")
    args, pytest_args = parser.parse_known_args()

    exit_code = 0
    if not args.from_xml:
        command = [sys.executable, "-m", "pytest", *(pytest_args or ["tests"]), "-q", "-rs", "-p", "no:cacheprovider",
                   f"--junitxml={XML_PATH}"]
        print("Running:", " ".join(command))
        exit_code = subprocess.run(command, cwd=ROOT).returncode
    if not XML_PATH.exists():
        print(f"No {XML_PATH.name} to build a report from: run the tests first.")
        return exit_code or 1

    attrib, cases = load(XML_PATH)
    HTML_PATH.write_text(build_html(attrib, cases), encoding="utf-8")
    counts = collections.Counter(c["status"] for c in cases)
    print(f"\nReport: {HTML_PATH}\n{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped")
    if args.open:
        webbrowser.open(HTML_PATH.as_uri())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
