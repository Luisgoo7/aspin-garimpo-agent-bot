/**
 * @OnlyCurrentDoc  Limita a permissão a ESTA planilha (não acessa outras planilhas do Drive).
 */
/**
 * ASPIN Garimpo — recebe as oportunidades APROVADAS no Telegram e grava na planilha.
 *
 * Instalação (uma vez): na planilha, Extensões > Apps Script > apague o conteúdo,
 * cole este arquivo inteiro, salve, e publique em Implantar > Nova implantação >
 * Tipo "App da Web" > Executar como "Eu" > Quem pode acessar "Qualquer pessoa".
 * Copie a URL que termina em /exec.
 */
const TOKEN = 'COLE_AQUI_O_TOKEN';

// Colunas que devem ficar como texto (número de processo começa com zero).
const TEXT_COLUMNS = ['Processo', 'Nº requisição', 'Processo referência', 'ID interno'];

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_({ ok: false, error: 'json_invalido' });
  }
  if (!body || body.token !== TOKEN) {
    return reply_({ ok: false, error: 'token_invalido' });
  }
  const row = body.row || {};
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    const idCol = headers.indexOf('ID interno') + 1;
    // Não duplica: se o bot reenviar a mesma aprovação, a linha já existente é mantida.
    if (idCol > 0 && sheet.getLastRow() > 1) {
      const ids = sheet.getRange(2, idCol, sheet.getLastRow() - 1, 1).getValues().map(function (r) { return String(r[0]); });
      if (ids.indexOf(String(row['ID interno'])) >= 0) {
        return reply_({ ok: true, duplicate: true });
      }
    }
    const values = headers.map(function (h) {
      const v = row[h] === undefined || row[h] === null ? '' : row[h];
      return TEXT_COLUMNS.indexOf(h) >= 0 && v !== '' ? "'" + v : v;
    });
    sheet.appendRow(values);
    return reply_({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return reply_({ ok: true, service: 'aspin-planilha' });
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
