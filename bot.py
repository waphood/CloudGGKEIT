import base64
import json
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx
from roles import ADMIN, LEADER, ROLES, STUDENT, Roles
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

UPLOAD = "📤 Загрузить"
DOWNLOAD = "📥 Скачать"
HOME = "🏠 В меню"
NEXT = "Далее ▶"
PREV = "◀ Назад"
ROOT = Path(__file__).parent
LIMIT = 20 * 1024 * 1024


def valid_name(name):
    return (isinstance(name, str) and bool(name.strip()) and not name.startswith(".")
            and "/" not in name and "\\" not in name
            and all(c.isprintable() for c in name) and len(name.encode()) <= 255)


async def github(method, path, *, raw=False, **kwargs):
    headers = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
               "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}
    url = "https://api.github.com/repos/waphood/CloudGGKEIT/contents/" + quote(path)
    async with httpx.AsyncClient(timeout=90) as client:
        return await client.request(method, url, headers=headers, **kwargs)


DELETE = "🗑 Удалить файл"
KEYS = "🔑 Выдать ключ"
LEADERS = "Список старост"
DONE = "Готово"
ALLOW = "Разрешить загрузку"
DENY = "Запретить загрузку"
CONFIRM = "Подтвердить удаление"


class Screen:
    def __init__(self, update, context):
        self.message = update.effective_message
        self.query = update.callback_query
        self.chat = context.chat_data

    async def show(self, prompt, rows):
        version = secrets.token_hex(6)
        actions = {}
        keys = []
        for row in rows:
            buttons = []
            for label in row:
                code = f"{version}.{len(actions)}"
                actions[code] = label
                buttons.append(InlineKeyboardButton(label, callback_data=code))
            keys.append(buttons)
        if self.query:
            await self.query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(keys))
            message_id = self.message.message_id
        else:
            sent = await self.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(keys))
            message_id = sent.message_id
        self.chat.update(menu_id=message_id, actions=actions)

    async def keyboard(self, prompt, labels):
        await self.show(prompt, [[label] for label in labels])


async def choices(screen, state, items, stage, page=0):
    start = page * 12
    visible = items[start:start + 12]
    buttons = {f"{i}. {name[:60]}": name for i, name in enumerate(visible, start + 1)}
    state.update(stage=stage, items=items, page=page, buttons=buttons)
    rows = [[label] for label in buttons]
    navigation = ([PREV] if page else []) + ([NEXT] if start + 12 < len(items) else [])
    if navigation:
        rows.append(navigation)
    rows.append([HOME])
    prompt = "Выберите предмет." if stage == "subject" else ("Выберите старосту." if stage == "leaders" else "Выберите файл.")
    if stage == "file":
        prompt = f"📁 {state['subject']}\n{prompt}"
    if len(items) > 12:
        prompt += f" · {page + 1}/{(len(items) + 11) // 12}"
    await screen.show(prompt, rows)


