"""Importa o secret-pacote (.env ou JSON) para o ambiente do job, mascarando valores."""
import io, json, os, uuid
from dotenv import dotenv_values
raw = os.environ.get('ASPIN_CONFIG_BUNDLE', '').strip()
allowed = {'DATABASE_URL','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','GROQ_API_KEY','GEMINI_API_KEY','NVIDIA_API_KEY','DATAJUD_API_KEY','SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY','SHEETS_WEBHOOK_URL','SHEETS_TOKEN','TELEGRAM_APPROVERS'}
if raw.startswith('```'):
    raw = '\n'.join(raw.splitlines()[1:-1])
try:
    data = json.loads(raw) if raw.startswith('{') else dict(dotenv_values(stream=io.StringIO(raw), interpolate=False))
except (ValueError, TypeError):
    raise SystemExit('O secret de configuração não tem formato .env ou JSON válido')
if not isinstance(data, dict):
    raise SystemExit('O secret de configuração precisa ser objeto JSON ou arquivo .env')
imported = []
with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as out:
    for key in sorted(allowed):
        if os.environ.get(key, '').strip() or data.get(key) in (None, ''):
            continue
        value = str(data[key])
        if '\x00' in value:
            raise SystemExit('Configuração contém caractere inválido')
        escaped = value.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
        print('::add-mask::' + escaped)
        delimiter = 'ASPIN_' + uuid.uuid4().hex
        out.write(f'{key}<<{delimiter}\n{value}\n{delimiter}\n')
        imported.append(key)
print('Configurações importadas (somente nomes): ' + ', '.join(imported))
