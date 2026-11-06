#!/usr/bin/env python3
"""musa.guide Discover 디렉터리에서 전 세계 도시의 진행 중·예정 전시를 수집한다.

musa.guide는 공개 API가 없지만 Discover 페이지마다 schema.org JSON-LD를 싣는다.
- 도시 목록:   /en/discover
- 전시 목록:   /en/discover/{city}/exhibitions, /en/discover/{city}/exhibitions/{month}-{year}
- 장소 상세:   /en/discover/{city}/{venue}  (주소, 좌표, 운영시간, 입장료, 홈페이지, 전시 설명)

사용 예:
  python3 scripts/collect_exhibitions.py                       # 전체 도시, 오늘 기준 3개월
  python3 scripts/collect_exhibitions.py --cities paris tokyo  # 일부 도시만
  python3 scripts/collect_exhibitions.py --months 1 --delay 2
"""
import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

BASE = 'https://www.musa.guide'
UA = 'Mozilla/5.0 (compatible; exhibition-collector/1.0; +https://github.com/silicao3o/nyc-exhibitions)'
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DAYS_KO = '월화수목금토일'
MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july',
          'august', 'september', 'october', 'november', 'december']

CITY_KO = {
    'amsterdam': '암스테르담', 'athens': '아테네', 'barcelona': '바르셀로나', 'berlin': '베를린',
    'boston': '보스턴', 'cairo': '카이로', 'chicago': '시카고', 'copenhagen': '코펜하겐',
    'edinburgh': '에든버러', 'florence': '피렌체', 'istanbul': '이스탄불', 'kyoto': '교토',
    'lisbon': '리스본', 'london': '런던', 'los-angeles': '로스앤젤레스', 'madrid': '마드리드',
    'mexico-city': '멕시코시티', 'milan': '밀라노', 'munich': '뮌헨', 'new-york': '뉴욕',
    'paris': '파리', 'philadelphia': '필라델피아', 'prague': '프라하', 'rome': '로마',
    'san-francisco': '샌프란시스코', 'seoul': '서울', 'singapore': '싱가포르', 'stockholm': '스톡홀름',
    'sydney': '시드니', 'tokyo': '도쿄', 'toronto': '토론토', 'venice': '베네치아',
    'vienna': '빈', 'washington-dc': '워싱턴 D.C.',
}
COUNTRY_KO = {
    'US': '미국', 'GB': '영국', 'FR': '프랑스', 'DE': '독일', 'IT': '이탈리아', 'ES': '스페인',
    'NL': '네덜란드', 'AT': '오스트리아', 'GR': '그리스', 'EG': '이집트', 'DK': '덴마크',
    'TR': '튀르키예', 'JP': '일본', 'PT': '포르투갈', 'MX': '멕시코', 'CZ': '체코',
    'KR': '한국', 'SG': '싱가포르', 'SE': '스웨덴', 'AU': '호주', 'CA': '캐나다',
}
# musa.guide는 갤러리도 Museum 타입으로 내보내므로 이름으로 추정한다
GALLERY_RE = re.compile(r'galler|galerie|galería|galleria|gagosian|zwirner|hauser|pace\b|newbury|white cube|lisson|perrotin|thaddaeus', re.I)


class Fetcher:
    """디스크 캐시 + 요청 간격 + 429/5xx 재시도."""

    def __init__(self, cache_dir: Path, delay: float, ttl_hours: float):
        self.cache_dir = cache_dir
        self.delay = delay
        self.ttl = ttl_hours * 3600
        self.last = 0.0
        self.requests = 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url: str, accept: str | None = None) -> str | None:
        key = url if accept is None else f'{accept} {url}'
        path = self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + '.html')
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            return path.read_text(errors='ignore')
        for attempt in range(4):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.requests += 1
            try:
                headers = {'User-Agent': UA, 'Accept-Language': 'en'}
                if accept:
                    headers['Accept'] = accept
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode('utf-8', errors='ignore')
                path.write_text(body)
                return body
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt * 5)
                    continue
                log(f'  ! HTTP {e.code} {url}')
                return None
            except (urllib.error.URLError, TimeoutError) as e:
                log(f'  ! {e} {url}')
                time.sleep(2 ** attempt * 2)
        return None


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def json_ld(page: str) -> list:
    out = []
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    return out


