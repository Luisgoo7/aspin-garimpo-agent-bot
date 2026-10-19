/**
 * @OnlyCurrentDoc  Limita a permissão a ESTA planilha (não acessa outras planilhas do Drive).
 */
/**
 * ASPIN Garimpo — recebe as oportunidades APROVADAS no Telegram, grava e formata a planilha.
 *
 * Instalação/atualização: na planilha, Extensões > Apps Script > apague o conteúdo,
 * cole este arquivo inteiro e salve. Para publicar: Implantar > Gerenciar implantações >
 * lápis > Versão "Nova versão" > Implantar (a URL /exec continua a mesma).
 * Para formatar na hora: escolha a função "formatarPlanilha" no topo e clique em Executar
 * (ou use o menu ASPIN > Formatar planilha, que aparece ao abrir a planilha).
 */
const TOKEN = 'COLE_AQUI_O_TOKEN';

// Ordem das colunas (igual à do bot). Colunas antigas são reorganizadas automaticamente.
const HEADERS = [
  'Aprovado em', 'Aprovado por', 'Nota ASPIN (0-10)', 'Valor (R$)', 'Credor', 'Devedor', 'Tribunal', 'Tipo',
  'Processo', 'Natureza', 'Preferência legal', 'Terceiro interessado', 'Confirmado DataJud',
  'Critérios atendidos', 'Confiança IA', 'Comissão estimada (R$)', 'Honorários destacados (R$)',
  'Nº requisição', 'Classe', 'Órgão julgador', 'Processo referência', 'Assunto', 'Trânsito em julgado',
  'Fonte', 'Publicação (trecho)', 'ID interno'
];
// Colunas que devem ficar como texto (número de processo começa com zero).
const TEXT_COLUMNS = ['Processo', 'Nº requisição', 'Processo referência', 'ID interno', 'Aprovado em'];
const MONEY_COLUMNS = ['Valor (R$)', 'Comissão estimada (R$)', 'Honorários destacados (R$)'];
const WRAP_COLUMNS = ['Credor', 'Devedor', 'Critérios atendidos', 'Classe', 'Órgão julgador', 'Assunto',
  'Publicação (trecho)', 'Preferência legal', 'Terceiro interessado', 'Tipo'];
const CENTER_COLUMNS = ['Aprovado em', 'Nota ASPIN (0-10)', 'Tribunal', 'Natureza', 'Confirmado DataJud',
  'Confiança IA', 'Trânsito em julgado', 'Fonte'];
const WIDTHS = {
  'Aprovado em': 125, 'Aprovado por': 140, 'Nota ASPIN (0-10)': 95, 'Valor (R$)': 130, 'Credor': 240,
  'Devedor': 240, 'Tribunal': 75, 'Tipo': 140, 'Processo': 190, 'Natureza': 95, 'Preferência legal': 150,
  'Terceiro interessado': 160, 'Confirmado DataJud': 105, 'Critérios atendidos': 260, 'Confiança IA': 95,
  'Comissão estimada (R$)': 140, 'Honorários destacados (R$)': 150, 'Nº requisição': 170, 'Classe': 220,
  'Órgão julgador': 220, 'Processo referência': 190, 'Assunto': 180, 'Trânsito em julgado': 115,
  'Fonte': 130, 'Publicação (trecho)': 420, 'ID interno': 100
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('ASPIN').addItem('Formatar planilha', 'formatarPlanilha').addToUi();
}

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
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sheet = sheet_();
    ensureLayout_(sheet);
    if (body.action === 'format') {
      formatSheet_(sheet);
      return reply_({ ok: true, formatted: true });
    }
    const row = body.row || {};
    const idCol = HEADERS.indexOf('ID interno') + 1;
    // Não duplica: se o bot reenviar a mesma aprovação, a linha já existente é mantida.
    if (sheet.getLastRow() > 1) {
      const ids = sheet.getRange(2, idCol, sheet.getLastRow() - 1, 1).getValues().map(function (r) { return String(r[0]); });
      if (ids.indexOf(String(row['ID interno'])) >= 0) {
        return reply_({ ok: true, duplicate: true });
      }
    }
    writeRows_(sheet, [row], sheet.getLastRow() + 1);
    formatSheet_(sheet);
    return reply_({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return reply_({ ok: true, service: 'aspin-planilha', version: 2 });
}

/** Executável pelo editor ou pelo menu ASPIN. */
function formatarPlanilha() {
  const sheet = sheet_();
  ensureLayout_(sheet);
  formatSheet_(sheet);
}

function sheet_() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
}

