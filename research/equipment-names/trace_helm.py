"""Research scratch: correlate helm .app appearances to Item display names.

Not production code. Run with the repo python; reads d4data JSON only.
"""
import json, glob, os, re
from collections import defaultdict

D = r'C:/Users/ryant/Documents/claude/Projects/diablo4analyzer/d4data/json'
STL = D + '/enUS_Text/meta/StringList'


def item_name(stem):
    """Resolve an item stem -> display name via Item_<stem>.stl."""
    p = os.path.join(STL, f'Item_{stem}.stl.json')
    if not os.path.isfile(p):
        return None
    d = json.load(open(p, encoding='utf-8'))
    for e in d.get('arStrings', []):
        if e.get('szLabel') == 'Name':
            return e.get('szText')
    return None


def main():
    # All Helm items
    items = []
    for f in glob.glob(D + '/base/meta/Item/Helm_*.itm.json'):
        d = json.load(open(f, encoding='utf-8'))
        stem = os.path.basename(f)[:-9]
        items.append((stem, d.get('eComponentStyleType'), d.get('dwComponentStyle')))

    by_style = defaultdict(list)
    for stem, t, s in items:
        by_style[s].append((stem, t))

    print("Total Helm items:", len(items))
    coll = sum(1 for s, v in by_style.items() if len(v) > 1)
    print("distinct dwComponentStyle:", len(by_style),
          " styles with >1 item:", coll)
    for s, v in sorted(by_style.items()):
        if len(v) > 1:
            print("  collision style", s, "->", [x[0] for x in v][:8])

    # barM helm appearances
    apps = []
    for f in glob.glob(D + '/base/meta/Appearance/barM_*_HLM.app.json'):
        n = os.path.basename(f)[:-9]
        m = re.match(r'(?i)barM_([a-z]+)(\d+)_HLM', n)
        if m:
            apps.append((n, m.group(1).lower(), int(m.group(2))))
    print("\nbarM helm appearances:", len(apps))

    matched = unmatched = 0
    samples = []
    for n, tok, num in sorted(apps, key=lambda x: x[2]):
        cand = by_style.get(num, [])
        barb = [c for c in cand if re.search(r'barb', c[0], re.I)]
        if cand:
            matched += 1
            pick = barb[0][0] if barb else cand[0][0]
            if len(samples) < 16:
                samples.append((n, num, pick, item_name(pick), len(cand)))
        else:
            unmatched += 1
            if unmatched <= 6:
                samples.append((n, num, '(no item)', None, 0))
    print("appearances matched by style number:", matched,
          " unmatched:", unmatched)
    print("\n  appearance -> style# -> item -> name (n items at style)")
    for s in samples:
        print("  ", s)


if __name__ == '__main__':
    main()
