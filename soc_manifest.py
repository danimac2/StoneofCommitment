#!/usr/bin/env python3
"""
soc_manifest.py — build a telemetry manifest from a compiled SOC (SugarCube 2) HTML build.

Usage:
    python3 soc_manifest.py VanquishDeath_v0_0_043.html [--out DIR]

Writes two files next to the HTML (or into --out):
    <build>.manifest.json   machine-readable: passages, links, choices, consent beats,
                            variables, telemetry events, audit findings
    <build>.manifest.md     human-readable audit summary

No dependencies beyond the Python 3 standard library.

What it reads (verified against VanquishDeath_v0_0_043):
    <tw-storydata>                       build name, IFID, format, start passage
    <tw-passagedata name tags pid>       every passage; body is escaped twee source
    <script id="twine-user-script">      Story JavaScript: macros, telemetry event names,
                                         WATCHED_STATS, SOC_VERSION
Passage bodies are twee source, not rendered HTML, so links and macros are parsed from
macro syntax. A quoted macro argument that exactly matches a passage name counts as a link.
"""
import argparse, collections, hashlib, html, json, os, re, sys

NAV_MACROS = {'advance', 'sceneadvance', 'chasechoice', 'stancechoice', 'goto', 'link',
              'hubexit', 'chapterend', 'button', 'include'}
CHOICE_MACROS = {'chasechoice', 'stancechoice'}


# ---------- low-level parsing ----------

def attr(attrs, name):
    m = re.search(r'\b' + name + r'="([^"]*)"', attrs)
    return html.unescape(m.group(1)) if m else ''


def iter_macros(text):
    """Yield (name, raw_args, start_offset) for every <<name ...>> opening macro,
    scanning to the matching >> while respecting quotes (macros may span lines)."""
    i, n = 0, len(text)
    while True:
        i = text.find('<<', i)
        if i < 0:
            return
        m = re.match(r'<<\s*([A-Za-z_][\w-]*)', text[i:])
        if not m:
            i += 2
            continue
        name = m.group(1)
        j = i + m.end()
        q = None
        while j < n:
            c = text[j]
            if q:
                if c == '\\':
                    j += 2
                    continue
                if c == q:
                    q = None
            elif c in '"\'`':
                q = c
            elif text.startswith('>>', j):
                break
            j += 1
        yield name, text[i + m.end():j], i
        i = j + 2


def tokenize_args(raw):
    """Split SugarCube macro args: "dq", 'sq', `expr`, [[link]], or bare words."""
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c.isspace():
            i += 1
            continue
        if c in '"\'`':
            j = i + 1
            buf = []
            while j < n and raw[j] != c:
                if raw[j] == '\\' and j + 1 < n:
                    buf.append(raw[j:j + 2]); j += 2; continue
                buf.append(raw[j]); j += 1
            out.append(('str', unescape_js(''.join(buf))))
            out[-1] = out[-1] + (''.join(buf),)  # keep raw form for JSON args
            i = j + 1
        elif raw.startswith('[[', i):
            j = raw.find(']]', i)
            j = n if j < 0 else j
            out.append(('link', raw[i + 2:j]))
            i = j + 2
        else:
            j = i
            while j < n and not raw[j].isspace():
                j += 1
            out.append(('bare', raw[i:j]))
            i = j
    return out


def unescape_js(s):
    """Decode JS string escapes (\\u2019, \\", \\n) without mangling non-ASCII text."""
    try:
        return json.loads('"' + s.replace('"', '\\"').replace('\\\\"', '\\"') + '"')
    except Exception:
        return re.sub(r'\\(.)', r'\1', s)


def strip_comments_and_scripts(t):
    """Remove /* */ comments and script bodies so JS arrays and docs aren't read as links/macros."""
    t = re.sub(r'/\*.*?\*/', '', t, flags=re.S)
    t = re.sub(r'<<script>>.*?<</script>>', '', t, flags=re.S)
    return re.sub(r'<script\b.*?</script>', '', t, flags=re.S)


