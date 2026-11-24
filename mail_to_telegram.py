"""Mail -> Telegram: присылает в Telegram уведомления о новых письмах из IMAP-ящика.

Только стандартная библиотека Python, ставить ничего не нужно.
Настройки берутся из переменных окружения, а если их нет, из .env рядом со скриптом.

Запуск:
    python mail_to_telegram.py              обычная работа на компьютере (проверка раз в минуту)
    python mail_to_telegram.py --once --min-age 180
                                            одна проверка, только письма старше 3 минут (GitHub Actions)
    python mail_to_telegram.py --check      проверить IMAP и Telegram, прислать тестовое сообщение
    python mail_to_telegram.py --chat-id    показать chat_id тех, кто написал боту
    python mail_to_telegram.py --folders    показать папки ящика (для IMAP_FOLDERS)
    python mail_to_telegram.py --mark-all   пометить все письма в ящике как уже отправленные

Кто что уже отправил, хранится в самом ящике: после отправки на письмо ставится невидимая
IMAP-метка $TgNotified. Поэтому компьютер и GitHub Actions не дублируют друг друга.
Письма не помечаются прочитанными: текст берётся через BODY.PEEK, флаг \\Seen не трогаем.
"""

import datetime
import email
import html
import imaplib
import json
import logging
import logging.handlers
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.policy import default as default_policy
from html.parser import HTMLParser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOG_PATH = os.path.join(BASE_DIR, "mail_to_telegram.log")
LOCK_PATH = os.path.join(BASE_DIR, ".lock")

KEYWORD = "$TgNotified"     # метка «уже отправлено в Telegram»
LOOKBACK_DAYS = 3           # письма старше этого не шлём никогда (защита от лавины старья)
FETCH_BYTES = 200_000       # сколько байт письма качать для превью (вложения целиком не тянем)
TELEGRAM_LIMIT = 4096
ENV_KEYS = ("IMAP_HOST", "IMAP_PORT", "IMAP_SSL", "IMAP_USER", "IMAP_PASSWORD", "IMAP_FOLDERS",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "POLL_SECONDS", "PREVIEW_CHARS")

log = logging.getLogger("mail2tg")


# ---------------------------------------------------------------- настройки

def load_env(path=ENV_PATH):
    """Сначала .env (если есть), поверх него переменные окружения (так работает GitHub Actions)."""
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                env[key.strip()] = value
    for key in ENV_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    if not env:
        sys.exit(f"Нет настроек: ни файла {path}, ни переменных окружения. Скопируй .env.example в .env.")
    return env


class Config:
    def __init__(self, env):
        def need(key):
            value = env.get(key, "")
            if not value or value.startswith("PUT_"):
                sys.exit(f"В настройках не заполнено {key}")
            return value

        self.imap_host = need("IMAP_HOST")
        self.imap_port = int(env.get("IMAP_PORT") or "993")
        self.imap_ssl = (env.get("IMAP_SSL") or "true").lower() in ("1", "true", "yes")
        self.imap_user = need("IMAP_USER")
        self.imap_password = need("IMAP_PASSWORD")
        self.folders = [f.strip() for f in (env.get("IMAP_FOLDERS") or "INBOX").split(",") if f.strip()]
        self.bot_token = need("TELEGRAM_BOT_TOKEN")
        self.chat_id = env.get("TELEGRAM_CHAT_ID", "")
        self.poll_seconds = max(30, int(env.get("POLL_SECONDS") or "60"))
        self.preview_chars = int(env.get("PREVIEW_CHARS") or "700")


# ---------------------------------------------------------------- Telegram

