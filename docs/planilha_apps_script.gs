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
 *
 * Web app do CRM (tela visual): crie também o arquivo HTML "crm" (Arquivo > + > HTML) com o
 * conteúdo de docs/crm.html e publique uma implantação separada como app da Web, executando como
 * "Usuário que acessa o app da Web". Abra a URL /exec com ?app=crm no final.
 *
 * CRM: as colunas Etapa, Responsável, Próxima ação... são preenchidas pela equipe (menus
 * suspensos). As opções ficam na aba "Listas" (pode editar) e o resumo na aba "Funil".
 */
const TOKEN = 'COLE_AQUI_O_TOKEN';

// Ordem das colunas (igual à do bot). Colunas antigas são reorganizadas automaticamente.
const HEADERS = [
  'Aprovado em', 'Aprovado por', 'Nota ASPIN (0-10)',
  'Etapa', 'Responsável', 'Próxima ação', 'Data da próxima ação',
  'Valor (R$)', 'Credor', 'Devedor', 'Tribunal', 'Tipo',
  'Processo', 'Data de expedição (ofício)', 'Data base', 'Natureza', 'Preferência legal', 'Terceiro interessado', 'Confirmado DataJud',
  'Critérios atendidos', 'Confiança IA', 'Comissão estimada (R$)', 'Honorários destacados (R$)',
  'Nº requisição', 'Classe', 'Órgão julgador', 'Processo referência', 'Assunto', 'Trânsito em julgado',
  'Valor proposto (R$)', 'Motivo da perda', 'Observações', 'Etapa atualizada em',
  'Fonte', 'Publicação (trecho)', 'ID interno'
];
// Colunas do CRM: a equipe preenche na planilha; o bot manda vazias e nunca sobrescreve.
const CRM_DATE_COLUMNS = ['Data da próxima ação'];
const DEFAULT_STAGE = 'Nova';
// Valores iniciais da aba "Listas" (criada só se não existir; depois a equipe edita lá).
const STAGES = ['Nova', 'Em análise', 'Contato feito', 'Em negociação', 'Proposta enviada',
  'Documentação', 'Ganho', 'Perdido'];
const STAGE_COLORS = {
  'Nova': ['#E3F2FD', '#0D47A1'], 'Em análise': ['#EDE7F6', '#4527A0'], 'Contato feito': ['#E0F7FA', '#006064'],
  'Em negociação': ['#FFF3C4', '#7A5B00'], 'Proposta enviada': ['#FFE0B2', '#8A4B00'],
  'Documentação': ['#F3E5F5', '#6A1B9A'], 'Ganho': ['#C8E6C9', '#1B5E20'], 'Perdido': ['#FAD4D4', '#8E1B1B']
};
const LOSS_REASONS = ['Credor não localizado', 'Sem interesse em vender', 'Deságio não aceito',
  'Vendeu para outro comprador', 'Crédito já cedido', 'Pendência jurídica/documental', 'Sem retorno', 'Outro'];
const OWNERS = ['Luís B Oliveira', 'Gabriel Tavares'];
// Colunas que devem ficar como texto (número de processo começa com zero).
const TEXT_COLUMNS = ['Processo', 'Nº requisição', 'Processo referência', 'ID interno', 'Aprovado em',
  'Data de expedição (ofício)', 'Data base'];
const MONEY_COLUMNS = ['Valor (R$)', 'Comissão estimada (R$)', 'Honorários destacados (R$)', 'Valor proposto (R$)'];
const WRAP_COLUMNS = ['Credor', 'Devedor', 'Critérios atendidos', 'Classe', 'Órgão julgador', 'Assunto',
  'Próxima ação', 'Observações', 'Motivo da perda',
  'Publicação (trecho)', 'Preferência legal', 'Terceiro interessado', 'Tipo'];
const CENTER_COLUMNS = ['Aprovado em', 'Nota ASPIN (0-10)', 'Tribunal', 'Natureza', 'Confirmado DataJud',
  'Etapa', 'Data da próxima ação', 'Etapa atualizada em',
  'Data de expedição (ofício)', 'Data base',
  'Confiança IA', 'Trânsito em julgado', 'Fonte'];
