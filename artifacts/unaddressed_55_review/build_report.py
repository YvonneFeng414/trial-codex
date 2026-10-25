from pathlib import Path
import csv
import html
import json
import os
from urllib.parse import quote

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
CSV = Path('/Users/yixuanfeng/Desktop/p1/55例_其余原因待核查/55例_其余情况清单.csv')
with CSV.open(encoding='utf-8-sig', newline='') as f:
    all_rows = list(csv.DictReader(f))
rows = [r for r in all_rows if not r['addressed'].strip()]

notes = {
    'M015': dict(name='IMPACT', category='scope', kind='Supplementary methods and results',
        issue='The input is a supplementary appendix rather than the complete trial protocol. Applying every SAP field to this document can produce gaps that OCR cannot resolve.',
        detail='The existing SAP introduction identifies the appendix-only scope. The CSV records supplemental methods/results and a page numbered 21 that is otherwise blank. A genuinely blank page is not evidence of failed extraction.',
        known='Source page 1 identifies a supplementary appendix for the IMPACT peanut oral-immunotherapy trial. The existing SAP describes analyzed desensitization/remission endpoints and exploratory mechanistic work.',
        uncertainty='The CSV does not demonstrate that the sampled Stratification Factors field, or all 25 entirely missing fields, is genuinely absent from the source. Appendix status alone cannot establish that.',
        action='Locate the matching complete protocol/SAP if the dataset is intended to contain full protocols. Retain the appendix for its methods and endpoint details, and verify individual missing fields before excluding or rerunning it.'),
    'M017': dict(name='TiNivo-2', category='scope', kind='Results appendix and amendment list',
        issue='The selected attachment reports supplementary results, eligibility and protocol amendments. It cannot automatically stand in for the full design and analysis document.',
        detail='The CSV records supplemental results, inclusion/exclusion criteria and an amendment list. The SAP reports observed arm denominators of 171 and 172 while distinguishing them from a planned allocation ratio.',
        known='Source page 1 identifies the TiNivo-2 supplementary appendix. The SAP states that amendments added exploratory objectives and efficacy subgroup analyses, without supplying the individual objective wording.',
        uncertainty='A mention that an objective or analysis was amended does not recover its complete definition. The 38 missing-field classifications still require field-by-field checks; observed arm sizes do not establish the planned randomization ratio.',
        action='Match the appendix to the complete protocol and amendment versions. Keep results, planned methods and amendment metadata distinct; recover the actual endpoint/objective definitions before regenerating the SAP.'),
    'M039': dict(name='CHAT / MORDOR II', category='scope', kind='Manual of Operations and Procedures, v2.5',
        issue='This is an operations manual with trial content, so its coverage may differ from a full protocol/SAP. It also uses both CHAT and MORDOR II names, which need consistent document identification.',
        detail='The CSV calls it the CHAT manual; the SAP uses MORDOR II. The source title page itself contains both “Community Health Azithromycin Trial” and “MORDOR II Study,” so the naming difference alone is not evidence of a wrong-file match.',
        known='Source page 1 confirms the manual title, version 2.5, Burkina Faso setting and both study names. The SAP introduction includes mortality and mechanistic objectives, showing that the manual contains substantive study information.',
        uncertainty='Being a manual does not prove that randomization, endpoint or statistical details are absent. The sampled Stratification Factors claim has not been independently verified across all 62 pages in this triage.',
        action='Verify the study-name/version mapping and compare the manual with the correct full protocol/SAP. Preserve the manual’s supported content; do not drop it solely because of its title.'),
    'M069': dict(name='ART', category='scope', kind='Bedside compact operations manual',
        issue='The input is a bedside ventilation-instruction manual. Its intended use is much narrower than the full SAP template, making source selection the leading issue to investigate.',
        detail='The CSV records an ART Bedside Compact Manual of Operations, version 3, May 2015. The SAP describes instructions for patients already randomized to ART or ARDSNET, with most design and analysis fields left unreported.',
        known='Source page 1 explicitly labels this a bedside compact manual for the ART group and says to attach it to the patient’s ventilator. This directly establishes its operational purpose.',
        uncertainty='The high missing-field fraction (52/68) is consistent with a scope mismatch, but it does not prove each absence. The complete protocol must be located before treating this case as unrecoverable.',
        action='Prioritize finding the complete ART trial protocol/SAP. Keep this manual as supporting evidence for intervention delivery and safety rules; rerunning OCR on the same bedside manual alone is not a demonstrated solution.'),
    'M076': dict(name='ENGAGES-CANADA', category='scope', kind='Operations manual with site revisions',
        issue='The input is an operations manual with multiple institutional revision dates. Document scope and version alignment need checking before missing information can be attributed to extraction.',
        detail='The CSV distinguishes this MOP from a research-application protocol. The SAP includes EEG monitoring, delirium and quality-of-life content but cautions that the primary/secondary objective labels are not explicitly assigned in the source.',
        known='Source page 1 identifies the Manual of Operations, with dates January 16, 2015; February 9, 2016 for Manitoba; and March 1, 2019 for Montréal.',
        uncertainty='Those dates do not establish which wording supersedes all other material. No specific loss caused by OCR is demonstrated by the CSV, and the presence of an OCR work directory is not proof of improved content.',
        action='Match the manual to the trial protocol/SAP and intended version. Check objective hierarchy, analysis details and assessment schedules individually while preserving the manual’s operational definitions.'),
    'M008': dict(name='SLN360-001', category='format', kind='Full protocol; assessment-table symbols flagged',
        issue='A localized assessment-table formatting issue is suspected. Dots, circles or other schedule symbols may have been altered during text extraction, affecting the mapping of assessments to visits.',
        detail='The CSV reports that the assessment schedule on PDF page 43 is visually readable, with possible symbol issues on pages 43–45. It explicitly says the full table has not been checked cell by cell.',
        known='Source pages 1–2 identify a randomized, double-blind, placebo-controlled first-in-human SLN360 study, protocol version 6.0 dated November 9, 2021, Amendment 3. This case is not classified as a manual/appendix scope mismatch.',
        uncertainty='The suspected symbols have not been shown to cause any of the 25 entirely missing fields. The CSV’s example is Stratification Factors, which has no demonstrated connection to the schedule-symbol issue.',
        action='Visually compare pages 43–45 with the extracted table and SAP visit timing, including headers, symbol legends and footnotes. Repair affected cells or use localized OCR only if a mismatch is confirmed; a whole-document rerun is not yet justified.'),
    'M018': dict(name='FANCOLEN-1', category='unresolved', kind='Long supplementary package with protocol material',
        issue='The cause of the missing fields remains unresolved. Successful PDF opening and text conversion do not establish either complete extraction or genuine source absence.',
        detail='The CSV records successful conversion and limited source checks without a clear reading failure. The existing SAP cites protocol V5 for objectives and describes toxicity/safety, engraftment and clinical response.',
        known='Source page 1 identifies the FANCOLEN-1/long-term-trial supplementary appendix. The current extracted file has 182 page markers, and the SAP uses version-specific citations within that package.',
        uncertainty='The 28 missing fields cannot yet be assigned to source omission, version selection, field applicability or extraction failure. There is no confirmed OCR diagnosis in the CSV.',
        action='Audit missing fields against the appropriate protocol version and all relevant material in the package. Start with consequential fields such as endpoint definitions, sample-size rationale, analysis populations and missing-data rules. Record a separate cause for each field.'),
}

