/**
 * Вышеград РП: отдаёт боту ответы Google Формы «РП ТЕСТ».
 * Работает от твоего аккаунта (владельца формы). Бот спрашивает ответы одного игрока,
 * админка показывает их в карточке игрока. Подробная инструкция в чате.
 */
var FORM_ID = '1xbpzVYk-PkOb7wOZ-AuVXNziw4T1RFYBqvLN2LcjriE';

// Вопрос, где игрок пишет свой Telegram-ник (ищем по слову в названии вопроса).
var TG_HINT = /telegram|телеграм|телеграмм|\bтг\b|\btg\b/i;
// Запасной поиск, если ник не нашёлся: вопрос с именем, совпадающим с именем в паспорте.
var NAME_HINT = /имя|фио|паспорт|ник/i;

/** Запусти ОДИН раз: создаст секретный ключ. Ключ появится в журнале выполнения, его вставишь в бота. */
function setup() {
  var token = Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty('SECRET', token);
  Logger.log('FORM_SCRIPT_TOKEN = ' + token);
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  var secret = PropertiesService.getScriptProperties().getProperty('SECRET');
  if (!secret || p.token !== secret) return json_({ ok: false, error: 'auth' });

  var uname = norm_(p.u), id = String(p.id || '').trim(), pname = norm_(p.n);
  var resps = FormApp.openById(FORM_ID).getResponses();   // от старых к новым
  var best = null, by = '';

  for (var i = resps.length - 1; i >= 0; i--) {            // идём с конца: нужна самая свежая анкета
    var items = resps[i].getItemResponses(), hitTg = false, hitName = false;
    for (var k = 0; k < items.length; k++) {
      var title = items[k].getItem().getTitle(), val = norm_(flat_(items[k].getResponse()));
      if (TG_HINT.test(title)) {
        if ((uname && val === uname) || (id && val === id)) hitTg = true;
      } else if (pname && NAME_HINT.test(title) && val === pname) {
        hitName = true;
      }
    }
    if (hitTg) { best = resps[i]; by = 'tg'; break; }
    if (hitName && !best) { best = resps[i]; by = 'name'; }
  }

  if (!best) return json_({ ok: true, found: false, total: resps.length });

  var answers = [];
  best.getItemResponses().forEach(function (ir) {
    var a = flat_(ir.getResponse());
    if (String(a).trim() !== '') answers.push({ q: ir.getItem().getTitle(), a: String(a) });
  });
  return json_({
    ok: true, found: true, by: by, total: resps.length,
    ts: Math.floor(best.getTimestamp().getTime() / 1000), answers: answers
  });
}

function norm_(v) {
  return String(v == null ? '' : v).trim().toLowerCase().replace(/^https?:\/\/t\.me\//, '').replace(/^@/, '');
}
function flat_(v) {                                          // ответ бывает строкой, списком или таблицей
  if (Array.isArray(v)) return v.map(function (x) { return Array.isArray(x) ? x.join(' / ') : x; }).join(', ');
  return v == null ? '' : v;
}
function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