def wiki_link_target(inner):
    """[[text|target]], [[text->target]], [[target<-text]], [[target]] (setter [..] stripped)."""
    inner = inner.split('][')[0]
    if '|' in inner:
        return inner.split('|', 1)[1].strip()
    if '->' in inner:
        return inner.rsplit('->', 1)[1].strip()
    if '<-' in inner:
        return inner.split('<-', 1)[0].strip()
    return inner.strip()


def try_json(s):
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------- extraction ----------

def extract(path):
    src = open(path, encoding='utf-8').read()

    sd = re.search(r'<tw-storydata([^>]*)>', src)
    if not sd:
        sys.exit('No <tw-storydata> found: is this a compiled Twine/SugarCube HTML file?')
    sd_attrs = sd.group(1)

    raw_passages = re.findall(r'<tw-passagedata([^>]*)>(.*?)</tw-passagedata>', src, re.S)
    us_m = re.search(r'id="twine-user-script"[^>]*>(.*?)</script>', src, re.S)
    userscript = html.unescape(us_m.group(1)) if us_m else ''

    passages = []
    for attrs, body in raw_passages:
        passages.append({
            'pid': int(attr(attrs, 'pid') or 0),
            'name': attr(attrs, 'name'),
            'tags': [t for t in attr(attrs, 'tags').split() if t],
            'text': html.unescape(body),
        })
    names = {p['name'] for p in passages}
    start_pid = int(attr(sd_attrs, 'startnode') or 0)

    # Build identity. Hash = passage names+text + Story JavaScript, so a hash
    # changes exactly when playable content or game code changes.
    h = hashlib.sha256()
    for p in sorted(passages, key=lambda p: p['pid']):
        h.update(p['name'].encode()); h.update(b'\0'); h.update(p['text'].encode()); h.update(b'\0')
    h.update(userscript.encode())
    ver = re.search(r'window\.SOC_VERSION\s*=\s*["\']([^"\']+)', userscript)
    build = {
        'story_name': attr(sd_attrs, 'name'),
        'soc_version': ver.group(1) if ver else None,
        'ifid': attr(sd_attrs, 'ifid'),
        'format': attr(sd_attrs, 'format') + ' ' + attr(sd_attrs, 'format-version'),
        'compiler': (attr(sd_attrs, 'creator') + ' ' + attr(sd_attrs, 'creator-version')).strip(),
        'start_passage': next((p['name'] for p in passages if p['pid'] == start_pid), None),
        'build_hash': h.hexdigest()[:16],
        'source_file': os.path.basename(path),
    }

    # ---- Story JavaScript ----
    macros_defined = sorted(set(re.findall(r"Macro\.add\(\s*['\"]([\w-]+)['\"]", userscript)))
    js_events = collections.Counter(
        re.findall(r"(?:_socTrack|SOC_Playtest\.track|P\.track|this\.track)\(\s*['\"]([\w:-]+)['\"]", userscript))
    # surface + '_reply' style dynamic names
    for suffix in re.findall(r"_socTrack\(\s*surface\s*\+\s*['\"]([\w-]+)['\"]", userscript):
        js_events['{surface}' + suffix] += 1
    ws = re.search(r'WATCHED_STATS\s*=\s*\[(.*?)\]', userscript, re.S)
    watched = re.findall(r"['\"](\w+)['\"]", re.sub(r'/\*.*?\*/', '', ws.group(1), flags=re.S)) if ws else []
    endpoint = re.search(r'SOC_PLAYTEST_ENDPOINT_DEFAULT\s*=\s*["\']([^"\']+)', userscript)
    sugarcube_events = sorted(set(re.findall(r"['\"](:(?:passage|story)\w+)", userscript)))

    # ---- passages ----
    out_passages, inbound = [], collections.defaultdict(set)
    all_choice_points, all_consent, all_track = [], [], collections.Counter()
    var_writes, var_reads = collections.defaultdict(set), collections.defaultdict(set)

    for p in sorted(passages, key=lambda p: p['pid']):
        full, name = p['text'], p['name']
        t = strip_comments_and_scripts(full)
        links, macros_used, track_calls, choices, consent, music = set(), collections.Counter(), [], [], [], []

        for inner in re.findall(r'\[\[(.*?)\]\]', t):
            tgt = wiki_link_target(inner)
            if tgt and not re.search(r'["\n]|https?:', tgt):
                links.add(tgt)
        for tgt in re.findall(r'data-passage=["\']([^"\']+)', t):
            links.add(html.unescape(tgt))
        for ev in re.findall(r"(?:_socTrack|SOC_Playtest\.track)\(\s*['\"]([\w:-]+)['\"]",
                             re.sub(r'/\*.*?\*/', '', full, flags=re.S)):
            track_calls.append({'event': ev, 'detail': None, 'from_script': True})
            all_track[ev] += 1
        for tgt in re.findall(r'Engine\.play\(\s*["\']([^"\']+)', full):
            links.add(tgt)

        for mname, raw, _ in iter_macros(t):
            macros_used[mname] += 1
            args = tokenize_args(raw)
            strs = [a[1] for a in args if a[0] == 'str']
            for a in args:
                if a[0] == 'str' and a[1] in names:
                    links.add(a[1])
                if a[0] == 'link':
                    links.add(wiki_link_target(a[1]))
            if mname == 'soctrack':
                ev = strs[0] if strs else 'event'
                track_calls.append({'event': ev, 'detail': strs[1] if len(strs) > 1 else None})
                all_track[ev] += 1
            elif mname in CHOICE_MACROS:
                # chasechoice style stat label next [skill] / stancechoice group stance label next
                c = {'macro': mname, 'args': strs}
                if mname == 'chasechoice' and len(strs) >= 4:
                    c = {'macro': mname, 'style': strs[0], 'stat': strs[1], 'label': strs[2],
                         'target': strs[3] or None, 'in_place': not strs[3]}
                elif mname == 'stancechoice' and len(strs) >= 4:
                    c = {'macro': mname, 'group': strs[0], 'stance': strs[1], 'label': strs[2], 'target': strs[3]}
                choices.append(c)
            elif mname == 'consentmoment':
                # <<consentmoment id stat delta chatRegister optsJSON>>
                vals = [a[1] for a in args]
                opts = try_json(args[4][2]) if len(args) > 4 and len(args[4]) > 2 else None
                consent.append({
                    'id': vals[0] if vals else '',
                    'stat': vals[1] if len(vals) > 1 else '',
                    'delta': vals[2] if len(vals) > 2 else None,
                    'chat_register': vals[3] if len(vals) > 3 else '',
                    'offered': (opts or {}).get('offered') if isinstance(opts, dict) else None,
                    'chosen': (opts or {}).get('chosen') if isinstance(opts, dict) else None,
                })
            elif mname in ('music', 'musictransition'):
                music.append(strs[:2])

        for v in re.findall(r'<<set\s+\$(\w+)', t):
            var_writes[v].add(name)
        for v in re.findall(r'(?:State\.variables|\bV)\.(\w+)\s*(?:=(?!=)|\+=|-=|\+\+|--)', t):
            var_writes[v].add(name)
        for v in re.findall(r'\$(\w+)', t):
            var_reads[v].add(name)
        # Chat-reply chips and choice JSON change stats via {"stat":"name","delta":n}
        for v in re.findall(r'"stat"\s*:\s*"(\w+)"', t):
            var_writes[v].add(name)

        links.discard(name)
        for tgt in links:
            inbound[tgt].add(name)
        for c in choices:
            all_choice_points.append(dict(c, passage=name))
        consent = [c for c in consent if c['id']]
        for c in consent:
            all_consent.append(dict(c, passage=name))

        data_uris = len(re.findall(r'data:[\w/+.-]+;base64,', t))
        out_passages.append({
            'pid': p['pid'], 'name': name, 'tags': p['tags'],
            'is_start': p['pid'] == start_pid,
            'is_cinematic': 'cine' in p['tags'],
            'is_special': name in ('StoryInit', 'StoryCaption', 'StoryMenu', 'PassageReady', 'PassageDone'),
            'bytes': len(t.encode()), 'embedded_media': data_uris,
            'links_out': sorted(links),
            'macros': dict(macros_used.most_common()),
            'soctrack': track_calls,
            'choice_points': choices,
            'consent_moments': consent,
            'music_cues': music,
        })

    for p in out_passages:
        p['links_in'] = sorted(inbound.get(p['name'], []))

    # Variables set in StoryInit are the declared state model.
    init = next((p for p in passages if p['name'] == 'StoryInit'), None)
    declared = sorted(set(re.findall(r'<<set\s+\$(\w+)', init['text']))) if init else []
    all_vars = sorted(set(var_writes) | set(declared))
    variables = [{
        'name': v,
        'declared_in_storyinit': v in declared,
        'watched_by_telemetry': v in watched,
        'written_in': sorted(var_writes.get(v, [])),
        'read_in': sorted(var_reads.get(v, [])),
    } for v in all_vars]

    # ---- audit ----
    playable = [p for p in out_passages if not p['is_special']]
    audit = []

    def add(severity, code, msg, items):
        if items:
            audit.append({'severity': severity, 'code': code, 'message': msg, 'items': items})

    add('high', 'no_scene_enter',
        'Passages with no <<soctrack "scene_enter">>: drop-off and dwell are invisible here unless a generic passage event is added.',
        [p['name'] for p in playable if not any(c['event'] == 'scene_enter' for c in p['soctrack'])])
    add('medium', 'scene_enter_label_mismatch',
        'scene_enter detail differs from the passage name, so queries keyed on passage name will miss these.',
        [f"{p['name']} -> {c['detail']}" for p in playable for c in p['soctrack']
         if c['event'] == 'scene_enter' and c['detail'] and c['detail'] != p['name']])
    add('high', 'broken_links', 'Links to passages that do not exist.',
        sorted({f"{p['name']} -> {l}" for p in out_passages for l in p['links_out'] if l not in names}))
    add('low', 'no_inbound_links',
        'Passages nothing links to (may be reached by loader jumps, scripts, or be dead).',
        [p['name'] for p in playable if not p['links_in'] and not p['is_start']])
    rel = [v for v in variables
           if re.search(r'(Trust|Bond|Read|Vector|Desire|Count)$', v['name']) and not v['watched_by_telemetry']]
    add('medium', 'unwatched_stats_changing',
        'Relationship-style variables that change during play but are not in WATCHED_STATS, so changes go unlogged.',
        [v['name'] + ' (changed in ' + ', '.join(w for w in v['written_in'] if w != 'StoryInit') + ')'
         for v in rel if any(w != 'StoryInit' for w in v['written_in'])])
    add('low', 'unwatched_stats_static',
        'Relationship-style variables declared but never changed in this build; watch them before they start changing.',
        [v['name'] for v in rel if not any(w != 'StoryInit' for w in v['written_in'])])
    add('low', 'watched_but_unset',
        'In WATCHED_STATS but never set by any passage (may be set from JavaScript).',
        [w for w in watched if w not in var_writes])
    if all_consent:
        add('medium', 'consent_payload_lossy',
            "consentmoment logs a single string ('id / stat=delta'); offered options, the chosen option and decline are not sent.",
            [c['id'] for c in all_consent])
    macros_called = {m for p in out_passages for m in p['macros']}
    add('info', 'macros_defined_not_used',
        'Macros defined in Story JavaScript but not called from any passage in this build.',
        sorted(set(macros_defined) - macros_called))
    add('info', 'heavy_passages',
        'Passages over 150 KB (usually embedded base64 media); slow on mobile, worth an asset_load check.',
        [f"{p['name']} ({p['bytes'] // 1024} KB)" for p in out_passages if p['bytes'] > 150_000])

    return {
        'manifest_version': 1,
        'build': build,
        'telemetry': {
            'endpoint_default': endpoint.group(1) if endpoint else None,
            'watched_stats': watched,
            'sugarcube_events_hooked': sugarcube_events,
            'events_from_javascript': dict(sorted(js_events.items())),
            'events_from_passages': dict(sorted(all_track.items())),
        },
        'counts': {
            'passages': len(out_passages),
            'cinematic_passages': sum(p['is_cinematic'] for p in out_passages),
            'choice_points': len(all_choice_points),
            'consent_moments': len(all_consent),
            'variables': len(variables),
            'macros_defined': len(macros_defined),
        },
        'choice_points': all_choice_points,
        'consent_moments': all_consent,
        'variables': variables,
        'macros_defined': macros_defined,
        'passages': out_passages,
        'audit': audit,
    }