const WIDTHS = {
  'Aprovado em': 125, 'Aprovado por': 140, 'Nota ASPIN (0-10)': 95, 'Valor (R$)': 130, 'Credor': 240,
  'Devedor': 240, 'Tribunal': 75, 'Tipo': 140, 'Processo': 190, 'Data de expedição (ofício)': 120, 'Data base': 105, 'Natureza': 95, 'Preferência legal': 150,
  'Terceiro interessado': 160, 'Confirmado DataJud': 105, 'Critérios atendidos': 260, 'Confiança IA': 95,
  'Comissão estimada (R$)': 140, 'Honorários destacados (R$)': 150, 'Nº requisição': 170, 'Classe': 220,
  'Órgão julgador': 220, 'Processo referência': 190, 'Assunto': 180, 'Trânsito em julgado': 115,
  'Fonte': 130, 'Publicação (trecho)': 420, 'ID interno': 100,
  'Etapa': 150, 'Responsável': 150, 'Próxima ação': 220, 'Data da próxima ação': 115,
  'Valor proposto (R$)': 140, 'Motivo da perda': 190, 'Observações': 280, 'Etapa atualizada em': 130
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('ASPIN').addItem('Formatar planilha', 'formatarPlanilha').addToUi();
}

/** Ao mudar a Etapa, registra quando mudou (gatilho simples, não precisa de autorização). */
function onEdit(e) {
  const range = e && e.range;
  if (!range || range.getSheet().getName() !== 'Aprovadas' || range.getRow() < 2) {
    return;
  }
  const stageCol = HEADERS.indexOf('Etapa') + 1;
  if (range.getColumn() > stageCol || range.getLastColumn() < stageCol) {
    return;
  }
  const sheet = range.getSheet();
  const stampCol = HEADERS.indexOf('Etapa atualizada em') + 1;
  // Texto no horário de Brasília (o fuso da planilha pode ser outro).
  const now = Utilities.formatDate(new Date(), 'America/Sao_Paulo', 'dd/MM/yyyy HH:mm');
  for (let r = range.getRow(); r <= range.getLastRow(); r++) {
    sheet.getRange(r, stampCol).setNumberFormat('@').setValue(now);
  }
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

function doGet(e) {
  // ?app=crm abre a tela do CRM; sem parâmetro, responde ao bot (checagem de versão).
  if (e && e.parameter && e.parameter.app === 'crm') {
    return crmPage_();
  }
  return reply_({ ok: true, service: 'aspin-planilha', version: 6 });
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
        if (h === 'Processo' && links[i][j] && links[i][j].getLinkUrl()) {
          obj['Link do processo'] = links[i][j].getLinkUrl();
        }
        if (h === 'Confiança') {  // coluna antiga em texto ("90%") vira número
          h = 'Confiança IA';
          v = typeof v === 'string' && v.indexOf('%') >= 0 ? parseFloat(v) / 100 : v;
        }
        if (v instanceof Date && CRM_DATE_COLUMNS.indexOf(h) < 0) {  // datas antigas gravadas como objeto viram texto dd/mm/aaaa hh:mm
          v = Utilities.formatDate(v, Session.getScriptTimeZone(), 'dd/MM/yyyy HH:mm');
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
      const v = h === 'Etapa' && !row[h] ? DEFAULT_STAGE : row[h];
      return v === undefined || v === null ? '' : (TEXT_COLUMNS.indexOf(h) >= 0 ? String(v) : v);
    });
  });
  sheet.getRange(startRow, 1, rows.length, HEADERS.length).setValues(values);
  // Fonte vira link clicável "Abrir publicação"; o nº do Processo abre a consulta pública do CNJ.
  const fonteCol = HEADERS.indexOf('Fonte') + 1;
  const processoCol = HEADERS.indexOf('Processo') + 1;
  rows.forEach(function (row, i) {
    const url = String(row['Fonte'] || '');
    if (url.indexOf('http') === 0) {
      sheet.getRange(startRow + i, fonteCol).setRichTextValue(
        SpreadsheetApp.newRichTextValue().setText('Abrir publicação').setLinkUrl(url).build());
    }
    const processoUrl = String(row['Link do processo'] || '');
    const processo = String(row['Processo'] || '');
    if (processoUrl.indexOf('http') === 0 && processo) {
      sheet.getRange(startRow + i, processoCol).setRichTextValue(
        SpreadsheetApp.newRichTextValue().setText(processo).setLinkUrl(processoUrl).build());
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
  sheet.setFrozenColumns(4);  // até a Etapa

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
  ].concat(crmRules_(sheet, dataRows)));
  applyCrm_(sheet, dataRows);

  // Filtro no cabeçalho para ordenar por nota, valor, tribunal... (recriado para incluir as linhas novas)
  if (sheet.getFilter()) {
    sheet.getFilter().remove();
  }
  if (sheet.getLastRow() > 1) {
    sheet.getRange(1, 1, sheet.getLastRow(), n).createFilter();
  }
}

