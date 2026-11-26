import base64
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx
from telegram import ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters

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


async def menu(message, state, info="Выберите действие."):
    state.clear()
    await message.reply_text(info, reply_markup=ReplyKeyboardMarkup(
        [[UPLOAD, DOWNLOAD]], resize_keyboard=True))


async def choices(message, state, items, stage, page=0):
    start = page * 12
    visible = items[start:start + 12]
    buttons = {f"{i}. {name[:60]}": name for i, name in enumerate(visible, start + 1)}
    state.update(stage=stage, items=items, page=page, buttons=buttons)
    rows = [[label] for label in buttons]
    navigation = ([PREV] if page else []) + ([NEXT] if start + 12 < len(items) else [])
    if navigation:
        rows.append(navigation)
    rows.append([HOME])
    prompt = "Выберите предмет." if stage == "subject" else "Выберите файл."
    await message.reply_text(prompt, reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True))


async def handle(update, context):
    message = update.effective_message
    state = context.user_data
    text = message.text
    if text in (UPLOAD, DOWNLOAD):
        state.clear()
        if text == UPLOAD and update.effective_user.id not in context.bot_data["uploaders"]:
            return await menu(message, state, "Загружать материалы может только редактор.")
        subjects = context.bot_data["subjects"]
        if not subjects:
            return await menu(message, state, "Предметы пока не добавлены.")
        state["mode"] = text
        return await choices(message, state, subjects, "subject")

    if text in (NEXT, PREV) and state.get("stage") in ("subject", "file"):
        page = state["page"] + (1 if text == NEXT else -1)
        if 0 <= page * 12 < len(state["items"]):
            return await choices(message, state, state["items"], state["stage"], page)

    selected = state.get("buttons", {}).get(text)
    if selected and state.get("stage") == "subject":
        state["folder"] = "materials/" + selected
        if state["mode"] == UPLOAD:
            state.update(stage="upload", buttons={})
            return await message.reply_text(
                f"Прикрепите файл до 20 МБ для предмета «{selected}». Он появится в публичном репозитории.",
                reply_markup=ReplyKeyboardMarkup([[HOME]], resize_keyboard=True))
        response = await github("GET", state["folder"])
        files = [] if response.status_code == 404 else response.raise_for_status().json()
        names = sorted(f["name"] for f in files if f["type"] == "file" and valid_name(f["name"]))
        if not names:
            return await menu(message, state, "В этой папке пока нет файлов.")
        return await choices(message, state, names, "file")

    if selected and state.get("stage") == "file":
        path = state["folder"] + "/" + selected
        meta = (await github("GET", path)).raise_for_status().json()
        if meta["size"] > 50 * 1024 * 1024:
            return await menu(message, state, "Файл превышает лимит Telegram. Скачать с GitHub: " + meta["html_url"])
        data = (await github("GET", path, raw=True, params={"ref": "main"})).raise_for_status().content
        await message.reply_document(document=data, filename=selected, write_timeout=120)
        return await menu(message, state)

    if state.get("stage") == "upload" and message.document:
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
        return await menu(message, state, "Файл сохранён в папке предмета.")
    await menu(message, state)


async def error(update, context):
    logging.warning("Ошибка обработки: %s", type(context.error).__name__)
    if update and update.effective_message:
        await menu(update.effective_message, context.user_data, "Не удалось завершить действие. Попробуйте снова.")


def main():
    subjects = json.loads((ROOT / "subjects.json").read_text(encoding="utf-8"))
    if not isinstance(subjects, list) or not all(valid_name(s) for s in subjects):
        raise ValueError("Проверьте названия предметов в subjects.json")
    if not os.environ.get("GITHUB_TOKEN"):
        raise ValueError("Укажите GITHUB_TOKEN")
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data.update(subjects=subjects, uploaders={
        int(s) for s in os.getenv("UPLOADER_IDS", "").split(",") if s.strip()})
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.Document.ALL), handle))
    app.add_error_handler(error)
    app.run_polling()


if __name__ == "__main__":
    main()
