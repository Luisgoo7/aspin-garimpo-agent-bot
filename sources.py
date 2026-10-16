from datetime import date, timedelta
import json
import logging
import httpx
from .config import settings
from .opportunity import KEYWORDS

log = logging.getLogger(__name__)
DJEN_URL = 'https://comunicaapi.pje.jus.br/api/v1/comunicacao'
TJSP_URLS = {
    'tjsp_requisitorios': 'https://esaj.tjsp.jus.br/esaj?servico=950000',
    'tjsp_precatorios': 'https://www.tjsp.jus.br/Precatorios',
}

async def search_djen(since: date, until: date) -> list[dict]:
    rows = []
    async with httpx.AsyncClient(timeout=45, headers={'User-Agent': 'ASPIN-Garimpo/1.0'}) as client:
        for term in KEYWORDS:
            params = {'texto': term, 'dataDisponibilizacaoInicio': since.isoformat(), 'dataDisponibilizacaoFim': until.isoformat(), 'itensPorPagina': 100, 'pagina': 1, 'meio': 'D'}
            try:
                response = await client.get(DJEN_URL, params=params)
            except httpx.HTTPError:
                continue
            if response.status_code == 429:
                break
            if response.status_code >= 400:
                continue
            payload = response.json()
            rows.extend(payload.get('items') or [])
    unique = {}
    for row in rows:
        key = str(row.get('hash') or row.get('id') or row.get('numeroComunicacao'))
        unique[key] = row
    return list(unique.values())

async def probe_tjsp_pages() -> list[dict]:
    rows = []
    async with httpx.AsyncClient(timeout=30, headers={'User-Agent': 'ASPIN-Garimpo/1.0'}) as client:
        for source, url in TJSP_URLS.items():
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("TJSP %s indisponivel: %s", source, exc)
                continue
            text = response.text
            rows.append({'source': source, 'external_id': url, 'title': source, 'text_excerpt': text[:4000], 'document_url': url, 'raw_data': {'status_code': response.status_code, 'content_length': len(text)}})
    return rows

def persist_source(row: dict) -> bool:
    """Grava o documento. Retorna True se for NOVO (nunca visto antes)."""
    if not settings.database_url:
        return True
    import psycopg
    source = row.get('source') or 'djen'
    external_id = str(row.get('external_id') or row.get('hash') or row.get('id') or row.get('numeroComunicacao') or row.get('link'))
    with psycopg.connect(settings.database_url) as conn:
        cur = conn.execute('''insert into public.source_documents(source,tribunal_alias,external_id,process_number,published_at,title,text_excerpt,document_url,raw_data) values(%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(source,external_id) do update set text_excerpt=excluded.text_excerpt,raw_data=excluded.raw_data returning (xmax = 0)''', (source, row.get('siglaTribunal'), external_id, row.get('numero_processo') or row.get('numeroProcesso'), row.get('data_disponibilizacao') or row.get('dataDisponibilizacao'), row.get('tipoComunicacao') or row.get('title'), row.get('texto') or row.get('text_excerpt') or '', row.get('link') or row.get('document_url'), json.dumps(row)))
        return bool(cur.fetchone()[0])
