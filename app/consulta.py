"""Comando /processo: consulta sob demanda de um número CNJ (DataJud + DJEN + histórico do bot).

Regras de produção das APIs do CNJ seguidas aqui (ref.: rvsanches/skills-datajud-djen, MIT):
- consulta sempre pelo alias do tribunal derivado do número CNJ (nunca o curinga);
- resposta 200 com shards falhados e zero hits NÃO é "não encontrado" (é indisponibilidade);
- datas de 14 dígitos estão em horário de Brasília.
"""
import asyncio
import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings
from .datajud import MOVEMENT_EXPEDICAO_PRECATORIO_RPV, DataJudClient, DataJudError

BRT = timezone(timedelta(hours=-3))
TR_UF = {
    "01": "ac", "02": "al", "03": "ap", "04": "am", "05": "ba", "06": "ce", "07": "dft", "08": "es",
    "09": "go", "10": "ma", "11": "mt", "12": "ms", "13": "mg", "14": "pa", "15": "pb", "16": "pr",
    "17": "pe", "18": "pi", "19": "rj", "20": "rn", "21": "rs", "22": "ro", "23": "rr", "24": "sc",
    "25": "se", "26": "sp", "27": "to",
}
TRANSITO_CODES = {848}
PARTIAL_RETRIES = 2
# Menções a requisição de pagamento no texto das publicações (texto já sem acentos e minúsculo).
REQUISICAO_RE = re.compile(r"oficio requisitorio|requisicao de pagamento|requisicao de pequeno valor|"
                           r"expedicao de (?:precatorio|rpv)|expeca-se (?:o )?(?:precatorio|rpv|oficio requisitorio)|"
                           r"requisite-se|precatorio expedido|rpv expedida")
COMMAND_RE = re.compile(r"^/(processo|consulta|ajuda|start)(?:@\w+)?\b\s*(.*)$", re.I | re.S)


class ConsultaIndisponivel(RuntimeError):
    """A API do CNJ não respondeu de forma confiável (não confundir com 'não encontrado')."""


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def format_cnj(digits: str) -> str:
    d = only_digits(digits)
    if len(d) != 20:
        return str(digits)
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def cnj_check_digits_ok(digits: str) -> bool:
    """Dígito verificador do número único (Res. CNJ 65/2008, módulo 97)."""
    d = only_digits(digits)
    if len(d) != 20:
        return False
    number, dv, rest = d[:7], d[7:9], d[9:]
    return 98 - (int(number + rest + "00") % 97) == int(dv)


def alias_from_cnj(digits: str) -> str | None:
    """Alias do índice DataJud a partir dos dígitos J.TR do número CNJ."""
    d = only_digits(digits)
    if len(d) != 20:
        return None
    j, tr = d[13], d[14:16]
    n = int(tr)
    uf = TR_UF.get(tr)
    if j == "8":
        return f"api_publica_tj{uf}" if uf else None
    if j == "4":
        return f"api_publica_trf{n}" if 1 <= n <= 6 else None
    if j == "5":
        if tr == "00":
            return "api_publica_tst"
        return f"api_publica_trt{n}" if 1 <= n <= 24 else None
    if j == "3":
        return "api_publica_stj" if tr == "00" else None
    return None


def parse_datahora(raw) -> datetime | None:
    if raw in (None, ""):
        return None
    s = str(raw)
    try:
        if re.fullmatch(r"\d{14}", s):
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=BRT)
        if re.fullmatch(r"\d{13}", s):
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").casefold()) if not unicodedata.combining(c))


def _movement(m: dict) -> tuple[int | None, str, datetime | None]:
    nacional = m.get("movimentoNacional") or {}
    code = m.get("codigo", nacional.get("codigo"))
    name = m.get("nome") or nacional.get("nome") or "Movimentação registrada"
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, str(name), parse_datahora(m.get("dataHora"))


async def datajud_lookup(digits: str, client: DataJudClient | None = None) -> dict | None:
    """Processo no tribunal de origem. None = não encontrado (com todos os shards ok)."""
    alias = alias_from_cnj(digits)
    if not alias:
        raise ValueError("número CNJ sem índice no DataJud")
    client = client or DataJudClient(max_retries=2)
    query = {"size": 3, "query": {"match": {"numeroProcesso": only_digits(digits)}}}
    sources: list = []
    for attempt in range(PARTIAL_RETRIES + 1):
        async with httpx.AsyncClient(timeout=35) as http:
            data = await client.search(alias, query, http)
        shards_failed = int(((data or {}).get("_shards") or {}).get("failed") or 0)
        hits = ((data or {}).get("hits") or {}).get("hits") or []
        sources = [h.get("_source") for h in hits if isinstance(h, dict) and h.get("_source")]
        if sources or not shards_failed:
            break
        # 200 parcial (shards rejeitados) sem hit: o shard pode voltar; tenta de novo.
        if attempt < PARTIAL_RETRIES:
            await asyncio.sleep(3 * (attempt + 1))
    if not sources:
        if shards_failed:
            raise ConsultaIndisponivel("resposta parcial do DataJud")
        return None
    # Pode haver mais de um grau (G1, G2...): junta os movimentos, usa metadados do mais recente.
    sources.sort(key=lambda s: str(s.get("dataHoraUltimaAtualizacao") or ""), reverse=True)
    main = dict(sources[0])
    movements = []
    for src in sources:
        movements.extend(src.get("movimentos") or [])
    main["movimentos"] = movements
    main["_alias"] = alias
    main["_graus"] = sorted({str(s.get("grau") or "") for s in sources if s.get("grau")})
    return main