/** Reorganiza a aba para a ordem de HEADERS, preservando as linhas existentes. */
function ensureLayout_(sheet) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const current = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
  if (current.join('|') === HEADERS.join('|')) {
    return;
  }
  const rows = [];
  const lastRow = sheet.getLastRow();
  if (lastRow > 1) {
    const values = sheet.getRange(2, 1, lastRow - 1, lastCol).getValues();
    const links = sheet.getRange(2, 1, lastRow - 1, lastCol).getRichTextValues();
    values.forEach(function (line, i) {
      const obj = {};
      current.forEach(function (h, j) {
        let v = line[j];
        if (h === 'Fonte' && links[i][j] && links[i][j].getLinkUrl()) {
          v = links[i][j].getLinkUrl();
        }
        if (h === 'Confiança') {  // coluna antiga em texto ("90%") vira número
          h = 'Confiança IA';
          v = typeof v === 'string' && v.indexOf('%') >= 0 ? parseFloat(v) / 100 : v;
        }
        obj[h] = v;
      });
      rows.push(obj);
    });
  }
  sheet.getBandings().forEach(function (b) { b.remove(); });
  if (sheet.getFilter()) {
    sheet.getFilter().remove();
  }
  sheet.clear();
  if (sheet.getMaxColumns() < HEADERS.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), HEADERS.length - sheet.getMaxColumns());
  }
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  if (rows.length) {
    writeRows_(sheet, rows, 2);
  }
}

function writeRows_(sheet, rows, startRow) {
  TEXT_COLUMNS.forEach(function (h) {
    sheet.getRange(startRow, HEADERS.indexOf(h) + 1, rows.length, 1).setNumberFormat('@');
  });
  const values = rows.map(function (row) {
    return HEADERS.map(function (h) {
      const v = row[h];
      return v === undefined || v === null ? '' : (TEXT_COLUMNS.indexOf(h) >= 0 ? String(v) : v);
    });
  });
  sheet.getRange(startRow, 1, rows.length, HEADERS.length).setValues(values);
  // Fonte vira link clicável "Abrir publicação".
  const fonteCol = HEADERS.indexOf('Fonte') + 1;
  rows.forEach(function (row, i) {
    const url = String(row['Fonte'] || '');
    if (url.indexOf('http') === 0) {
      sheet.getRange(startRow + i, fonteCol).setRichTextValue(
        SpreadsheetApp.newRichTextValue().setText('Abrir publicação').setLinkUrl(url).build());
    }
  });
}

function formatSheet_(sheet) {
  const ss = sheet.getParent();
  if (sheet.getName() !== 'Aprovadas' && !ss.getSheetByName('Aprovadas')) {
    sheet.setName('Aprovadas');
  }
  const n = HEADERS.length;
  const dataRows = Math.max(sheet.getLastRow() - 1, 1);
  const all = sheet.getRange(1, 1, dataRows + 1, n);

  all.setFontFamily('Arial').setFontSize(10).setVerticalAlignment('top');
  const header = sheet.getRange(1, 1, 1, n);
  header.setBackground('#0B3D5C').setFontColor('#FFFFFF').setFontWeight('bold')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(1, 46);
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(3);

  HEADERS.forEach(function (h, i) {
    const col = i + 1;
    sheet.setColumnWidth(col, WIDTHS[h] || 140);
    const range = sheet.getRange(2, col, dataRows, 1);
    range.setWrapStrategy(WRAP_COLUMNS.indexOf(h) >= 0 ? SpreadsheetApp.WrapStrategy.WRAP : SpreadsheetApp.WrapStrategy.CLIP);
    if (CENTER_COLUMNS.indexOf(h) >= 0) {
      range.setHorizontalAlignment('center');
    }
    if (MONEY_COLUMNS.indexOf(h) >= 0) {
      range.setNumberFormat('"R$" #,##0.00').setHorizontalAlignment('right');
    }
    if (h === 'Confiança IA') {
      range.setNumberFormat('0%');
    }
    if (h === 'Nota ASPIN (0-10)') {
      range.setNumberFormat('0" / 10"').setFontWeight('bold');
    }
  });
  sheet.getRange(2, HEADERS.indexOf('Valor (R$)') + 1, dataRows, 1).setFontWeight('bold');
  sheet.hideColumns(HEADERS.indexOf('ID interno') + 1);

  // Linhas zebradas.
  sheet.getBandings().forEach(function (b) { b.remove(); });
  sheet.getRange(2, 1, dataRows, n).applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);

  // Cores da nota e alerta de possível cessão.
  const notaRange = sheet.getRange(2, HEADERS.indexOf('Nota ASPIN (0-10)') + 1, dataRows, 1);
  const cessaoRange = sheet.getRange(2, HEADERS.indexOf('Terceiro interessado') + 1, dataRows, 1);
  sheet.setConditionalFormatRules([
    SpreadsheetApp.newConditionalFormatRule().whenNumberGreaterThanOrEqualTo(8)
      .setBackground('#C8E6C9').setFontColor('#1B5E20').setRanges([notaRange]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenNumberBetween(5, 7)
      .setBackground('#FFF3C4').setFontColor('#7A5B00').setRanges([notaRange]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenNumberLessThan(5)
      .setBackground('#FAD4D4').setFontColor('#8E1B1B').setRanges([notaRange]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenTextStartsWith('Sim')
      .setFontColor('#B00020').setBold(true).setRanges([cessaoRange]).build()
  ]);

  // Filtro no cabeçalho para ordenar por nota, valor, tribunal... (recriado para incluir as linhas novas)
  if (sheet.getFilter()) {
    sheet.getFilter().remove();
  }
  if (sheet.getLastRow() > 1) {
    sheet.getRange(1, 1, sheet.getLastRow(), n).createFilter();
  }
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
