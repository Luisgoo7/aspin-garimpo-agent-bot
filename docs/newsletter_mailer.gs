/**
 * Radar ASPIN — envio do e-mail diário pela conta remetente (aspininforma@gmail.com).
 *
 * Instalação (uma vez, logado na conta remetente):
 * 1. script.google.com > Novo projeto > cole este arquivo > Salvar.
 * 2. Configurações do projeto > Propriedades do script > adicione TOKEN (mesmo valor do
 *    secret NEWSLETTER_MAILER_TOKEN no GitHub) e, se quiser, DESTINOS_PERMITIDOS
 *    (e-mails separados por vírgula; vazio = qualquer destinatário).
 * 3. Implantar > Nova implantação > App da Web > Executar como: eu > Quem pode acessar:
 *    Qualquer pessoa > Implantar > autorize o Gmail. Copie a URL /exec para o secret
 *    NEWSLETTER_MAILER_URL.
 *
 * Nenhuma senha sai da conta: o robô só envia o HTML pronto com o TOKEN.
 */
var VERSAO = 1;

function doGet() {
  return json_({ ok: true, version: VERSAO, cota_restante: MailApp.getRemainingDailyQuota() });
}

function doPost(e) {
  try {
    var dados = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var token = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!token || dados.token !== token) return json_({ ok: false, error: 'token inválido' });
    if (dados.action !== 'send') return json_({ ok: false, error: 'ação desconhecida' });

    var para = [].concat(dados.to || []).map(function (s) { return String(s).trim().toLowerCase(); })
      .filter(function (s) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s); });
    if (!para.length) return json_({ ok: false, error: 'sem destinatário' });
    var permitidos = (PropertiesService.getScriptProperties().getProperty('DESTINOS_PERMITIDOS') || '')
      .split(',').map(function (s) { return s.trim().toLowerCase(); }).filter(String);
    if (permitidos.length) {
      var fora = para.filter(function (s) { return permitidos.indexOf(s) < 0; });
      if (fora.length) return json_({ ok: false, error: 'destinatário fora da lista permitida' });
    }
    if (MailApp.getRemainingDailyQuota() < para.length) return json_({ ok: false, error: 'cota diária do Gmail esgotada' });

    MailApp.sendEmail({
      to: para.join(','),
      subject: String(dados.subject || 'Radar ASPIN').slice(0, 200),
      htmlBody: String(dados.html || ''),
      body: String(dados.text || ''),
      name: String(dados.name || 'Radar ASPIN')
    });
    return json_({ ok: true, enviados: para.length, cota_restante: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return json_({ ok: false, error: String(err).slice(0, 200) });
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