def walk(node):
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            yield x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def types(node: dict) -> list:
    t = node.get('@type')
    return t if isinstance(t, list) else [t]


def image_url(img) -> str:
    if isinstance(img, dict):
        return img.get('contentUrl') or img.get('url') or ''
    return img or ''


def hours_text(spec: list) -> str:
    """요일별 운영시간을 '월-목 10:30 - 17:30' 형태로 묶는다. 빠진 요일은 휴관."""
    if not spec:
        return ''
    by_day = {}
    for h in spec:
        days = h.get('dayOfWeek')
        for d in days if isinstance(days, list) else [days]:
            if d and h.get('opens'):
                by_day[d.split('/')[-1]] = f"{h['opens']} - {h['closes']}"
    out, i = [], 0
    while i < 7:
        v, j = by_day.get(DAYS[i], '휴관'), i
        while j + 1 < 7 and by_day.get(DAYS[j + 1], '휴관') == v:
            j += 1
        out.append(f"{DAYS_KO[i]}{'' if i == j else '-' + DAYS_KO[j]} {v}")
        i = j + 1
    return '\n'.join(out)


def list_cities(f: Fetcher) -> list[str]:
    page = f.get(f'{BASE}/en/discover') or ''
    return sorted(set(re.findall(r'href="/en/discover/([a-z0-9-]+)"', page)))


