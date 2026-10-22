"""Diagnóstico sanitizado da conexão PostgreSQL.

Nunca devolve senha, usuário completo, DSN ou mensagem bruta do driver: só
categorias, formato do host e dicas de correção em português.
"""
import re
import socket
from urllib.parse import unquote, urlsplit

DIRECT_HOST_RE = re.compile(r"^db\.([a-z0-9]{20})\.supabase\.co$")
POOLER_HOST_RE = re.compile(r"^(aws-\d+-[a-z0-9-]+)\.pooler\.supabase\.com$")

# Região AWS sem o último hífen (ex.: ca-central1 em vez de ca-central-1).
BROKEN_REGION_RE = re.compile(r"^(aws-\d+-[a-z]+-[a-z]+)(\d+)(\.pooler\.supabase\.com)$")


def repair_pooler_dsn(dsn: str) -> str | None:
    """URI com a região do pooler corrigida, se a original não resolve e a corrigida resolve."""
    try:
        parts = urlsplit(dsn)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    match = BROKEN_REGION_RE.match(host)
    if not match or resolve(host, 5432)["dns"] == "ok":
        return None
    fixed = f"{match.group(1)}-{match.group(2)}{match.group(3)}"
    if resolve(fixed, 5432)["dns"] != "ok":
        return None
    netloc = parts.netloc
    at = netloc.rfind("@")
    hostport = netloc[at + 1:]
    new_hostport = re.sub(re.escape(host), fixed, hostport, count=1, flags=re.I)
    return parts._replace(netloc=netloc[:at + 1] + new_hostport).geturl()


# (trecho da mensagem do driver em minúsculas, categoria, dica)
_RULES = (
    ("network is unreachable", "ipv6_sem_rota",
     "O host só respondeu em IPv6 e o runner do GitHub não tem rota IPv6. Use a URI do Session pooler do Supabase (porta 5432, host *.pooler.supabase.com)."),
    ("cannot assign requested address", "ipv6_sem_rota",
     "O host só respondeu em IPv6 e o runner do GitHub não tem rota IPv6. Use a URI do Session pooler do Supabase."),
    ("could not translate host name", "dns_nao_resolve",
     "O nome do host não existe. Projeto Supabase pausado/excluído ou host digitado errado. Reative o projeto ou copie a URI de novo."),
    ("name or service not known", "dns_nao_resolve",
     "O nome do host não existe. Projeto Supabase pausado/excluído ou host digitado errado."),
    ("temporary failure in name resolution", "dns_nao_resolve",
     "Falha de DNS. Projeto Supabase pausado/excluído ou host errado."),
    ("tenant or user not found", "pooler_usuario_ou_projeto",
     "O pooler não reconheceu o usuário/projeto. No pooler o usuário precisa ser postgres.<ref-do-projeto>; também acontece com projeto pausado."),
    ("password authentication failed", "senha_recusada",
     "Senha do banco recusada. Redefina a senha em Supabase > Database Settings e atualize o secret."),
    ("timeout expired", "timeout",
     "O servidor não respondeu a tempo. Projeto pausado, firewall/restrição de rede ou host errado."),
    ("connection refused", "conexao_recusada",
     "Porta fechada. Confira a porta (5432 session pooler / 6543 transaction pooler)."),
    ("too many connections", "limite_conexoes",
     "Limite de conexões atingido. Use o pooler ou reduza conexões simultâneas."),
    ("remaining connection slots", "limite_conexoes",
     "Limite de conexões atingido. Use o pooler."),
    ("max client connections", "limite_conexoes",
     "Limite de conexões do pooler atingido."),
    ("ssl", "ssl",
     "Falha de SSL. Use a URI do Supabase sem alterar sslmode."),
    ("invalid connection option", "uri_invalida",
     "DATABASE_URL malformada. Use exatamente a URI do botão Connect do Supabase."),
    ("missing \"=\"", "uri_invalida",
     "DATABASE_URL malformada (parece texto em vez de URI postgresql://)."),
    ("invalid uri", "uri_invalida",
     "DATABASE_URL malformada. Se a senha tem caracteres especiais, aplique URL encoding."),
    ("invalid integer value", "uri_invalida",
     "Porta inválida na DATABASE_URL (ex.: senha com @ ou : sem URL encoding)."),
    ("server closed the connection", "conexao_encerrada",
     "O servidor encerrou a conexão. Projeto pausado/reiniciando ou pooler com URI errada."),
    ("does not exist", "banco_inexistente",
     "Banco ou usuário não existe. Use o banco 'postgres' da URI do Supabase."),
)