async def handle(update, context):
    message = update.effective_message
    state = context.user_data
    user = update.effective_user
    uid = user.id
    screen = Screen(update, context)
    query = update.callback_query
    if query:
        text = context.chat_data.get("actions", {}).get(query.data)
        if not text or context.chat_data.get("menu_id") != message.message_id:
            await query.answer("Это меню устарело. Отправьте любое слово, чтобы открыть новое.")
            return
        await query.answer()
    else:
        text = message.text or ""
        if not context.chat_data.get("keyboard_removed"):
            await message.reply_text("Кнопки меню теперь под сообщениями.", reply_markup=ReplyKeyboardRemove())
            context.chat_data["keyboard_removed"] = True
    roles = context.bot_data["roles"]
    subjects = context.bot_data["subjects"]
    if text.startswith("/start "):
        ok = roles.redeem(text[7:].strip(), uid, user.full_name)
        state.clear()
        await message.reply_text("Ключ принят." if ok else "Ключ недействителен или уже использован.")
        text = HOME
    role, allowed = roles.access(uid, subjects)
    if not role:
        state.clear()
        return await message.reply_text("Чтобы открыть материалы, перейдите по ссылке-ключу от администратора.",
                                        reply_markup=ReplyKeyboardRemove())

    async def home(info=None):
        state.clear()
        if role == STUDENT:
            state.update(stage="subject", mode=DOWNLOAD, buttons={s: s for s in subjects})
            return await screen.keyboard(info or "📚 Предметы\nВыберите папку с материалами.", subjects)
        actions = [DOWNLOAD]
        if allowed:
            actions.append(UPLOAD)
        if role == ADMIN:
            actions.extend([DELETE, KEYS, LEADERS])
        rows = [actions[:2]] + [[label] for label in actions[2:]]
        await screen.show(info or f"📚 Учебные материалы\n{role} · Главное меню", rows)

    async def invite(granted, scope):
        key = roles.issue(granted, scope)
        await message.reply_text(f"Одноразовый ключ «{granted}». Перешлите ссылку получателю.\n"
                   f"https://t.me/{context.bot.username}?start={key}")
        await home("Ключ готов. Перешлите сообщение со ссылкой.")

    stage = state.get("stage")
    selected = state.get("buttons", {}).get(text)
    if text in (HOME, "/start"):
        return await home()
    if role == ADMIN:
        if text == KEYS:
            state.clear()
            state["stage"] = "grant"
            return await screen.keyboard("Выберите роль для ключа.", [*ROLES, HOME])
        if stage == "grant" and text in ROLES:
            if text != LEADER:
                return await invite(text, [])
            state.update(stage="scope", scope=[])
            return await screen.keyboard("Отметьте предметы старосты.", [*subjects, DONE, HOME])
        if stage == "scope":
            scope = state["scope"]
            if text == DONE and scope:
                return await invite(LEADER, scope)
            if text in subjects:
                if text in scope:
                    scope.remove(text)
                else:
                    scope.append(text)
            return await screen.keyboard("Выбрано: " + (", ".join(scope) or "пока ничего"),
                                  [*subjects, DONE, HOME])
        if text == LEADERS:
            entries = roles.leaders()
            if not entries:
                return await home("Зарегистрированных старост пока нет.")
            state.clear()
            state["leaders"] = {f"{name} ({i}) · {'запрещено' if blocked else 'разрешено'}": (i, blocked)
                                for i, name, blocked in entries}
            return await choices(screen, state, list(state["leaders"]), "leaders")
        if stage == "leaders" and selected:
            target, blocked = state["leaders"][selected]
            state.update(stage="block", target=target, block=not blocked)
            return await screen.keyboard(selected, [ALLOW if blocked else DENY, HOME])
        if stage == "block" and text == (DENY if state["block"] else ALLOW):
            roles.block(state["target"], state["block"])
            return await home("Права старосты обновлены.")
        if stage == "confirm" and text == CONFIRM:
            response = await github("DELETE", state["path"], json={
                "sha": state["sha"], "message": "Удалить учебный материал"})
            response.raise_for_status()
            return await home("Файл удалён.")

    if text in (UPLOAD, DOWNLOAD, DELETE):
        if (text == DELETE and role != ADMIN) or (text == UPLOAD and not allowed):
            return await home("Недостаточно прав.")
        if role == STUDENT:
            return await home("Выберите предмет.")
        state.clear()
        state["mode"] = text
        return await choices(screen, state, allowed if text == UPLOAD else subjects, "subject")
    if text in (NEXT, PREV) and state.get("stage") in ("subject", "file", "leaders"):
        page = state["page"] + (1 if text == NEXT else -1)
        if 0 <= page * 12 < len(state["items"]):
            return await choices(screen, state, state["items"], state["stage"], page)

    selected = state.get("buttons", {}).get(text)
    if selected and state.get("stage") == "subject":
        state["subject"] = selected
        state["folder"] = "materials/" + selected
        if state["mode"] == UPLOAD:
            state.update(stage="upload", buttons={})
            return await screen.keyboard(
                f"Прикрепите файл до 20 МБ для предмета «{selected}». Он появится в публичном репозитории.",
                [HOME])
        response = await github("GET", state["folder"])
        files = [] if response.status_code == 404 else response.raise_for_status().json()
        names = sorted(f["name"] for f in files if f["type"] == "file" and valid_name(f["name"]))
        if not names:
            return await home("В этой папке пока нет файлов.")
        return await choices(screen, state, names, "file")

    if selected and state.get("stage") == "file":
        path = state["folder"] + "/" + selected
        meta = (await github("GET", path)).raise_for_status().json()
        if state["mode"] == DELETE:
            if role != ADMIN:
                return await home("Недостаточно прав.")
            state.update(stage="confirm", path=path, sha=meta["sha"], buttons={})
            return await screen.keyboard(f"Удалить «{selected}» из папки «{state['subject']}»?", [CONFIRM, HOME])
        if meta["size"] > 50 * 1024 * 1024:
            return await home("Файл превышает лимит Telegram. Скачать с GitHub: " + meta["html_url"])
        data = (await github("GET", path, raw=True, params={"ref": "main"})).raise_for_status().content
        await message.reply_document(document=data, filename=selected, write_timeout=120)
        return await home()

    if state.get("stage") == "upload" and message.document:
        if state["subject"] not in allowed:
            return await home("Загрузка по этому предмету запрещена.")
        doc = message.document
        if not valid_name(doc.file_name) or (doc.file_size or 0) > LIMIT:
            return await message.reply_text("Пришлите файл с обычным именем размером до 20 МБ.")
        data = await (await doc.get_file()).download_as_bytearray()
        response = await github("PUT", state["folder"] + "/" + doc.file_name,
                                json={"message": "Добавить учебный материал",
                                      "content": base64.b64encode(data).decode("ascii")})
        if response.status_code in (409, 422):
            return await message.reply_text("GitHub отклонил запись. Возможно, имя уже занято. Переименуйте файл и повторите.")
        response.raise_for_status()
        return await home("Файл сохранён в папке предмета.")
    await home()


async def error(update, context):
    logging.warning("Ошибка обработки: %s", type(context.error).__name__)
    if update and update.effective_message:
        context.user_data.clear()
        await Screen(update, context).keyboard("Не удалось завершить действие.", [HOME])


def main():
    subjects = json.loads((ROOT / "subjects.json").read_text(encoding="utf-8"))
    if not isinstance(subjects, list) or not all(valid_name(s) for s in subjects):
        raise ValueError("Проверьте названия предметов в subjects.json")
    if not os.environ.get("GITHUB_TOKEN"):
        raise ValueError("Укажите GITHUB_TOKEN")
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    admins = {int(s) for s in os.environ.get("ADMIN_IDS", "").split(",") if s.strip()}
    roles = Roles(os.getenv("ROLE_DB", "roles.sqlite3"), admins)
    app.bot_data.update(subjects=subjects, roles=roles)
    for value in os.getenv("UPLOADER_IDS", "").split(","):
        if value.strip() and int(value) not in admins:
            with roles.db:
                roles.db.execute("INSERT OR IGNORE INTO members(id,name,role,subjects) VALUES (?,?,?,?)",
                                 (int(value), value, "Учитель", "[]"))
    app.add_handler(CallbackQueryHandler(handle))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.Document.ALL), handle))
    app.add_error_handler(error)
    app.run_polling()


if __name__ == "__main__":
    main()