async def djen_lookup(digits: str) -> dict:
    """Publicações do processo no DJEN (exige IP brasileiro; falha vira aviso, não erro)."""
    params = {"numeroProcesso": only_digits(digits), "itensPorPagina": 50, "pagina": 1}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "ASPIN-Garimpo/1.0"}) as client:
        response = await client.get("https://comunicaapi.pje.jus.br/api/v1/comunicacao", params=params)
    if response.status_code == 403:
        return {"error": "DJEN indisponível fora do Brasil"}
    if response.status_code >= 400:
        return {"error": f"DJEN HTTP {response.status_code}"}
    data = response.json()
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    items.sort(key=lambda i: str(i.get("data_disponibilizacao") or i.get("dataDisponibilizacao") or ""), reverse=True)
    mention = next((i for i in items if REQUISICAO_RE.search(_norm(i.get("texto") or ""))), None)
    return {"count": int(data.get("count") or len(items)), "items": items,
            "requisicao_em": (mention.get("data_disponibilizacao") or mention.get("dataDisponibilizacao")) if mention else None}


def summarize(digits: str, process: dict | None, djen: dict | None, internal: dict | None, max_amount=None) -> str:
    """Resumo HTML curto para o Telegram."""
    e = html.escape
    lines = [f"<b>📄 {e(format_cnj(digits))}</b>"]
    if process is None:
        lines.append("Não encontrado no DataJud. Pode ser processo recente (a indexação do CNJ atrasa semanas) "
                     "ou em segredo de justiça.")
    else:
        tribunal = str(process.get("tribunal") or process.get("_alias", "")).removeprefix("api_publica_").upper()
        classe = (process.get("classe") or {}).get("nome") or "classe n/i"
        orgao = (process.get("orgaoJulgador") or {}).get("nome") or "órgão n/i"
        assuntos = ", ".join(str(a.get("nome")) for a in (process.get("assuntos") or [])[:2] if isinstance(a, dict) and a.get("nome"))
        ajuiz = parse_datahora(process.get("dataAjuizamento"))
        lines.append(f"{e(tribunal)} · {e(classe)}")
        lines.append(f"🏛 {e(orgao)}")
        if assuntos:
            lines.append(f"📚 {e(assuntos)}")
        if ajuiz:
            lines.append(f"Ajuizado em {ajuiz.astimezone(BRT):%d/%m/%Y}")
        moves = sorted((_movement(m) for m in process.get("movimentos") or [] if isinstance(m, dict)),
                       key=lambda x: x[2] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        expedicao = next((m for m in moves if m[0] == MOVEMENT_EXPEDICAO_PRECATORIO_RPV), None)
        transito = next((m for m in moves if m[0] in TRANSITO_CODES or "transito em julgado" in _norm(m[1])), None)
        lines.append("")
        if expedicao:
            lines.append("✅ DataJud: expedição de precatório/RPV" +
                         (f" em {expedicao[2].astimezone(BRT):%d/%m/%Y}" if expedicao[2] else ""))
        else:
            lines.append("▫️ DataJud: sem o movimento específico de precatório/RPV "
                         "(alguns tribunais registram só como “expedição de documento”)")
        if transito:
            lines.append("⚖️ Trânsito em julgado" + (f" em {transito[2].astimezone(BRT):%d/%m/%Y}" if transito[2] else ""))
        if moves:
            lines.append("")
            lines.append("<b>Últimos andamentos</b>")
            for _, name, when in moves[:4]:
                lines.append(f"• {when.astimezone(BRT):%d/%m/%Y} — {e(name[:70])}" if when else f"• {e(name[:70])}")
    if djen:
        if djen.get("error"):
            lines.append(f"\n<i>{e(djen['error'])}</i>")
        else:
            lines.append(f"\n📰 Publicações no DJEN: {djen.get('count', 0)}")
            if djen.get("requisicao_em"):
                when = parse_datahora(djen["requisicao_em"])
                lines.append("✅ DJEN: publicação menciona requisição/precatório/RPV" +
                             (f" ({when:%d/%m/%Y})" if when else ""))
            if max_amount:
                lines.append(f"💰 Maior valor citado nas publicações: R$ {max_amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    if internal:
        if internal.get("nota") is not None:
            status = {"aprovada": "aprovada", "descartada": "descartada"}.get(internal.get("decision"), "aguardando decisão")
            lines.append(f"\n⭐ No Garimpo: Nota ASPIN {internal['nota']}/10 · {status}")
        elif internal.get("documents"):
            lines.append(f"\n🔎 No Garimpo: {internal['documents']} publicação(ões) já coletada(s)")
    lines.append("\n<i>Consulta de apoio; confira no sistema do tribunal antes de decidir.</i>")
    return "\n".join(lines)[:4000]


HELP_TEXT = ("<b>Comandos do Garimpo ASPIN</b>\n"
             "/processo 0000000-00.0000.0.00.0000 — resumo do processo (DataJud + DJEN + histórico do bot)\n"
             "/ajuda — esta mensagem")