def tg_call(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram {method}: HTTP {e.code} {body}") from None


def tg_send(cfg, text):
    if not cfg.chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID пустой. Узнай его: python mail_to_telegram.py --chat-id")
    params = {"chat_id": cfg.chat_id, "disable_web_page_preview": "true"}
    try:
        result = tg_call(cfg.bot_token, "sendMessage",
                         {**params, "text": text[:TELEGRAM_LIMIT], "parse_mode": "HTML"})
    except RuntimeError as e:
        if "HTTP 400" not in str(e) or "chat not found" in str(e):
            raise
        # Telegram не принял разметку (например, обрезали посреди тега): шлём простым текстом,
        # чтобы одно странное письмо не застопорило всю очередь.
        log.warning("HTML не принят (%s), шлю простым текстом", e)
        plain = html.unescape(re.sub(r"</?[bi]>", "", text))
        result = tg_call(cfg.bot_token, "sendMessage", {**params, "text": plain[:TELEGRAM_LIMIT]})
    if not result.get("ok"):
        raise RuntimeError(f"Telegram ответил: {result}")


# ---------------------------------------------------------------- разбор письма

class _TextFromHtml(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup):
    parser = _TextFromHtml()
    parser.feed(markup)
    return "".join(parser.parts)


def body_preview(msg, limit):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # битая кодировка или обрезанное письмо
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def format_message(raw, folder, footer, preview_chars):
    msg = email.message_from_bytes(raw, policy=default_policy)
    sender = str(msg.get("From", "(без отправителя)"))
    subject = str(msg.get("Subject", "")) or "(без темы)"
    attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    preview = body_preview(msg, preview_chars)

    lines = [f"📧 <b>{html.escape(subject)}</b>", f"От: {html.escape(sender)}"]
    if folder.upper() != "INBOX":
        lines.append(f"Папка: {html.escape(folder)}")
    if attachments:
        lines.append("📎 " + html.escape(", ".join(attachments)))
    if preview:
        lines += ["", html.escape(preview)]
    lines += ["", f"<i>{html.escape(footer)}</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- IMAP

def imap_connect(cfg):
    if cfg.imap_ssl:
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port,
                                 ssl_context=ssl.create_default_context(), timeout=60)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=60)
        conn.starttls(ssl_context=ssl.create_default_context())
    conn.login(cfg.imap_user, cfg.imap_password)
    return conn


def quote_folder(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(days_ago):
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{d.day}-{months[d.month - 1]}-{d.year}"  # формат IMAP не зависит от локали Windows


def check_folder(conn, cfg, folder, min_age, footer):
    """Шлёт в Telegram письма без метки $TgNotified и ставит метку. Возвращает число отправленных.

    min_age > 0: брать только письма, пришедшие в ящик больше min_age секунд назад.
    Так GitHub Actions даёт компьютеру время отправить письмо первым.
    """
    typ, _ = conn.select(quote_folder(folder))  # read-write, чтобы ставить метку; \\Seen не трогаем
    if typ != "OK":
        log.error("Не удалось открыть папку %s", folder)
        return 0

    criteria = ["UNKEYWORD", KEYWORD, "SINCE", imap_date(LOOKBACK_DAYS)]
    if min_age > 0:
        criteria += ["OLDER", str(min_age)]  # расширение WITHIN (RFC 5032)
    _, data = conn.uid("SEARCH", None, *criteria)
    uids = sorted(int(u) for u in data[0].split()) if data and data[0] else []

    sent = 0
    for uid in uids:
        _, parts = conn.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{FETCH_BYTES}>)")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
        if raw is None:
            log.warning("UID %s: пустой ответ FETCH, помечаю и пропускаю", uid)
        else:
            # Если Telegram упадёт, исключение вылетит до метки и письмо уйдёт на следующем круге.
            tg_send(cfg, format_message(raw, folder, footer, cfg.preview_chars))
            sent += 1
            log.info("Отправлено: папка %s, UID %s", folder, uid)
        conn.uid("STORE", str(uid), "+FLAGS.SILENT", f"({KEYWORD})")
    return sent


def poll_once(cfg, min_age=0, footer=None):
    footer = footer or cfg.imap_user
    conn = imap_connect(cfg)
    try:
        return sum(check_folder(conn, cfg, folder, min_age, footer) for folder in cfg.folders)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- режимы

def run_forever(cfg):
    log.info("Старт. Ящик %s@%s, папки %s, опрос раз в %s с",
             cfg.imap_user, cfg.imap_host, cfg.folders, cfg.poll_seconds)
    failures = 0
    while True:
        try:
            poll_once(cfg)
            if failures >= 5:
                tg_send(cfg, "✅ Почта снова проверяется.")
            failures = 0
        except imaplib.IMAP4.error as e:
            failures += 1
            log.error("Ошибка IMAP (%d подряд): %s", failures, e)
            if "AUTH" in str(e).upper() or "LOGIN" in str(e).upper():
                _notify_once(cfg, failures, f"⚠️ Не могу войти в почту {cfg.imap_user}: {e}")
        except Exception as e:  # сеть, таймауты, Telegram
            failures += 1
            log.exception("Ошибка (%d подряд): %s", failures, e)
            if failures == 5:
                _notify_once(cfg, failures, f"⚠️ Почта не проверяется уже {failures} раз подряд: {e}")
        # При серии ошибок ждём дольше (до 15 минут), чтобы не долбить сервер.
        time.sleep(min(cfg.poll_seconds * (2 ** min(failures, 4)), 900))