def render_md(m):
    b, c, t = m['build'], m['counts'], m['telemetry']
    L = [f"# Manifest: {b['story_name']}", '',
         f"Build {b['soc_version']} · hash `{b['build_hash']}` · {b['format']} · compiled by {b['compiler']}", '',
         f"Start passage: `{b['start_passage']}`. {c['passages']} passages ({c['cinematic_passages']} cinematic), "
         f"{c['choice_points']} choice points, {c['consent_moments']} consent moments, {c['variables']} variables, "
         f"{c['macros_defined']} macros.", '',
         '## Audit', '']
    for a in m['audit']:
        L.append(f"### [{a['severity']}] {a['code']} ({len(a['items'])})")
        L.append(a['message'])
        L += [f"- {x}" for x in a['items']]
        L.append('')
    L += ['## Consent moments', '', '| Passage | ID | Stat | Delta | Offered |', '| --- | --- | --- | --- | --- |']
    for x in m['consent_moments']:
        L.append(f"| {x['passage']} | {x['id']} | {x['stat']} | {x['delta']} | {'; '.join(x['offered'] or [])} |")
    L += ['', '## Choice points', '', '| Passage | Macro | Option | Target |', '| --- | --- | --- | --- |']
    for x in m['choice_points']:
        opt = x.get('style') or x.get('stance') or ''
        tgt = x.get('target') or ('(stays in passage, reveals text)' if x.get('in_place') else '')
        L.append(f"| {x['passage']} | {x['macro']} | {opt}: {html.unescape(x.get('label', ''))} | {tgt} |")
    L += ['', '## Telemetry events already in the build', '',
          f"Endpoint: `{t['endpoint_default']}`", '',
          f"Watched stats: {', '.join(t['watched_stats'])}", '', '| Event | Source | Call sites |', '| --- | --- | --- |']
    for k, v in t['events_from_javascript'].items():
        L.append(f"| {k} | Story JavaScript | {v} |")
    for k, v in t['events_from_passages'].items():
        L.append(f"| {k} | passage (macro or script) | {v} |")
    L += ['', '## Passages', '', '| PID | Passage | Tags | KB | Links out | Links in |', '| --- | --- | --- | --- | --- | --- |']
    for p in m['passages']:
        L.append(f"| {p['pid']} | {p['name']} | {' '.join(p['tags'])} | {p['bytes'] // 1024} | "
                 f"{len(p['links_out'])} | {len(p['links_in'])} |")
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('html')
    ap.add_argument('--out', default=None, help='output directory (default: next to the HTML)')
    a = ap.parse_args()
    m = extract(a.html)
    out_dir = a.out or os.path.dirname(os.path.abspath(a.html))
    os.makedirs(out_dir, exist_ok=True)
    stem = (m['build']['story_name'] or 'build').replace(' ', '_')
    jp, mp = os.path.join(out_dir, stem + '.manifest.json'), os.path.join(out_dir, stem + '.manifest.md')
    json.dump(m, open(jp, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
    open(mp, 'w', encoding='utf-8').write(render_md(m))
    print(f"{m['counts']['passages']} passages, hash {m['build']['build_hash']}")
    for x in m['audit']:
        print(f"  [{x['severity']}] {x['code']}: {len(x['items'])}")
    print(jp); print(mp)


if __name__ == '__main__':
    main()