def describe_target(dsn: str) -> dict:
    """Formato da DATABASE_URL sem expor credenciais."""
    info = {"configurada": bool(dsn and dsn.strip())}
    if not info["configurada"]:
        return info
    dsn = dsn.strip()
    if "your-password" in dsn.casefold() or re.search(r"://[^@/]*\[", dsn):
        info["contem_placeholder"] = True
    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        info["formato"] = "uri_invalida"
        return info
    info["esquema_ok"] = parts.scheme in ("postgres", "postgresql")
    if parts.scheme in ("http", "https"):
        info["formato"] = "url_rest_em_vez_de_postgres"
        return info
    host = (parts.hostname or "").lower()
    user = unquote(parts.username or "")
    info["porta"] = port or 5432
    info["tem_senha"] = bool(parts.password)
    info["banco"] = (parts.path or "/").lstrip("/") or "(vazio)"
    if DIRECT_HOST_RE.match(host):
        info["tipo_host"] = "supabase_direto"
        info["host"] = "db.<ref>.supabase.co"
    elif POOLER_HOST_RE.match(host):
        info["tipo_host"] = "supabase_pooler"
        info["host"] = host
        info["pooler_modo"] = "transaction" if info["porta"] == 6543 else "session"
        info["usuario_com_ref"] = bool(re.match(r"^[^.]+\.[a-z0-9]{20}$", user))
    elif host in ("localhost", "127.0.0.1", "postgres", "db"):
        info["tipo_host"] = "local"
        info["host"] = host
    elif re.match(r"^[a-z0-9]{20}\.supabase\.co$", host):
        info["tipo_host"] = "supabase_api_em_vez_de_banco"
        info["host"] = "<ref>.supabase.co"
    elif host:
        info["tipo_host"] = "outro"
        labels = host.split(".")
        # Só o domínio de topo (ex.: supabase.com, neon.tech) — nunca o host inteiro.
        info["host"] = "(oculto)." + ".".join(labels[-2:]) if len(labels) >= 2 else "(oculto, sem domínio)"
    else:
        info["tipo_host"] = "sem_host"
    netloc = parts.netloc
    if netloc.count("@") > 1:
        info["senha_com_caractere_reservado"] = True
    if any(c.isspace() for c in dsn):
        info["contem_espaco"] = True
    if host:
        info.update(resolve(host, info["porta"]))
    return info


def resolve(host: str, port: int) -> dict:
    try:
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return {"dns": "nao_resolve", "ipv4": 0, "ipv6": 0}
    v4 = {a[4][0] for a in addrs if a[0] == socket.AF_INET}
    v6 = {a[4][0] for a in addrs if a[0] == socket.AF_INET6}
    return {"dns": "ok", "ipv4": len(v4), "ipv6": len(v6)}


def classify_error(exc: BaseException) -> tuple[str, str]:
    """(categoria, dica) a partir da mensagem do driver, sem devolvê-la."""
    message = str(exc).casefold()
    for needle, category, hint in _RULES:
        if needle in message:
            return category, hint
    return "desconhecido", "Erro de conexão não catalogado; teste a URI com o botão Connect do Supabase."