/** Aba "Listas" com as opções dos menus suspensos. Criada uma vez; depois a equipe edita. */
function listsSheet_(ss) {
  let lists = ss.getSheetByName('Listas');
  if (lists) {
    return lists;
  }
  lists = ss.insertSheet('Listas', ss.getNumSheets());
  const n = Math.max(STAGES.length, LOSS_REASONS.length, OWNERS.length);
  const rows = [['Etapas (ordem do funil)', 'Motivos da perda', 'Responsáveis']];
  for (let i = 0; i < n; i++) {
    rows.push([STAGES[i] || '', LOSS_REASONS[i] || '', OWNERS[i] || '']);
  }
  lists.getRange(1, 1, rows.length, 3).setValues(rows);
  lists.getRange(1, 1, 1, 3).setBackground('#0B3D5C').setFontColor('#FFFFFF').setFontWeight('bold');
  lists.setColumnWidths(1, 3, 220);
  lists.setFrozenRows(1);
  lists.getRange(1, 5).setValue('Pode incluir, renomear ou reordenar as opções: os menus da aba Aprovadas usam estas colunas.')
    .setFontColor('#666666').setFontStyle('italic');
  return lists;
}

/** Separador de argumentos das fórmulas: em português (e outros idiomas com vírgula decimal) é ";". */
function sep_(ss) {
  return /^(en|ja|zh|ko|th|iw|he|hi|ms|fil|tl)/.test(String(ss.getSpreadsheetLocale())) ? ',' : ';';
}

