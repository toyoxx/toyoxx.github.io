import difflib
import glob
import html
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path('.')
FILES = sorted([Path(p) for p in glob.glob('_publications/*.md')] + [Path(p) for p in glob.glob('_talks/*.md')])
UA = 'Mozilla/5.0 (Codex abstract filler)'


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def http_get_json(url):
    try:
        return json.loads(http_get(url))
    except Exception:
        return None


def split_frontmatter(text):
    if not text.startswith('---\n'):
        return '', text, ''
    parts = text.split('\n---\n', 1)
    if len(parts) != 2:
        return '', text, ''
    front = parts[0][4:]
    body = parts[1]
    return '---\n', front, body


def parse_frontmatter(front):
    data = {}
    for line in front.splitlines():
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if v.startswith("'") and v.endswith("'") and len(v) >= 2:
            v = v[1:-1]
        elif v.startswith('"') and v.endswith('"') and len(v) >= 2:
            v = v[1:-1]
        data[k] = v
    return data


def has_abstract(path, body):
    if path.parts[0] == '_publications':
        return re.search(r'(?m)^Abstract\s*$', body) is not None
    return re.search(r'(?m)^Paper Abstract\s*$', body) is not None


def jats_to_text(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def inv_index_to_text(inv):
    if not inv:
        return None
    max_pos = -1
    for positions in inv.values():
        if positions:
            max_pos = max(max_pos, max(positions))
    if max_pos < 0:
        return None
    arr = [''] * (max_pos + 1)
    for word, positions in inv.items():
        for p in positions:
            if 0 <= p < len(arr):
                arr[p] = word
    txt = ' '.join(arr)
    txt = txt.replace(' ,', ',').replace(' .', '.').replace(' ;', ';').replace(' :', ':')
    txt = txt.replace(' )', ')').replace('( ', '(')
    txt = txt.replace(" n't", "n't")
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt


def title_similarity(a, b):
    return difflib.SequenceMatcher(None, (a or '').lower(), (b or '').lower()).ratio()


def get_crossref_by_doi(doi):
    doi_enc = urllib.parse.quote(doi, safe='')
    data = http_get_json(f'https://api.crossref.org/works/{doi_enc}')
    if not data or 'message' not in data:
        return None
    msg = data['message']
    abstract = jats_to_text(msg.get('abstract')) if msg.get('abstract') else None
    title = (msg.get('title') or [''])[0]
    return {'title': title, 'abstract': abstract}


def search_crossref(title):
    q = urllib.parse.quote(title)
    data = http_get_json(f'https://api.crossref.org/works?query.title={q}&rows=5')
    if not data:
        return None
    items = data.get('message', {}).get('items', [])
    best = None
    for it in items:
        it_title = (it.get('title') or [''])[0]
        sim = title_similarity(title, it_title)
        candidate = {
            'title': it_title,
            'doi': it.get('DOI'),
            'abstract': jats_to_text(it.get('abstract')) if it.get('abstract') else None,
            'sim': sim,
        }
        if best is None or candidate['sim'] > best['sim']:
            best = candidate
    return best


def get_openalex_by_doi(doi):
    doi_url = 'https://doi.org/' + doi.lower()
    url = 'https://api.openalex.org/works/' + urllib.parse.quote(doi_url, safe='')
    data = http_get_json(url)
    if not data:
        return None
    return {
        'title': data.get('display_name') or '',
        'abstract': inv_index_to_text(data.get('abstract_inverted_index')),
    }


def search_openalex(title):
    q = urllib.parse.quote(title)
    data = http_get_json(f'https://api.openalex.org/works?search={q}&per-page=5')
    if not data:
        return None
    results = data.get('results', [])
    best = None
    for it in results:
        it_title = it.get('display_name') or ''
        sim = title_similarity(title, it_title)
        candidate = {
            'title': it_title,
            'doi': (it.get('doi') or '').replace('https://doi.org/', ''),
            'abstract': inv_index_to_text(it.get('abstract_inverted_index')),
            'sim': sim,
        }
        if best is None or candidate['sim'] > best['sim']:
            best = candidate
    return best


def search_semanticscholar(title):
    q = urllib.parse.quote(title)
    data = http_get_json(
        f'https://api.semanticscholar.org/graph/v1/paper/search?query={q}&limit=5&fields=title,abstract,externalIds'
    )
    if not data:
        return None
    best = None
    for it in data.get('data', []):
        it_title = it.get('title') or ''
        sim = title_similarity(title, it_title)
        ext = it.get('externalIds') or {}
        candidate = {
            'title': it_title,
            'doi': ext.get('DOI'),
            'abstract': (it.get('abstract') or '').strip() or None,
            'sim': sim,
        }
        if best is None or candidate['sim'] > best['sim']:
            best = candidate
    return best


def fetch_abstract(title, doi=None):
    tried = []
    if doi:
        doi = doi.strip().lower()
        for fn, name in ((get_openalex_by_doi, 'openalex-doi'), (get_crossref_by_doi, 'crossref-doi')):
            try:
                res = fn(doi)
            except Exception:
                res = None
            tried.append(name)
            if res and res.get('abstract'):
                return res['abstract'], name, res
            time.sleep(0.2)

    for fn, name in ((search_openalex, 'openalex-search'), (search_crossref, 'crossref-search'), (search_semanticscholar, 'semanticscholar-search')):
        try:
            res = fn(title)
        except Exception:
            res = None
        tried.append(name)
        if res and res.get('sim', 0) >= 0.75 and res.get('abstract'):
            return res['abstract'], name, res
        time.sleep(0.2)

    return None, ','.join(tried), None


def append_publication_abstract(body, abstract):
    body = body.rstrip() + '\n\n'
    return body + 'Abstract\n:\t' + abstract.strip() + '\n'


def append_talk_abstract(body, abstract, link=None):
    chunks = []
    base = body.rstrip()
    if base:
        chunks.append(base)
    if not re.search(r'(?m)^### This presentation was based on a peer-reviewed paper', body):
        if link:
            chunks.append(f'### This presentation was based on a peer-reviewed paper, which can be found [here]({link})')
        else:
            chunks.append('### This presentation was based on a peer-reviewed paper')
    chunks.append('------')
    chunks.append('Paper Abstract\n:\t' + abstract.strip())
    return '\n\n'.join(chunks).rstrip() + '\n'


def normalize_abstract(text):
    text = html.unescape(text or '')
    text = text.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def main():
    updated = []
    unresolved = []
    for path in FILES:
        txt = path.read_text(encoding='utf-8')
        prefix, front, body = split_frontmatter(txt)
        if not prefix:
            unresolved.append((str(path), 'no frontmatter'))
            continue
        if has_abstract(path, body):
            continue
        meta = parse_frontmatter(front)
        title = meta.get('title', '').strip()
        if not title:
            unresolved.append((str(path), 'no title'))
            continue
        doi = (meta.get('doi') or '').strip()
        abstract, source, res = fetch_abstract(title, doi=doi or None)
        if not abstract:
            unresolved.append((str(path), f'abstract not found ({source})'))
            continue
        abstract = normalize_abstract(abstract)
        if path.parts[0] == '_publications':
            new_body = append_publication_abstract(body, abstract)
        else:
            link = meta.get('paperurl') or meta.get('url') or None
            new_body = append_talk_abstract(body, abstract, link=link)
        new_txt = prefix + front + '\n---\n' + new_body
        path.write_text(new_txt, encoding='utf-8')
        updated.append((str(path), source))
        print(f'UPDATED {path} via {source}')
        time.sleep(0.2)

    print('\nSUMMARY')
    print(f'Updated: {len(updated)}')
    print(f'Unresolved: {len(unresolved)}')
    if unresolved:
        for p, why in unresolved:
            print(f'UNRESOLVED {p}: {why}')


if __name__ == '__main__':
    main()