def esc(s):
    return html.escape(str(s), quote=True)

def link(path):
    return quote(os.path.relpath(path, OUT), safe='/')

source_paths = {
 'M015': 'resources/sap_result_label0/work', 'M017': 'resources/sap_result_label0/work',
 'M039': 'resources/test_result_label1/work', 'M069': 'prod_result/work',
 'M076': 'prod_result/work', 'M008': 'resources/sap_result_label0/work',
 'M018': 'resources/sap_result_label0/work',
}
category_labels = {'scope': 'Source scope', 'format': 'Table formatting', 'unresolved': 'Cause unresolved'}
cards = []
overview_rows = []
export_rows = []
for r in rows:
    case = r['case_id']
    n = notes[case]
    old = CSV.parent / r['包内md路径']
    pdf = CSV.parent / r['包内PDF路径']
    source = ROOT / source_paths[case] / (old.stem + '.protocol.txt')
    percent = float(r['整项完全无信息占比_百分数'])
    stats = f"{r['整项完全无信息数量']} / {r['信息项总数']} fields · {percent:.1f}%"
    title = r['文件名及DOI标识'].splitlines()[0]
    search = ' '.join([case, n['name'], title, n['kind'], n['category']])
    overview_rows.append(f'<tr><td><a href="#{case}">{case}</a></td><td>{esc(n["name"])}</td><td>{esc(category_labels[n["category"]])}</td><td>{esc(stats)}</td></tr>')
    cards.append(f'''<details class="case" id="{case}" data-category="{n['category']}" data-search="{esc(search.lower())}">
      <summary><span class="case-id">{case}</span><span class="case-heading"><strong>{esc(n['name'])}</strong><span>{esc(n['kind'])}</span></span><span class="badge {n['category']}">{esc(category_labels[n['category']])}</span><span class="count">{percent:.1f}%<small>entirely missing</small></span><span class="chevron" aria-hidden="true"></span></summary>
      <div class="case-body">
        <p class="issue">{esc(n['issue'])}</p>
        <div class="metrics"><span><b>{r['整项完全无信息数量']}</b> entirely missing fields</span><span><b>{r['信息项总数']}</b> total fields</span><span>Recorded priority: <b>{esc(r['原优先级'])}</b></span><span>CSV assessment: <b>{esc(r['判断状态'])}</b></span></div>
        <div class="columns"><section><h3>What the existing evidence says</h3><p>{esc(n['detail'])}</p><h3>Confirmed in this triage</h3><p>{esc(n['known'])}</p></section><section><h3>What remains unresolved</h3><p>{esc(n['uncertainty'])}</p><div class="action"><h3>Recommended next step</h3><p>{esc(n['action'])}</p></div></section></div>
        <details class="evidence"><summary>View recorded evidence and source links</summary><div>
          <dl><dt>Document identifier</dt><dd>{esc(title)}</dd><dt>CSV observation / 观察到的现象</dt><dd>{esc(r['观察到的现象'])}</dd><dt>Pages checked in the earlier CSV review</dt><dd>{esc(r['PDF实际页序_从1开始'])}</dd><dt>Relationship to entirely missing fields / 与整项无信息的关系</dt><dd>{esc(r['与整项无信息的关系'])}</dd><dt>Sample SAP statement recorded in CSV</dt><dd><pre>{esc(r['md原句及行号'])}</pre></dd></dl>
          <p class="muted">The quoted SAP statement is a recorded claim, not a new verification of source absence. Earlier page checks are taken from the CSV; this triage inspected source pages 1–2 and the existing SAP opening.</p>
          <div class="source-links"><a href="{link(old)}">Original SAP Markdown ↗</a><a href="{link(pdf)}">Packaged PDF ↗</a><a href="{link(source)}">Extracted source text ↗</a></div>
        </div></details>
      </div></details>''')
    export_rows.append({
        'case_id': case, 'study': n['name'], 'document_identifier': title,
        'category': category_labels[n['category']], 'missing_fields': r['整项完全无信息数量'],
        'total_fields': r['信息项总数'], 'missing_percent': r['整项完全无信息占比_百分数'],
        'recorded_priority': r['原优先级'], 'recorded_status': r['判断状态'],
        'issue': n['issue'], 'confirmed_in_triage': n['known'], 'unresolved': n['uncertainty'],
        'recommended_action': n['action'], 'csv_observation': r['观察到的现象'],
        'prior_pages_checked': r['PDF实际页序_从1开始'], 'original_SAP': str(old),
        'PDF': str(pdf), 'source_text': str(source),
    })