def _notify_once(cfg, failures, text):
    if failures in (1, 5):
        try:
            tg_send(cfg, html.escape(text))
        except Exception:
            log.exception("Не удалось отправить предупреждение в Telegram")


def cmd_once(cfg, min_age):
    sent = poll_once(cfg, min_age, footer=f"{cfg.imap_user} · через GitHub, компьютер был выключен")
    print(f"Проверка завершена, отправлено писем: {sent}")


def cmd_mark_all(cfg):
    conn = imap_connect(cfg)
    for folder in cfg.folders:
        if conn.select(quote_folder(folder))[0] != "OK":
            print(f"  папка {folder}: не найдена")
            continue
        _, data = conn.uid("SEARCH", None, "UNKEYWORD", KEYWORD)
        uids = data[0].split() if data and data[0] else []
        if uids:
            conn.uid("STORE", b",".join(uids).decode(), "+FLAGS.SILENT", f"({KEYWORD})")
        print(f"  папка {folder}: помечено {len(uids)}")
    conn.logout()


def cmd_check(cfg):
    print(f"IMAP: подключаюсь к {cfg.imap_host}:{cfg.imap_port} как {cfg.imap_user} ...")
    conn = imap_connect(cfg)
    for folder in cfg.folders:
        typ, data = conn.select(quote_folder(folder), readonly=True)
        print(f"  папка {folder}: {'OK, писем ' + data[0].decode() if typ == 'OK' else 'НЕ НАЙДЕНА'}")
    conn.logout()
    print("Telegram: отправляю тестовое сообщение ...")
    tg_send(cfg, f"✅ Бот подключён к почте <b>{html.escape(cfg.imap_user)}</b>.\n"
                 f"Новые письма будут приходить сюда.")
    print("Готово. Проверь Telegram.")


def cmd_chat_id(token):
    result = tg_call(token, "getUpdates", {})
    chats = {}
    for upd in result.get("result", []):
        chat = (upd.get("message") or upd.get("my_chat_member") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("Пусто. Напиши боту в Telegram любое сообщение (например /start) и запусти ещё раз.")
    for chat_id, name in chats.items():
        print(f"chat_id = {chat_id}   ({name})")


def decode_imap_utf7(name):
    """Имена папок в IMAP закодированы modified UTF-7 (&BCEEHwQQBBw- = СПАМ)."""
    def repl(m):
        chunk = m.group(1)
        if not chunk:
            return "&"
        return ("+" + chunk.replace(",", "/") + "-").encode().decode("utf-7")
    return re.sub(r"&([^-]*)-", repl, name)


def cmd_folders(cfg):
    conn = imap_connect(cfg)
    _, data = conn.list()
    conn.logout()
    print("Имя для IMAP_FOLDERS          как видно в почте")
    for line in data:
        raw = line.decode()
        m = re.match(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)', raw)
        name = m.group("name").strip('"') if m else raw
        print(f"  {name:<28} {decode_imap_utf7(name)}")


# ---------------------------------------------------------------- запуск

def setup_logging(to_console, to_file=True):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if to_file:
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    if to_console and sys.stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def acquire_single_instance_lock():
    """Не даём запустить вторую копию на компьютере (автозапуск + ручной запуск)."""
    import msvcrt
    lock = open(LOCK_PATH, "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error("Уже запущена другая копия, выхожу.")
        sys.exit(0)
    return lock  # держим файл открытым всё время работы


def arg_value(args, name, default):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
    setup_logging(to_console=bool(args) or sys.stdout is not None, to_file=not in_ci)
    env = load_env()
    if "--chat-id" in args:  # тут нужен только токен бота, пароль почты ещё может быть не вписан
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        if not token or token.startswith("PUT_"):
            sys.exit("Не заполнено TELEGRAM_BOT_TOKEN")
        cmd_chat_id(token)
        return
    cfg = Config(env)
    if "--check" in args:
        cmd_check(cfg)
    elif "--folders" in args:
        cmd_folders(cfg)
    elif "--mark-all" in args:
        cmd_mark_all(cfg)
    elif "--once" in args:
        cmd_once(cfg, int(arg_value(args, "--min-age", "0")))
    else:
        _lock = acquire_single_instance_lock()
        run_forever(cfg)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            log.error("%s", e.code)
        raise