def diagnose(dsn: str, exc: BaseException | None = None) -> dict:
    from .config import DATABASE_URL_NORMALIZED
    target = describe_target(dsn)
    if DATABASE_URL_NORMALIZED[0]:
        target["espacos_removidos_da_uri"] = True
    result = {"alvo": target}
    if exc is not None:
        category, hint = classify_error(exc)
        result["erro"] = type(exc).__name__
        result["categoria"] = category
        result["dica"] = hint
        if category == "desconhecido":
            result["detalhe"] = sanitized_detail(exc)
    # Sinais fortes que não dependem da mensagem do driver.
    if target.get("formato") == "url_rest_em_vez_de_postgres":
        result["categoria"] = "url_rest_em_vez_de_postgres"
        result["dica"] = "DATABASE_URL está com a URL REST (https://...). Use a URI postgresql:// do Session pooler."
    elif target.get("contem_placeholder"):
        result["categoria"] = "placeholder_na_uri"
        result["dica"] = "A URI ainda tem o texto [YOUR-PASSWORD]/colchetes. Troque pela senha real do banco (sem colchetes)."
    elif target.get("senha_com_caractere_reservado"):
        result["categoria"] = "senha_sem_url_encoding"
        result["dica"] = "A senha contém @ (ou outro caractere reservado) sem URL encoding; o host fica errado. Codifique a senha (@ vira %40) ou gere uma senha sem símbolos."
    elif target.get("tipo_host") == "supabase_api_em_vez_de_banco":
        result["categoria"] = "host_da_api_em_vez_do_banco"
        result["dica"] = "O host é <ref>.supabase.co (API). Use a URI do Session pooler (host *.pooler.supabase.com)."
    elif target.get("dns") == "nao_resolve":
        result["categoria"] = "dns_nao_resolve"
        result["dica"] = ("O host do banco não existe no DNS: projeto Supabase pausado/excluído ou host errado. "
                          "Reative o projeto (Dashboard > Restore) ou copie a URI de novo.")
    elif target.get("tipo_host") == "supabase_direto" and target.get("ipv4") == 0 and target.get("ipv6", 0) > 0:
        result["categoria"] = "ipv6_sem_rota" if exc is not None else "risco_ipv6"
        result["dica"] = ("Host direto db.<ref>.supabase.co só tem IPv6; GitHub Actions é IPv4. "
                          "Troque DATABASE_URL pela URI do Session pooler (Connect > Session pooler).")
    elif target.get("tipo_host") == "supabase_pooler" and target.get("usuario_com_ref") is False:
        result["categoria"] = "pooler_usuario_sem_ref"
        result["dica"] = "No pooler o usuário precisa ser postgres.<ref-do-projeto>, não apenas postgres."
    return result


def format_diagnosis(diag: dict) -> str:
    target = diag.get("alvo", {})
    keys = ("configurada", "formato", "esquema_ok", "tipo_host", "host", "porta", "pooler_modo",
            "usuario_com_ref", "tem_senha", "banco", "dns", "ipv4", "ipv6",
            "senha_com_caractere_reservado", "contem_placeholder", "contem_espaco", "espacos_removidos_da_uri")
    alvo = ", ".join(f"{k}={target[k]}" for k in keys if k in target)
    text = f"Banco: {alvo}"
    if diag.get("categoria"):
        text += f" | causa={diag['categoria']}"
    if diag.get("erro"):
        text += f" ({diag['erro']})"
    if diag.get("detalhe"):
        text += f" | mensagem do servidor: {diag['detalhe']}"
    if diag.get("dica"):
        text += f" | como corrigir: {diag['dica']}"
    return text


def sanitized_detail(exc: BaseException) -> str:
    """Primeira frase do erro do servidor, sem host, IP, porta ou usuário."""
    raw = str(exc).strip()
    text = raw.splitlines()[0] if raw else ""
    text = re.sub(r'"[^"]*"', '"…"', text)                      # host, usuário, banco entre aspas
    text = re.sub(r"\(\s*[0-9a-fA-F:.]+\s*\)", "(…)", text)     # IPs entre parênteses
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "…", text)    # IPv4 soltos
    text = re.sub(r"[a-z0-9.-]+\.supabase\.(?:co|com)", "…", text)
    text = re.sub(r"port \d+", "port …", text)
    return text[:180]