with (OUT / 'unaddressed_cases.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=list(export_rows[0]))
    writer.writeheader()
    writer.writerows(export_rows)

document = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seven unaddressed cases · Source triage</title>
<style>
:root{--ink:#172b38;--muted:#556b79;--line:#dce5e9;--bg:#f4f7f8;--teal:#087d79;--navy:#123441;--amber:#905900}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#086b77;text-underline-offset:3px}button,input{font:inherit}button,a,input,summary{-webkit-tap-highlight-color:transparent}:focus-visible{outline:3px solid #2a9dba;outline-offset:4px}.wrap{max-width:1120px;margin:auto;padding:0 28px}header{background:var(--navy);color:#fff;padding:48px 0 42px}.eyebrow{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#a2dbd8;font-weight:700}h1{font-size:clamp(30px,4vw,45px);line-height:1.16;letter-spacing:-1.2px;margin:12px 0 17px;max-width:850px}header p{max-width:800px;color:#d3e3e9;margin:0}header .meta{margin-top:25px;font-size:13px;color:#b9cbd2}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:26px 0}.stat{background:white;border:1px solid var(--line);border-radius:13px;padding:19px 22px}.stat b{display:block;font-size:31px;line-height:1.3}.stat span{color:var(--muted);font-size:14px}.notice{background:#fff8e8;border:1px solid #e8d7ae;border-left:4px solid #b77c19;padding:17px 21px;border-radius:10px;margin-bottom:25px}.notice p{margin:0}.notice p+p{margin-top:7px}.panel{background:white;border:1px solid var(--line);border-radius:12px;margin-bottom:24px}.panel>summary{padding:16px 21px;font-weight:650;cursor:pointer}.table-wrap{overflow:auto;padding:0 21px 18px}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}th,td{padding:12px 10px;border-bottom:1px solid var(--line)}td:first-child,th:first-child{padding-left:0}td:last-child{white-space:nowrap}.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin:29px 0 15px}.search{background:white;border:1px solid #becdd5;border-radius:8px;padding:9px 13px;min-width:265px;flex:1;max-width:360px}.filters{display:flex;gap:7px;flex-wrap:wrap}button,.download{border:1px solid #c6d4da;background:white;border-radius:7px;color:var(--ink);padding:8px 12px;cursor:pointer;font-size:13px;text-decoration:none}button:hover,.download:hover{background:#edf4f5}button[aria-pressed=true]{background:var(--navy);border-color:var(--navy);color:white}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:19px}.actions .count-label{margin-right:auto;color:var(--muted);font-size:13px}.case{background:white;border:1px solid var(--line);border-radius:12px;margin-bottom:13px;overflow:hidden;scroll-margin-top:20px}.case>summary{list-style:none;display:flex;align-items:center;gap:17px;padding:20px 22px;cursor:pointer}.case>summary::-webkit-details-marker{display:none}.case[open]{border-color:#9fbfca;box-shadow:0 5px 20px #183a4909}.case[open]>summary{border-bottom:1px solid var(--line)}.case-id{font:700 12px/1.4 ui-monospace,monospace;color:var(--muted);background:#edf2f5;padding:7px 8px;border-radius:6px}.case-heading{display:flex;flex-direction:column;flex:1;min-width:0}.case-heading strong{font-size:18px;line-height:1.4}.case-heading>span{font-size:13px;color:var(--muted);margin-top:3px}.badge{font-size:11px;font-weight:650;padding:4px 9px;border-radius:20px;white-space:nowrap}.badge.scope{background:#e5f3f1;color:#126962}.badge.format{background:#fef1d8;color:#845200}.badge.unresolved{background:#edf0fb;color:#4a5489}.count{text-align:right;font-size:18px;font-weight:700;min-width:105px}.count small{display:block;font-size:10px;font-weight:400;color:var(--muted)}.chevron{width:9px;height:9px;border-right:2px solid #6d8591;border-bottom:2px solid #6d8591;transform:rotate(45deg);margin-left:8px;margin-top:-5px}.case[open] .chevron{transform:rotate(225deg);margin-top:5px}.case-body{padding:23px}.issue{font-size:18px;line-height:1.65;margin:0 0 18px;max-width:950px}.metrics{display:flex;flex-wrap:wrap;gap:9px 23px;background:#f5f8fa;padding:11px 15px;border-radius:7px;font-size:12px;color:var(--muted)}.metrics b{color:var(--ink)}.columns{display:grid;grid-template-columns:1fr 1fr;gap:30px;margin:17px 0}h3{font-size:13px;color:#285568;margin:15px 0 7px}.columns p{font-size:14px;margin:0 0 18px}.action{background:#edf7f5;border-radius:8px;padding:1px 15px 3px}.action h3{color:#08665f}.evidence{border-top:1px solid var(--line);padding-top:13px}.evidence>summary{cursor:pointer;font-size:13px;color:#286473;font-weight:600}.evidence>div{padding-top:12px}dl{font-size:13px;margin:0}dt{font-weight:650;color:var(--muted);margin:12px 0 4px}dd{margin:0;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,monospace;background:#f3f6f8;padding:13px;border-radius:6px}.muted{font-size:12px;color:var(--muted)}.source-links{display:flex;gap:15px;flex-wrap:wrap;font-size:12px;margin-top:16px}.empty{padding:35px;text-align:center;color:var(--muted)}[hidden]{display:none!important}.method{padding:20px 23px;margin-top:25px}.method h2{font-size:17px;margin:0 0 9px}.method p{font-size:13px;color:var(--muted);margin:9px 0}.method code{font-size:12px;background:#eef2f5;padding:2px 5px;border-radius:3px}footer{font-size:12px;color:var(--muted);padding:8px 0 35px}.footer-links{display:flex;gap:18px;flex-wrap:wrap}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:740px){.wrap{padding:0 16px}header{padding:33px 0}.stats{grid-template-columns:repeat(2,1fr);gap:10px}.stat{padding:15px}.case>summary{gap:10px;padding:17px 14px;flex-wrap:wrap}.case-heading{flex-basis:55%}.badge{order:4;margin-left:56px}.count{min-width:72px;font-size:16px}.case-body{padding:17px}.columns{grid-template-columns:1fr;gap:0}.search{max-width:none;min-width:0;width:100%}.toolbar{align-items:stretch}.filters{width:100%}.issue{font-size:16px}.chevron{margin-left:0}.count small{font-size:9px}}@media print{body{background:white;font-size:11px}header{background:white;color:var(--ink);padding:10px 0}header p,header .meta,.eyebrow{color:var(--muted)}h1{font-size:26px}.toolbar,.actions,.download,.source-links{display:none}.wrap{max-width:none;padding:0}.stats{margin:12px 0}.stat{padding:10px}.stat b{font-size:23px}.case{break-inside:avoid;box-shadow:none}.columns{gap:20px}.notice{background:white}.badge{border:1px solid #ccd8dd}.case-body{padding:12px}.issue{font-size:14px}.columns p{font-size:11px}.method p{font-size:11px}}
</style></head><body>
<header><div class="wrap"><div class="eyebrow">Protocol → SAP · Source triage</div><h1>What is holding up the<br>seven unaddressed cases?</h1><p>Five concern the type or scope of the supplied document. One concerns assessment-table symbols. One still has no established cause.</p><div class="meta">55-case inventory · 48 marked addressed · 7 blank in “addressed” · Reviewed 11 October 2026</div></div></header>
<main class="wrap">
<div class="stats" aria-label="Case counts"><div class="stat"><b>7</b><span>Unaddressed cases</span></div><div class="stat"><b>5</b><span>Source-scope concerns</span></div><div class="stat"><b>1</b><span>Table-format concern</span></div><div class="stat"><b>1</b><span>Cause still unresolved</span></div></div>
<div class="notice"><p><strong>These are triage findings, not seven confirmed extraction failures.</strong></p><p>The CSV labels six cases as suspected and one as undetermined. No unaddressed case has a confirmed causal link between an OCR error and its entirely missing fields. Document titles are checked; individual absence claims still need source verification.</p></div>
<details class="panel"><summary>Quick comparison · all seven cases</summary><div class="table-wrap"><table><thead><tr><th>Case</th><th>Study</th><th>Recorded concern</th><th>Entirely missing / total</th></tr></thead><tbody>__OVERVIEW__</tbody></table></div></details>
<div class="toolbar"><label class="sr-only" for="search">Search case, study or DOI</label><input class="search" id="search" type="search" placeholder="Search case, study or DOI…"><div class="filters" aria-label="Filter cases"><button data-filter="all" aria-pressed="true">All 7</button><button data-filter="scope" aria-pressed="false">Source scope · 5</button><button data-filter="format" aria-pressed="false">Table formatting · 1</button><button data-filter="unresolved" aria-pressed="false">Unresolved · 1</button></div></div>
<div class="actions"><span id="visible-count" class="count-label" aria-live="polite">7 cases · click a row to expand</span><button id="expand">Expand visible cases</button><button id="collapse">Collapse all</button><a class="download" href="unaddressed_cases.csv" download>Download case table ↓</a></div>
<div id="cases">__CARDS__</div><p id="empty" class="empty" hidden>No cases match this search.</p>
<section class="panel method"><h2>How to interpret this report</h2><p><strong>Selection:</strong> all rows whose <code>addressed</code> value is blank in the supplied CSV. A blank solution or a “待人工复核” value is not used as an alternative filter: the latter appears on all 55 rows, including those marked addressed.</p><p><strong>Counts:</strong> the percentages are the CSV’s <code>整项完全无信息数量 / 信息项总数</code>. They are historical counts of entirely missing fields, not a recount of “Not reported” phrases and not a measure of OCR accuracy.</p><p><strong>Verification performed:</strong> all seven CSV records; the opening/background/objective passages of their packaged SAPs; and pages 1–2 of the corresponding local page-tagged source texts. Source front matter supports document identity and type. The more extensive PDF-page checks shown inside the evidence folds are historical checks recorded in the CSV, not newly repeated here. A table of contents is not used to prove body content or absence.</p><p><strong>Scope:</strong> this is a source-selection and problem-triage report. It does not certify every missing field, audit every source page, or approve any generated SAP. None of the source files, SAPs or CSV status values were changed.</p><p><strong>Next-work order:</strong> first resolve source selection for ART and the two results appendices; verify manual/protocol alignment for CHAT and ENGAGES-CANADA; check SLN360’s schedule cells locally; then perform a version-aware missing-field audit for FANCOLEN-1. Retain useful supporting attachments even when a complete protocol is needed.</p></section>
<footer><div class="footer-links"><a href="__CSV_LINK__">Open original 55-case CSV ↗</a><span>Offline report · no external scripts or fonts · source links refer to files on this computer</span></div></footer>
</main><script>
const cases = [...document.querySelectorAll('details.case')];
const filters = [...document.querySelectorAll('[data-filter]')];
let category = 'all';
function applyFilters() {
  const query = document.getElementById('search').value.trim().toLowerCase();
  let shown = 0;
  cases.forEach(item => {
    item.hidden = !((category === 'all' || item.dataset.category === category) && item.dataset.search.includes(query));
    if (!item.hidden) shown++;
  });
  document.getElementById('visible-count').textContent = `${shown} of ${cases.length} cases · click a row to expand`;
  document.getElementById('empty').hidden = shown !== 0;
}
filters.forEach(button => button.addEventListener('click', () => {
  category = button.dataset.filter;
  filters.forEach(other => other.setAttribute('aria-pressed', String(other === button)));
  applyFilters();
}));
document.getElementById('search').addEventListener('input', applyFilters);
document.getElementById('expand').addEventListener('click', () => cases.filter(item => !item.hidden).forEach(item => item.open = true));
document.getElementById('collapse').addEventListener('click', () => document.querySelectorAll('details').forEach(item => item.open = false));
function openLinkedCase() {
  const item = document.getElementById(location.hash.slice(1));
  if (item && item.classList.contains('case')) {
    category = 'all';
    document.getElementById('search').value = '';
    filters.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.filter === 'all')));
    applyFilters();
    item.open = true;
    item.scrollIntoView({block:'start'});
  }
}
window.addEventListener('hashchange', openLinkedCase);
openLinkedCase();
let beforePrint = [];
window.addEventListener('beforeprint', () => {
  beforePrint = [...document.querySelectorAll('details')].map(item => [item,item.open]);
  beforePrint.forEach(([item]) => item.open = true);
});
window.addEventListener('afterprint', () => beforePrint.forEach(([item,open]) => item.open = open));
</script></body></html>'''
document = document.replace('__OVERVIEW__', ''.join(overview_rows)).replace('__CARDS__', '\n'.join(cards)).replace('__CSV_LINK__', link(CSV))
(OUT / 'index.html').write_text(document, encoding='utf-8')
print(f'Created {OUT / "index.html"}; {len(rows)} cases')
