from pathlib import Path
import csv
import html
import json
import re

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STEM = 'lancet++10.1016_S0140-6736_24_01880-4++mmc1_protocol'
OLD = Path('/Users/yixuanfeng/Desktop/p1/55例_其余原因待核查/文件/10.1016_S0140-6736_24_01880-4__mmc1_protocol') / (STEM + '.md')
NEW = ROOT / 'rerun_m018_result' / (STEM + '.md')
WORK = ROOT / 'rerun_m018_result/work'


def sections(text):
    parts = re.split(r'^### (.+)\n', text, flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def counts(text):
    return {
        'not_reported_phrase_count': len(re.findall('not reported', text, re.I)),
        'exact_not_reported_in_protocol_count': len(re.findall('not reported in protocol', text, re.I)),
        'not_applicable_phrase_count': len(re.findall('not applicable', text, re.I)),
        'whitespace_delimited_word_count': len(text.split()),
    }


def main():
    old, new = OLD.read_text(), NEW.read_text()
    findings = json.loads((HERE / 'findings.json').read_text())
    metrics = {'original': counts(old), 'regenerated': counts(new)}
    (HERE / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    with (HERE / 'findings.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['assessment', 'field', 'original', 'regenerated', 'source_pages', 'conclusion'])
        writer.writeheader()
        writer.writerows(findings['findings'])
    esc = html.escape
    rows = ''.join('<tr>' + ''.join(f'<td>{esc(str(v))}</td>' for v in [label, metrics['original'][key], metrics['regenerated'][key]]) + '</tr>' for key, label in [
        ('not_reported_phrase_count', '“Not reported” occurrences'),
        ('exact_not_reported_in_protocol_count', '“Not reported in protocol” occurrences'),
        ('not_applicable_phrase_count', '“Not applicable” occurrences'),
        ('whitespace_delimited_word_count', 'Words (whitespace separated)'),
    ])
    items = ''.join(f'''<details open><summary>{esc(f['assessment'])}: {esc(f['field'])}</summary>
      <p><b>Original:</b> {esc(f['original'])}</p><p><b>Regenerated:</b> {esc(f['regenerated'])}</p>
      <p><b>Source PDF-marker pages:</b> {esc(f['source_pages'])}</p><p>{esc(f['conclusion'])}</p></details>''' for f in findings['findings'])
    before, after = sections(old), sections(new)
    diffs = ''.join(f'''<details><summary>{esc(title)}</summary><div class="compare"><div><h3>Original</h3><pre>{esc(before.get(title, '(absent)'))}</pre></div><div><h3>Regenerated</h3><pre>{esc(after.get(title, '(absent)'))}</pre></div></div></details>''' for title in dict.fromkeys([*before, *after]))
    links = ' · '.join(f'<a href="{p.as_uri()}">{esc(label)}</a>' for label, p in [('Original SAP', OLD), ('Regenerated SAP', NEW), ('Evidence', WORK / (STEM + '.evidence.md')), ('Review history', WORK / (STEM + '.review.md')), ('Page-tagged source', WORK / (STEM + '.protocol.txt'))])
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>M018 regeneration comparison</title><style>
body{{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f4f6fa;color:#172b43}}main{{max-width:1260px;margin:auto;padding:32px}}
h1{{margin-bottom:8px}}a{{color:#145ba9}}.panel,details{{background:white;border:1px solid #dce3ed;border-radius:10px;padding:18px;margin:14px 0}}
summary{{font-weight:650;cursor:pointer}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #dde3ec}}
.compare{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.compare>div{{min-width:0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}}
button{{padding:9px 15px;background:white;border:1px solid #a8b8cc;border-radius:6px;cursor:pointer;margin-right:8px}}@media(max-width:800px){{.compare{{grid-template-columns:1fr}}main{{padding:16px}}}}
</style><main><h1>M018 · FANCOLEN-1</h1><p>Original vs regenerated SAP · 182-page source · high verbosity</p>
<section class="panel"><p><strong>{esc(findings['verdict'])}</strong></p><p>{esc(findings['scope'])}</p><p>{links}</p></section>
<section class="panel"><h2>Comparable counts</h2><table><thead><tr><th>Metric</th><th>Original</th><th>Regenerated</th></tr></thead><tbody>{rows}</tbody></table>
<p>Phrase counts include partial absence statements. They are not the CSV’s historical entirely-missing-field count and are not a quality score. Source text is unchanged apart from whitespace.</p></section>
<h2>Source-checked findings</h2>{items}<h2>Full section comparison</h2><p><button onclick="document.querySelectorAll('details').forEach(d=>d.open=true)">Expand all</button><button onclick="document.querySelectorAll('details').forEach(d=>d.open=false)">Collapse all</button></p>{diffs}</main></html>'''
    (HERE / 'index.html').write_text(document)
    print(json.dumps(metrics, indent=2))
    print(HERE / 'index.html')


if __name__ == '__main__':
    main()
