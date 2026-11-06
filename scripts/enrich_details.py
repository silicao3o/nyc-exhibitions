#!/usr/bin/env python3
"""collect_exhibitions.py 결과(data/exhibitions.json)에 상세 설명과 전화번호를 덧붙인다.

- 상세 설명: musa.guide 전시 페이지를 `Accept: text/markdown`으로 받아
  소개글(Preview), 에디터 추천평, 관람 포인트(Look for), 추천 대상(Good for),
  동네, 입장료, 공식 전시 페이지 링크를 뽑는다.
- 전화번호: 장소 페이지의 Wikidata 링크(sameAs)에서 P1329를 읽고,
  없으면 공식 홈페이지의 JSON-LD telephone 또는 tel: 링크 중 해당 도시 번호를 쓴다.

사용 예:
  python3 scripts/enrich_details.py                    # 뉴욕
  python3 scripts/enrich_details.py --cities paris seoul
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collect_exhibitions import BASE, UA, Fetcher, json_ld, log, walk  # noqa: E402

# 홈페이지 tel: 링크는 다른 지점 번호일 수 있어 도시 지역번호로 거른다
AREA_CODES = {
    'new-york': ('212', '646', '332', '718', '347', '929', '917'),
}


def section(md: str, heading: str) -> str:
    m = re.search(rf'^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)', md, re.S | re.M)
    return m.group(1).strip() if m else ''


def bullets(block: str) -> list[dict]:
    """'-   제목\\n\\n    본문' 형태의 목록을 [{title, text}]로 바꾼다."""
    items = []
    for chunk in re.split(r'^-\s+', block, flags=re.M)[1:]:
        lines = [l.strip() for l in chunk.strip().split('\n') if l.strip()]
        if lines:
            items.append(dict(title=lines[0], text=' '.join(lines[1:])))
    return items


def strip_utm(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith('utm_')]
    return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(q)))


def parse_exhibition_md(md: str) -> dict:
    out = {}
    m = re.search(r'^\[[^\]]+\]\(https://www\.musa\.guide/en/discover/[^)]+\)([^\n\[]+)$', md, re.M)
    out['neighborhood'] = m.group(1).strip() if m else ''
    # "Now showing25 October 2026 – 27 February 2027$30" → 날짜 뒤 꼬리가 입장료
    m = re.search(r'^(?:Now showing|Upcoming|Closing soon|Last chance)?.*?\d{4}\s*–\s*\d{1,2} \w+ \d{4}(.*)$', md, re.M)
    out['price'] = m.group(1).strip() if m else ''
    m = re.search(r'\[Exhibition website ↗\]\(([^)]+)\)', md)
    out['official_url'] = strip_utm(m.group(1)) if m else ''
    m = re.search(r'^Preview:[^\n]*\n(.*?)(?=^## |\Z)', md, re.S | re.M)
    if not m:
        m = re.search(r'^## More in [^\n]*\n.*?\n\n(?:[^\n]*\n\n)?(.*?)(?=^## |\Z)', md, re.S | re.M)
    body = m.group(1).strip() if m else ''
    out['long_description'] = re.sub(r'\n{3,}', '\n\n', body)
    out['editors_pick'] = section(md, "Editor's pick")
    out['look_for'] = bullets(section(md, 'Look for'))
    out['good_for'] = [b['title'] for b in bullets(section(md, 'Good for'))]
    return out


def wikidata_phones(qids: list[str]) -> dict:
    out = {}
    for i in range(0, len(qids), 50):
        url = ('https://www.wikidata.org/w/api.php?action=wbgetentities&props=claims&format=json&ids='
               + '|'.join(qids[i:i + 50]))
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        entities = json.load(urllib.request.urlopen(req, timeout=30))['entities']
        for qid, ent in entities.items():
            phones = [c['mainsnak'].get('datavalue', {}).get('value')
                      for c in ent.get('claims', {}).get('P1329', [])]
            if phones and phones[0]:
                out[qid] = phones[0]
    return out


def homepage_phone(f: Fetcher, url: str, area_codes: tuple) -> str:
    page = f.get(url) or ''
    candidates = []
    for d in json_ld(page):
        for n in walk(d):
            if n.get('telephone'):
                candidates.append(str(n['telephone']))
    candidates += [urllib.parse.unquote(t) for t in re.findall(r'href="tel:([^"]+)"', page)]
    for c in candidates:
        digits = re.sub(r'\D', '', c)
        local = digits[1:] if len(digits) == 11 and digits.startswith('1') else digits
        if len(local) == 10 and (not area_codes or local[:3] in area_codes):
            return f'+1 ({local[:3]}) {local[3:6]}-{local[6:]}'
    return ''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cities', nargs='*', default=['new-york'])
    ap.add_argument('--data', default='data/exhibitions.json')
    ap.add_argument('--delay', type=float, default=1.0)
    ap.add_argument('--cache-ttl', type=float, default=20)
    args = ap.parse_args()

    path = Path(args.data)
    data = json.loads(path.read_text())
    f = Fetcher(path.parent / '.cache', args.delay, args.cache_ttl)
    targets = [e for e in data['exhibitions'] if e['city'] in args.cities]
    log(f'{len(targets)} exhibitions in {", ".join(args.cities)}')

    # 1) 전시 상세 (마크다운)
    for i, e in enumerate(targets, 1):
        md = f.get(e['url'], accept='text/markdown') or ''
        e.update(parse_exhibition_md(md))
        if i % 20 == 0:
            log(f'  details {i}/{len(targets)}')

    # 2) 장소 전화번호: Wikidata → 공식 홈페이지
    venues = {}
    for e in targets:
        slug = e['url'].split('/exhibitions/')[1].split('/')[0]
        venues.setdefault(e['venue'], dict(city=e['city'], url=f"{BASE}/en/discover/{e['city']}/{slug}",
                                           homepage=e['venue_homepage']))
    for v in venues.values():
        v['qid'] = ''
        for d in json_ld(f.get(v['url']) or ''):
            for n in walk(d):
                for s in n.get('sameAs') or [] if n.get('address') else []:
                    m = re.search(r'wikidata\.org/wiki/(Q\d+)', s)
                    if m:
                        v['qid'] = m.group(1)
    wd = wikidata_phones([v['qid'] for v in venues.values() if v['qid']])
    for name, v in venues.items():
        v['phone'], v['phone_source'] = wd.get(v['qid'], ''), 'wikidata' if v['qid'] in wd else ''
        if not v['phone'] and v['homepage']:
            v['phone'] = homepage_phone(f, v['homepage'], AREA_CODES.get(v['city'], ()))
            v['phone_source'] = 'homepage' if v['phone'] else ''
        log(f"  {name[:40]:40} {v['phone'] or '-':20} {v['phone_source']}")
    for e in targets:
        v = venues[e['venue']]
        e['venue_phone'], e['venue_phone_source'] = v['phone'], v['phone_source']

    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    filled = lambda k: sum(1 for e in targets if e.get(k))
    log(f"done ({f.requests} requests): long_description {filled('long_description')}, "
        f"look_for {filled('look_for')}, official_url {filled('official_url')}, "
        f"price {filled('price')}, phone {filled('venue_phone')} / {len(targets)}")


if __name__ == '__main__':
    main()