def month_slugs(as_of: date, months: int) -> list[str]:
    y, m = as_of.year, as_of.month
    out = []
    for _ in range(months + 1):
        out.append(f'{MONTHS[m - 1]}-{y}')
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def collect_city(f: Fetcher, city: str, as_of: date, months: int) -> list[dict]:
    events, venue_urls = {}, {}
    pages = [f'{BASE}/en/discover/{city}/exhibitions'] + \
            [f'{BASE}/en/discover/{city}/exhibitions/{s}' for s in month_slugs(as_of, months)]
    for url in pages:
        for d in json_ld(f.get(url) or ''):
            for n in walk(d):
                if n.get('@type') != 'ExhibitionEvent':
                    continue
                loc = n.get('location') or {}
                venue = html.unescape(loc.get('name', '')) if isinstance(loc, dict) else ''
                key = (html.unescape(n.get('name', '')), venue)
                if isinstance(loc, dict) and loc.get('url'):
                    venue_urls[venue] = loc['url']
                events.setdefault(key, dict(
                    title=key[0], venue=venue,
                    start_date=(n.get('startDate') or '')[:10], end_date=(n.get('endDate') or '')[:10],
                    url=n.get('url', ''), image_url=image_url(n.get('image')),
                ))

    venues = {}
    for venue, url in venue_urls.items():
        info = {}
        for d in json_ld(f.get(url) or ''):
            for n in walk(d):
                if not any(t in ('Museum', 'TouristAttraction', 'ArtGallery') for t in types(n)) or not n.get('address'):
                    continue
                addr = n['address'] if isinstance(n['address'], dict) else {'streetAddress': n['address']}
                geo = n.get('geo') or {}
                info = dict(
                    address=addr.get('streetAddress', ''), country=addr.get('addressCountry', ''),
                    lat=geo.get('latitude'), lng=geo.get('longitude'),
                    hours=hours_text(n.get('openingHoursSpecification') or []),
                    phone=n.get('telephone', ''), price=n.get('priceRange', ''), homepage=n.get('url', ''),
                    events={html.unescape(e.get('name', '')): e for e in n.get('event') or [] if isinstance(e, dict)},
                )
        venues[venue] = info

    rows = []
    for (title, venue), e in events.items():
        v = venues.get(venue, {})
        ev = v.get('events', {}).get(title, {})
        thumb = e['image_url']
        rows.append(dict(
            e,
            city=city, city_ko=CITY_KO.get(city, city),
            country_code=v.get('country', ''), country_ko=COUNTRY_KO.get(v.get('country', ''), v.get('country', '')),
            venue_type='gallery' if GALLERY_RE.search(venue) else 'museum',
            description=ev.get('description', ''),
            # 목록 썸네일은 -w256이라 카드용 -w768로 올린다 (없으면 프론트에서 -w256으로 폴백)
            image_url=re.sub(r'-w256\.webp$', '-w768.webp', thumb),
            image_full=image_url(ev.get('image')) or re.sub(r'-w\d+\.webp$', '.webp', thumb),
            venue_address=v.get('address', ''), venue_lat=v.get('lat'), venue_lng=v.get('lng'),
            venue_hours=v.get('hours', ''), venue_phone=v.get('phone', ''),
            venue_price=v.get('price', ''), venue_homepage=v.get('homepage', ''),
            source='musa.guide',
        ))
    # 장소 페이지가 비어 국가를 못 얻은 전시는 같은 도시의 다수 국가로 채운다
    common = Counter(r['country_code'] for r in rows if r['country_code']).most_common(1)
    for r in rows:
        if not r['country_code'] and common:
            r['country_code'] = common[0][0]
            r['country_ko'] = COUNTRY_KO.get(common[0][0], common[0][0])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cities', nargs='*', help='도시 slug (기본: Discover의 전체 도시)')
    ap.add_argument('--as-of', default=date.today().isoformat(), help='기준일 YYYY-MM-DD (기본: 오늘)')
    ap.add_argument('--months', type=int, default=3, help='예정 전시를 찾을 개월 수 (기본: 3)')
    ap.add_argument('--closing-days', type=int, default=14, help='closing_soon 기준 일수 (기본: 14)')
    ap.add_argument('--delay', type=float, default=1.0, help='요청 간격(초) (기본: 1.0)')
    ap.add_argument('--cache-ttl', type=float, default=20, help='캐시 유효 시간(시간) (기본: 20)')
    ap.add_argument('--out', default='data', help='출력 디렉터리 (기본: data)')
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of)
    out = Path(args.out)
    f = Fetcher(out / '.cache', args.delay, args.cache_ttl)
    cities = args.cities or list_cities(f)
    log(f'{len(cities)} cities, as of {as_of}')

    today, soon = as_of.isoformat(), (as_of + timedelta(days=args.closing_days)).isoformat()
    all_rows = []
    for i, city in enumerate(cities, 1):
        rows = collect_city(f, city, as_of, args.months)
        # 이미 끝난 전시만 제외한다 (기준일 당일 종료 포함)
        rows = [r for r in rows if not r['end_date'] or r['end_date'] >= today]
        for r in rows:
            r['status'] = 'upcoming' if r['start_date'] > today else 'on_view'
            r['closing_soon'] = r['status'] == 'on_view' and bool(r['end_date']) and r['end_date'] <= soon
        rows.sort(key=lambda r: (r['status'] != 'on_view',
                                 r['start_date'] if r['status'] == 'upcoming' else (r['end_date'] or '9999')))
        all_rows += rows
        log(f'[{i}/{len(cities)}] {city}: {len(rows)} exhibitions ({f.requests} requests so far)')

    keys = ['id', 'status', 'closing_soon', 'title', 'venue', 'venue_type', 'city', 'city_ko', 'country_code',
            'country_ko', 'start_date', 'end_date', 'url', 'image_url', 'image_full', 'description',
            'venue_address', 'venue_lat', 'venue_lng', 'venue_hours', 'venue_phone', 'venue_price',
            'venue_homepage', 'source']
    for i, r in enumerate(all_rows, 1):
        r['id'] = i
    all_rows = [{k: r.get(k, '') for k in keys} for r in all_rows]

    out.mkdir(parents=True, exist_ok=True)
    meta = dict(as_of=today, generated_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), source=f'{BASE}/en/discover',
                cities=len(cities), count=len(all_rows))
    (out / 'exhibitions.json').write_text(json.dumps(dict(meta, exhibitions=all_rows), ensure_ascii=False, indent=1))
    with open(out / 'exhibitions.csv', 'w', newline='') as fp:
        w = csv.DictWriter(fp, fieldnames=keys)
        w.writeheader()
        w.writerows(all_rows)

    by_country = Counter(r['country_ko'] or '?' for r in all_rows)
    by_status = Counter(r['status'] for r in all_rows)
    log(f'done: {len(all_rows)} exhibitions, {dict(by_status)}, {f.requests} requests')
    log('by country: ' + ', '.join(f'{k} {v}' for k, v in by_country.most_common()))


if __name__ == '__main__':
    main()
