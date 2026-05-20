"""Research scratch: full appearance(.app) -> in-game name resolver.

Not production code. Demonstrates the chain so the implementation prompt
has measured hit rates. Reads d4data JSON only; writes a sample dump.
"""
import json, glob, os, re, time
from collections import defaultdict

D = r'C:/Users/ryant/Documents/claude/Projects/diablo4analyzer/d4data/json'
STL = D + '/enUS_Text/meta/StringList'
APP = D + '/base/meta/Appearance'


def L(p):
    return json.load(open(p, encoding='utf-8'))


# ---- name resolution -------------------------------------------------------
def item_name(stem):
    p = os.path.join(STL, f'Item_{stem}.stl.json')
    if not os.path.isfile(p):
        return None
    for e in L(p).get('arStrings', []):
        if e.get('szLabel') == 'Name':
            return e.get('szText')
    return None


# ---- indices ---------------------------------------------------------------
t0 = time.time()
toc = L(D + '/base/CoreTOC.dat.json')
idmap = {}
actor_by_name = {}
for grp, e in toc.items():
    if isinstance(e, dict):
        for sid, name in e.items():
            idmap[sid] = (grp, name)
            if grp == '1':
                actor_by_name[name.lower()] = sid
inc = L(D + '/incomingSnoReferences.json')
print('indices built in %.1fs (idmap=%d)' % (time.time() - t0, len(idmap)))

# armor slot suffix -> actor prefix
ARMOR = {'HLM': 'HLM', 'TRS': 'TRS', 'BDY': 'TRS', 'GLV': 'GLV',
         'LEG': 'LEG', 'BTS': 'BTS'}
# cosmetic style tokens have no <SLOT>_<tok><num> actor
COSMETIC_TOKENS = {'stor', 'dlux', 'dulx', 'pvpa'}
CLASS_PREFIX = {'bar': 'barb', 'dru': 'druid', 'nec': 'necro', 'pal': 'pal',
                'rog': 'rogue', 'sor': 'sorc', 'spi': 'spirit', 'war': 'warlock'}

# Build cosmetic-item index: (slotword, classword, number) -> item stem.
cos_index = {}
for f in glob.glob(D + '/base/meta/Item/*_Cosmetic_*.itm.json'):
    stem = os.path.basename(f)[:-9]
    m = re.match(r'(?i)(Helm|Chest|Gloves|Pants|Boots)_Cosmetic_([A-Za-z]+)_'
                 r'(?:[a-z]+)?(\d+)', stem)
    if m:
        slot, cls, num = m.group(1).lower(), m.group(2).lower(), int(m.group(3))
        cls = cls.replace('barbarian', 'barb').replace('necromancer', 'necro')
        cls = cls.replace('sorcerer', 'sorc').replace('spiritborn', 'spirit')
        cos_index[(slot, cls, num)] = stem
print('cosmetic-item index entries:', len(cos_index))

SLOTWORD = {'HLM': 'helm', 'TRS': 'chest', 'BDY': 'chest', 'GLV': 'gloves',
            'LEG': 'pants', 'BTS': 'boots'}


def resolve_armor(cg, tok, num, slot):
    """Return (items, method) for an armor appearance."""
    cls_pref = cg[:3].lower()
    if tok in COSMETIC_TOKENS:
        cw = CLASS_PREFIX.get(cls_pref)
        stem = cos_index.get((SLOTWORD[slot], cw, num))
        if stem:
            return [stem], 'cosmetic-name'
        return [], 'cosmetic-miss'
    actor = f'{ARMOR[slot]}_{tok}{num:02d}'
    aid = actor_by_name.get(actor.lower())
    if aid is None:
        actor = f'{ARMOR[slot]}_{tok}{num}'
        aid = actor_by_name.get(actor.lower())
    if aid is None:
        return [], 'no-actor'
    items = [idmap[str(r)][1] for r in inc.get(aid, [])
             if idmap.get(str(r), ('', ''))[0] == '73']
    return items, 'actor-ref' if items else 'actor-no-item'


def resolve_weapon(stem):
    """stem is the appearance stem, e.g. axe_uniq01."""
    aid = actor_by_name.get(stem.lower())
    if aid is None:
        return [], 'no-actor'
    items = [idmap[str(r)][1] for r in inc.get(aid, [])
             if idmap.get(str(r), ('', ''))[0] == '73']
    return items, 'actor-ref' if items else 'actor-no-item'


# ---- run over barM armor + weapons ----------------------------------------
def run():
    results = []
    stats = defaultdict(int)
    for slot in ('HLM', 'TRS', 'GLV', 'LEG', 'BTS'):
        for f in sorted(glob.glob(f'{APP}/barM_*_{slot}.app.json')):
            n = os.path.basename(f)[:-9]
            m = re.match(rf'(?i)(barM)_([a-z]+)(\d+)_{slot}', n)
            if not m:
                stats['unparsed'] += 1
                continue
            cg, tok, num = m.group(1), m.group(2).lower(), int(m.group(3))
            items, method = resolve_armor(cg, tok, num, slot)
            names = [(it, item_name(it)) for it in items]
            named = [x for x in names if x[1]]
            stats[method] += 1
            stats['named' if named else 'unnamed'] += 1
            results.append((n, slot, tok, num, method, named or names))

    weapons = []
    for pat in ('axe_*', 'sword_*', 'wand_*', 'mace_*', 'dagger_*',
                'staff_*', 'bow_*', 'crossbow_*', 'polearm_*', 'scythe_*',
                'shield_*'):
        for f in sorted(glob.glob(f'{APP}/{pat}.app.json')):
            n = os.path.basename(f)[:-9]
            items, method = resolve_weapon(n)
            names = [(it, item_name(it)) for it in items]
            named = [x for x in names if x[1]]
            stats['w_' + method] += 1
            stats['w_named' if named else 'w_unnamed'] += 1
            weapons.append((n, method, named or names))

    print('\n=== ARMOR (barM) stats ===')
    for k in sorted(stats):
        if not k.startswith('w_'):
            print(f'  {k}: {stats[k]}')
    print('=== WEAPON stats ===')
    for k in sorted(stats):
        if k.startswith('w_'):
            print(f'  {k}: {stats[k]}')

    print('\n--- armor samples (one per token/slot) ---')
    seen = set()
    for r in results:
        key = (r[1], r[2])
        if key in seen:
            continue
        seen.add(key)
        print(f'  {r[0]:<26} {r[4]:<14} -> {r[5][:2]}')
    print('\n--- weapon samples ---')
    for w in weapons[:14]:
        print(f'  {w[0]:<22} {w[1]:<13} -> {w[2][:2]}')

    json.dump(
        {'armor': [list(r[:5]) + [r[5]] for r in results],
         'weapons': [list(w) for w in weapons]},
        open(os.path.join(os.path.dirname(__file__), 'sample_resolved.json'),
             'w', encoding='utf-8'),
        indent=1, ensure_ascii=False)
    print('\nwrote sample_resolved.json')


if __name__ == '__main__':
    run()