function colLetter_(h) {
  let n = HEADERS.indexOf(h) + 1;
  let s = '';
  while (n > 0) {
    const m = (n - 1) % 26;
    s = String.fromCharCode(65 + m) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/** Menus suspensos, formatos de data e a aba Funil. */
function applyCrm_(sheet, dataRows) {
  const ss = sheet.getParent();
  const lists = listsSheet_(ss);
  const rows = Math.max(dataRows, 1) + 200;  // linhas novas também ganham os menus
  const listRule = function (col) {
    return SpreadsheetApp.newDataValidation().requireValueInRange(lists.getRange(col + '2:' + col + '100'), true)
      .setAllowInvalid(false).build();
  };
  if (sheet.getMaxRows() < rows + 1) {
    sheet.insertRowsAfter(sheet.getMaxRows(), rows + 1 - sheet.getMaxRows());
  }
  sheet.getRange(2, HEADERS.indexOf('Etapa') + 1, rows, 1).setDataValidation(listRule('A'));
  sheet.getRange(2, HEADERS.indexOf('Motivo da perda') + 1, rows, 1).setDataValidation(listRule('B'));
  sheet.getRange(2, HEADERS.indexOf('Responsável') + 1, rows, 1).setDataValidation(listRule('C'));
  sheet.getRange(2, HEADERS.indexOf('Data da próxima ação') + 1, rows, 1)
    .setDataValidation(SpreadsheetApp.newDataValidation().requireDate().setAllowInvalid(false).build())
    .setNumberFormat('dd/mm/yyyy');
  sheet.getRange(2, HEADERS.indexOf('Etapa') + 1, dataRows, 1).setFontWeight('bold');
  funnelSheet_(ss);
}

function crmRules_(sheet, dataRows) {
  const stageRange = sheet.getRange(2, HEADERS.indexOf('Etapa') + 1, dataRows, 1);
  const rules = Object.keys(STAGE_COLORS).map(function (stage) {
    return SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(stage)
      .setBackground(STAGE_COLORS[stage][0]).setFontColor(STAGE_COLORS[stage][1]).setRanges([stageRange]).build();
  });
  // Próxima ação vencida (data passada e negociação ainda aberta) fica em vermelho.
  const dateCol = colLetter_('Data da próxima ação');
  const stageCol = colLetter_('Etapa');
  const s = sep_(sheet.getParent());
  rules.push(SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=AND($' + dateCol + '2<>""' + s + '$' + dateCol + '2<TODAY()' + s + '$' + stageCol + '2<>"Ganho"' + s + '$' + stageCol + '2<>"Perdido")')
    .setBackground('#FAD4D4').setFontColor('#8E1B1B').setBold(true)
    .setRanges([sheet.getRange(2, HEADERS.indexOf('Data da próxima ação') + 1, dataRows, 1)]).build());
  return rules;
}

/** Aba "Funil": quantidade e valores por etapa e motivos de perda (fórmulas, atualiza sozinha). */
function funnelSheet_(ss) {
  const funnel = ss.getSheetByName('Funil') || ss.insertSheet('Funil', ss.getNumSheets());
  funnel.clear();
  const col = function (h) { return 'Aprovadas!$' + colLetter_(h) + ':$' + colLetter_(h); };
  const stage = col('Etapa');
  const s = sep_(ss);
  const rows = [['Etapa', 'Oportunidades', 'Valor de face (R$)', 'Comissão estimada (R$)']];
  for (let i = 2; i <= 30; i++) {
    const a = 'Listas!$A$' + i;
    rows.push(['=IF(' + a + '=""' + s + '""' + s + a + ')',
      '=IF(' + a + '=""' + s + '""' + s + 'COUNTIF(' + stage + s + a + '))',
      '=IF(' + a + '=""' + s + '""' + s + 'SUMIF(' + stage + s + a + s + col('Valor (R$)') + '))',
      '=IF(' + a + '=""' + s + '""' + s + 'SUMIF(' + stage + s + a + s + col('Comissão estimada (R$)') + '))']);
  }
  funnel.getRange(1, 1, rows.length, 4).setFormulas(rows.map(function (r, i) {
    return i === 0 ? ['', '', '', ''] : r;
  }));
  funnel.getRange(1, 1, 1, 4).setValues([rows[0]]);
  const lossRows = [['Motivo da perda', 'Oportunidades']];
  for (let i = 2; i <= 30; i++) {
    const b = 'Listas!$B$' + i;
    lossRows.push(['=IF(' + b + '=""' + s + '""' + s + b + ')',
      '=IF(' + b + '=""' + s + '""' + s + 'COUNTIF(' + col('Motivo da perda') + s + b + '))']);
  }
  funnel.getRange(1, 6, lossRows.length, 2).setFormulas(lossRows.map(function (r, i) {
    return i === 0 ? ['', ''] : r;
  }));
  funnel.getRange(1, 6, 1, 2).setValues([lossRows[0]]);
  [funnel.getRange(1, 1, 1, 4), funnel.getRange(1, 6, 1, 2)].forEach(function (h) {
    h.setBackground('#0B3D5C').setFontColor('#FFFFFF').setFontWeight('bold');
  });
  funnel.getRange(2, 3, rows.length - 1, 2).setNumberFormat('"R$" #,##0.00');
  funnel.setColumnWidth(1, 180);
  funnel.setColumnWidths(2, 3, 170);
  funnel.setColumnWidth(6, 230);
  funnel.setFrozenRows(1);
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ===================== Web app do CRM (?app=crm) =====================
// Publicado numa implantação própria, "Executar como: usuário que acessa", para só abrir
// para quem já tem acesso à planilha. Lê e grava só na aba Aprovadas (as mesmas colunas do CRM).

const CRM_EDITABLE = ['Etapa', 'Responsável', 'Próxima ação', 'Data da próxima ação',
  'Valor proposto (R$)', 'Motivo da perda', 'Observações'];

/** Tela do CRM. */
function crmPage_() {
  return HtmlService.createHtmlOutputFromFile('crm')
    .setTitle('ÂSPIN · CRM de Oportunidades')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

/** Todas as oportunidades + listas, para a tela (chamado pelo navegador). */
function crmData() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = sheet_();
  const lastRow = sheet.getLastRow();
  const lastCol = sheet.getLastColumn();
  const header = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
  const rows = [];
  if (lastRow > 1) {
    const range = sheet.getRange(2, 1, lastRow - 1, lastCol);
    const values = range.getValues();
    const shown = range.getDisplayValues();
    const rich = range.getRichTextValues();
    const idCol = header.indexOf('ID interno');
    values.forEach(function (line, i) {
      if (idCol >= 0 && !String(line[idCol] || '').trim()) {
        return;
      }
      rows.push(crmRow_(header, line, shown[i], rich[i], ss));
    });
  }
  const lists = ss.getSheetByName('Listas');
  const col = function (c) {
    if (!lists) {
      return [];
    }
    return lists.getRange(c + '2:' + c + '100').getValues().map(function (r) { return String(r[0]).trim(); })
      .filter(String);
  };
  const stages = col('A');
  return {
    rows: rows,
    stages: stages.length ? stages : STAGES,
    reasons: col('B').length ? col('B') : LOSS_REASONS,
    owners: col('C').length ? col('C') : OWNERS,
    colors: STAGE_COLORS,
    sheetUrl: ss.getUrl(),
    loadedAt: Utilities.formatDate(new Date(), 'America/Sao_Paulo', 'dd/MM/yyyy HH:mm')
  };
}

/** Grava as mudanças de UMA oportunidade (só colunas do CRM). Devolve a linha atualizada. */
function crmSave(id, changes) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = sheet_();
    const lastCol = sheet.getLastColumn();
    const header = sheet.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
    const idCol = header.indexOf('ID interno') + 1;
    if (!idCol || sheet.getLastRow() < 2) {
      throw new Error('Planilha sem a coluna ID interno.');
    }
    const ids = sheet.getRange(2, idCol, sheet.getLastRow() - 1, 1).getValues().map(function (r) { return String(r[0]); });
    const index = ids.indexOf(String(id));
    if (index < 0) {
      throw new Error('Oportunidade não encontrada (a planilha mudou?). Clique em Atualizar.');
    }
    const row = index + 2;
    const stageCol = header.indexOf('Etapa') + 1;
    const oldStage = stageCol ? String(sheet.getRange(row, stageCol).getValue()) : '';
    Object.keys(changes || {}).forEach(function (h) {
      const c = header.indexOf(h) + 1;
      if (CRM_EDITABLE.indexOf(h) < 0 || !c) {
        return;
      }
      let v = changes[h];
      if (h === 'Data da próxima ação') {
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || ''));
        v = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : '';
      } else if (h === 'Valor proposto (R$)') {
        v = v === '' || v === null || isNaN(Number(v)) ? '' : Number(v);
      } else {
        v = String(v === null || v === undefined ? '' : v).slice(0, 2000);
      }
      sheet.getRange(row, c).setValue(v);
    });
    const newStage = stageCol ? String(sheet.getRange(row, stageCol).getValue()) : '';
    const stampCol = header.indexOf('Etapa atualizada em') + 1;
    if (stampCol && newStage !== oldStage) {
      sheet.getRange(row, stampCol).setNumberFormat('@')
        .setValue(Utilities.formatDate(new Date(), 'America/Sao_Paulo', 'dd/MM/yyyy HH:mm'));
    }
    SpreadsheetApp.flush();
    const range = sheet.getRange(row, 1, 1, lastCol);
    return crmRow_(header, range.getValues()[0], range.getDisplayValues()[0], range.getRichTextValues()[0], ss);
  } finally {
    lock.releaseLock();
  }
}

/** Uma linha da planilha como objeto simples (datas viram texto: o navegador não recebe Date). */
function crmRow_(header, line, shown, rich, ss) {
  const tz = ss.getSpreadsheetTimeZone();
  const o = {};
  header.forEach(function (h, j) {
    let v = line[j];
    if (h === 'Data da próxima ação') {
      v = v instanceof Date ? Utilities.formatDate(v, tz, 'yyyy-MM-dd') : '';
    } else if (v instanceof Date) {
      v = shown[j];
    } else if (typeof v !== 'number') {
      v = String(v === null || v === undefined ? '' : v);
    }
    o[h] = v;
    if ((h === 'Processo' || h === 'Fonte') && rich[j] && rich[j].getLinkUrl()) {
      o[h === 'Processo' ? 'Link do processo' : 'Link da fonte'] = rich[j].getLinkUrl();
    }
  });
  return o;
}
