/**
 * Вышеград РП: отдаёт боту ВСЕ ответы Google Формы «Сдача теории».
 * Это отдельный скрипт, не тот, что для формы «РП ТЕСТ». Работает от аккаунта владельца формы.
 * Админка показывает список ответов, баллы (если форма в режиме теста) и выдаёт права.
 */

// ID формы из ссылки РЕДАКТИРОВАНИЯ: https://docs.google.com/forms/d/ВОТ_ЭТО/edit
// (ссылка для прохождения /d/e/1FAIpQLS... для скрипта не подходит)
var EXAM_FORM_ID = 'https://docs.google.com/forms/d/1fuHKi0h6xuqfwRhRUnJA5H9DFz-ttqCKZO5qnrzYrgA/edit';

// По названию вопроса скрипт сам находит, где ник в Telegram, имя и машина.
// Если названия в твоей форме другие, добавь слова сюда. Админ всё равно может поправить поля вручную.
var TG_HINT   = /telegram|телеграм|телеграмм|\bтг\b|\btg\b/i;
var NAME_HINT = /имя|фио|фамил|персонаж/i;
var CAR_HINT  = /машин|марк[аи]|\bcar\b/i;
var CAT_HINT  = /категори/i;                      // «На какую категорию сдаете?»

var LIMIT = 150;   // сколько последних ответов отдавать

/** Запусти ОДИН раз: создаст секретный ключ. Он появится в журнале выполнения, его вставишь в Railway. */
function setup() {
  var token = Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty('SECRET', token);
  Logger.log('EXAM_SCRIPT_TOKEN = ' + token);
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  var secret = PropertiesService.getScriptProperties().getProperty('SECRET');
  if (!secret || p.token !== secret) return json_({ ok: false, error: 'auth' });

  var form;
  try { form = FormApp.openById(EXAM_FORM_ID); } catch (err) { return json_({ ok: false, error: 'form' }); }

  var quiz = false;
  try { quiz = form.isQuiz(); } catch (err) {}
  var pts = quiz ? itemPoints_(form) : {};

  var resps = form.getResponses();                       // от старых к новым
  var items = [];
  for (var i = resps.length - 1; i >= 0 && items.length < LIMIT; i--) {
    var r = resps[i], irs = r.getItemResponses();
    var answers = [], name = '', tg = '', car = '', cat = '', sc = 0, mx = 0, graded = false;

    for (var k = 0; k < irs.length; k++) {
      var ir = irs[k], it = ir.getItem(), title = it.getTitle(), a = String(flat_(ir.getResponse()));
      if (a.trim() === '') continue;
      var row = { q: title, a: a };

      if (quiz) {
        var s = null;
        try { s = ir.getScore(); } catch (err) {}
        var m = pts[it.getId()];
        if (s !== null && s !== undefined && m !== undefined && m > 0) {
          row.sc = s; row.pts = m; row.ok = (s >= m);
          sc += s; mx += m; graded = true;
        }
      }

      if (TG_HINT.test(title))        { if (!tg) tg = a; }
      else if (CAT_HINT.test(title))  { if (!cat) cat = a; }
      else if (CAR_HINT.test(title))  { if (!car) car = a; }
      else if (NAME_HINT.test(title)) { if (!name) name = a; }
      answers.push(row);
    }

    items.push({
      rid: r.getId(),
      ts: Math.floor(r.getTimestamp().getTime() / 1000),
      email: email_(r),
      name: name, tg: tg, car: car, cat: cat,
      sc: graded ? sc : null, mx: graded ? mx : null,
      answers: answers
    });
  }
  return json_({ ok: true, total: resps.length, quiz: quiz, items: items });
}

function itemPoints_(form) {            // баллы только у вопросов с вариантами; текстовые Google сам не проверяет
  var out = {}, its = form.getItems();
  for (var i = 0; i < its.length; i++) {
    var it = its[i], t = it.getType(), pt;
    try {
      if (t === FormApp.ItemType.MULTIPLE_CHOICE) pt = it.asMultipleChoiceItem().getPoints();
      else if (t === FormApp.ItemType.CHECKBOX) pt = it.asCheckboxItem().getPoints();
      else if (t === FormApp.ItemType.LIST) pt = it.asListItem().getPoints();
      else if (t === FormApp.ItemType.SCALE) pt = it.asScaleItem().getPoints();
    } catch (err) {}
    if (pt !== undefined) out[it.getId()] = pt;
  }
  return out;
}

function email_(r) {
  try { return r.getRespondentEmail() || ''; } catch (err) { return ''; }
}
function flat_(v) {                                       // ответ бывает строкой, списком или таблицей
  if (Array.isArray(v)) return v.map(function (x) { return Array.isArray(x) ? x.join(' / ') : x; }).join(', ');
  return v == null ? '' : v;
}
function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
