import asyncio
import html
import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)


load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_ID = int(os.environ.get("GROUP_ID"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))

TIME_FORMAT = "%d.%m.%Y %H:%M"
MSK = timezone(timedelta(hours=3))

ALBUM_DELAY = 2.0


def _is_not_modified(exc: Exception) -> bool:
    if type(exc).__name__ == "MessageNotModified":
        return True
    return "message is not modified" in str(exc).lower()


async def _copy_messages(bot, chat_id, from_chat_id, message_ids):
    try:
        copied = await bot.copy_messages(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_ids=message_ids,
        )
        return [m.message_id for m in copied]
    except AttributeError:
        pass
    except Exception as e:
        print(f"copy_messages не сработал ({e}); fallback на copy_message")

    result = []
    for mid in message_ids:
        sent = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=mid,
        )
        result.append(sent.message_id)
    return result


async def _publish(context: ContextTypes.DEFAULT_TYPE, data: dict):
    user_message_id = data["user_message_id"]
    group_message_id = data["group_message_id"]
    user_id = data["user_id"]
    key = (user_id, user_message_id)

    published = context.bot_data.setdefault("published_posts", set())
    if key in published:
        print(f"Пост {key} уже опубликован — повторный запуск игнорируем")
        return
    published.add(key)

    sent_to_channel = False
    try:
        sent_ids = None

        source = context.bot_data.get("post_source", {}).get(key)
        if source:
            try:
                sent_ids = await _copy_messages(
                    context.bot,
                    CHANNEL_ID,
                    source["chat_id"],
                    source["message_ids"],
                )
            except Exception as e:
                print(f"Не удалось скопировать пост из источника: {e}")
                sent_ids = None

        if not sent_ids:
            media_ids = context.bot_data.get("post_media", {}).get(key)
            if not media_ids:
                media_ids = [group_message_id]
            sent_ids = await _copy_messages(context.bot, CHANNEL_ID, GROUP_ID, media_ids)

        if not sent_ids:
            raise RuntimeError("Не удалось скопировать сообщения в канал")

        sent_to_channel = True

        content = context.bot_data.get("post_content", {}).get(key, {})
        original_text = content.get("text")
        original_caption = content.get("caption")
        caption_index = content.get("caption_index", 0)

        try:
            chat = await context.bot.get_chat(user_id)
            name = chat.full_name or chat.first_name or "Автор"
        except Exception:
            name = "Автор"

        author_line = f'\n\nОт: <a href="tg://user?id={user_id}">{html.escape(name)}</a>'

        if original_text is not None and len(sent_ids) == 1:
            try:
                await context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=sent_ids[0],
                    text=html.escape(original_text) + author_line,
                    parse_mode="HTML",
                )
            except Exception as e:
                if not _is_not_modified(e):
                    print(f"Не удалось добавить автора: {e}")
        else:
            if original_caption:
                caption = html.escape(original_caption) + author_line
            else:
                caption = author_line.strip()
            target_id = sent_ids[min(caption_index, len(sent_ids) - 1)]
            try:
                await context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=target_id,
                    caption=caption,
                    parse_mode="HTML",
                )
            except Exception as e:
                if not _is_not_modified(e):
                    print(f"Не удалось добавить подпись: {e}")

        accepted_notify = context.bot_data.setdefault("accepted_notify", {})
        prev_id = accepted_notify.pop(key, None)

        if prev_id is not None:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=prev_id,
                    text="✅ Ваш пост опубликован в канале",
                )
            except Exception as e:
                if not _is_not_modified(e):
                    print(f"Не удалось обновить уведомление: {e}")
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text="✅ Ваш пост опубликован в канале",
                            reply_to_message_id=user_message_id,
                        )
                    except Exception as e2:
                        print(f"Не удалось уведомить {user_id}: {e2}")
        else:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="✅ Ваш пост опубликован в канале",
                    reply_to_message_id=user_message_id,
                )
            except Exception as e:
                print(f"Не удалось уведомить {user_id}: {e}")

        if data.get("admin_chat_id"):
            await context.bot.send_message(
                chat_id=data["admin_chat_id"],
                text="Пост опубликован в канале",
                reply_to_message_id=group_message_id,
            )

        context.bot_data.get("post_media", {}).pop(key, None)
        context.bot_data.get("post_source", {}).pop(key, None)
        context.bot_data.get("post_content", {}).pop(key, None)

    except Exception as e:
        if not sent_to_channel:
            published.discard(key)
        print(f"Ошибка публикации: {e}")
        if data.get("admin_chat_id"):
            try:
                await context.bot.send_message(
                    chat_id=data["admin_chat_id"],
                    text=f"⚠️ Не удалось опубликовать пост: {e}",
                    reply_to_message_id=group_message_id,
                )
            except Exception:
                pass


async def publish_job(context: ContextTypes.DEFAULT_TYPE):
    await _publish(context, context.job.data)


async def schedule_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_schedule")
    if pending is None:
        return

    text = (update.message.text or "").strip()
    try:
        dt = datetime.strptime(text, TIME_FORMAT).replace(tzinfo=MSK)
    except ValueError:
        example_dt = datetime.now(MSK).strftime(TIME_FORMAT)
        await update.message.reply_text(
            "Неверный формат. Отправьте дату и время так:\n"
            f"`{TIME_FORMAT}`\nНапример: {example_dt}",
            parse_mode="Markdown",
        )
        return

    now = datetime.now(MSK)
    if dt <= now:
        await update.message.reply_text("Это время уже прошло, укажите ещё раз")
        return

    if context.job_queue is None:
        await update.message.reply_text(
            'JobQueue недоступен. Установите: pip install "python-telegram-bot[job-queue]"'
        )
        context.user_data.pop("pending_schedule", None)
        return

    delay = (dt - now).total_seconds()
    context.job_queue.run_once(
        publish_job,
        when=delay,
        data={
            "group_message_id": pending["group_message_id"],
            "user_id": pending["user_id"],
            "user_message_id": pending["user_message_id"],
            "admin_chat_id": update.effective_chat.id,
        },
        name=f"publish_{pending['group_message_id']}",
    )
    context.user_data.pop("pending_schedule", None)
    await update.message.reply_text(
        f"✅ Публикация запланирована на {dt.strftime(TIME_FORMAT)} (МСК)"
    )


async def _buffer_album_message(context: ContextTypes.DEFAULT_TYPE, message):
    media_group_id = message.media_group_id
    groups = context.bot_data.setdefault("media_groups", {})

    entry = groups.get(media_group_id)
    if entry is None:
        entry = {
            "messages": [],
            "user_id": message.from_user.id,
            "chat_id": message.chat_id,
            "generation": 0,
        }
        groups[media_group_id] = entry

    entry["messages"].append({
        "message_id": message.message_id,
        "text": message.text,
        "caption": message.caption,
    })
    entry["generation"] += 1
    gen = entry["generation"]

    asyncio.create_task(_finish_album(context, media_group_id, gen))


async def _finish_album(context: ContextTypes.DEFAULT_TYPE, media_group_id, generation):
    await asyncio.sleep(ALBUM_DELAY)
    groups = context.bot_data.get("media_groups", {})
    entry = groups.get(media_group_id)
    if entry is None or entry.get("generation") != generation:
        return
    groups.pop(media_group_id, None)

    try:
        await _process_album(context, entry)
    except Exception as e:
        print(f"Ошибка обработки альбома: {e}")


async def _process_album(context: ContextTypes.DEFAULT_TYPE, entry: dict):
    messages = entry["messages"]
    user_id = entry["user_id"]
    chat_id = entry["chat_id"]
    if not messages:
        return

    text = None
    caption = None
    caption_index = 0
    for i, m in enumerate(messages):
        if m.get("text"):
            text = m["text"]
        if m.get("caption"):
            caption = m["caption"]
            caption_index = i

    primary_uid = messages[0]["message_id"]
    key = (user_id, primary_uid)
    context.bot_data.setdefault("post_content", {})[key] = {
        "text": text,
        "caption": caption,
        "caption_index": caption_index,
    }

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Пост принят в обработку администрацией",
            reply_to_message_id=primary_uid,
        )
    except Exception as e:
        print(f"Не удалось уведомить пользователя: {e}")

    message_ids = [m["message_id"] for m in messages]
    try:
        copied_ids = await _copy_messages(context.bot, GROUP_ID, chat_id, message_ids)
    except Exception as e:
        print(f"Не удалось скопировать альбом в группу: {e}")
        return
    if not copied_ids:
        print("Не удалось скопировать альбом: пустой результат")
        return

    context.bot_data.setdefault("post_media", {})[key] = copied_ids
    context.bot_data.setdefault("post_source", {})[key] = {
        "chat_id": chat_id,
        "message_ids": message_ids,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅", callback_data=f"accept:{user_id}:{primary_uid}"),
            InlineKeyboardButton("❌", callback_data=f"reject:{user_id}:{primary_uid}"),
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=f"📎 Альбом ({len(messages)} шт.) — выберите действие:",
            reply_to_message_id=copied_ids[-1],
            reply_markup=keyboard,
        )
    except Exception as e:
        print(f"Не удалось отправить кнопки для альбома: {e}")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None:
        return

    if message.media_group_id:
        await _buffer_album_message(context, message)
        return

    user_id = message.from_user.id
    user_message_id = message.message_id
    key = (user_id, user_message_id)

    context.bot_data.setdefault("post_content", {})[key] = {
        "text": message.text,
        "caption": message.caption,
        "caption_index": 0,
    }

    await message.reply_text("Пост принят в обработку администрацией")

    sent = await context.bot.copy_message(
        chat_id=GROUP_ID,
        from_chat_id=message.chat_id,
        message_id=user_message_id,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅", callback_data=f"accept:{user_id}:{user_message_id}"),
                InlineKeyboardButton("❌", callback_data=f"reject:{user_id}:{user_message_id}"),
            ]
        ]),
    )
    context.bot_data.setdefault("post_media", {})[key] = [sent.message_id]
    context.bot_data.setdefault("post_source", {})[key] = {
        "chat_id": message.chat_id,
        "message_ids": [user_message_id],
    }


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("accept:"):
        _, user_id_str, user_message_id_str = data.split(":")
        group_message_id = query.message.message_id
        user_id = int(user_id_str)
        user_message_id = int(user_message_id_str)
        key = (user_id, user_message_id)

        try:
            notify = await context.bot.send_message(
                chat_id=user_id,
                text="✅ Ваш пост принят и будет опубликован в канале",
                reply_to_message_id=user_message_id,
            )
            context.bot_data.setdefault("accepted_notify", {})[key] = notify.message_id
        except Exception as e:
            print(f"Не удалось уведомить {user_id}: {e}")

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🚀 Опубликовать сразу",
                callback_data=f"pub_now:{user_id_str}:{user_message_id_str}:{group_message_id}",
            )],
            [InlineKeyboardButton(
                "⏱ Через полчаса",
                callback_data=f"pub_30m:{user_id_str}:{user_message_id_str}:{group_message_id}",
            )],
            [InlineKeyboardButton(
                "⏱ Через 1 час",
                callback_data=f"pub_1h:{user_id_str}:{user_message_id_str}:{group_message_id}",
            )],
            [InlineKeyboardButton(
                "🕒 Своё время",
                callback_data=f"pub_custom:{user_id_str}:{user_message_id_str}:{group_message_id}",
            )],
        ]))
        return

    if data.startswith("reject:"):
        _, user_id_str, user_message_id_str = data.split(":")
        user_id = int(user_id_str)
        user_message_id = int(user_message_id_str)

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
        await query.message.reply_text("Пост отклонён")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Ваш пост отклонён администрацией",
                reply_to_message_id=user_message_id,
            )
        except Exception as e:
            print(f"Не удалось уведомить {user_id}: {e}")
        return

    if data.startswith("pub_"):
        _, user_id_str, user_message_id_str, group_message_id_str = data.split(":")
        user_id = int(user_id_str)
        user_message_id = int(user_message_id_str)
        group_message_id = int(group_message_id_str)

        payload = {
            "group_message_id": group_message_id,
            "user_id": user_id,
            "user_message_id": user_message_id,
            "admin_chat_id": query.message.chat_id,
        }

        if data.startswith("pub_custom:"):
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
            context.user_data["pending_schedule"] = payload
            example_dt = datetime.now(MSK).strftime(TIME_FORMAT)
            await query.message.reply_text(
                "Отправьте дату и время публикации в формате:\n"
                f"`{TIME_FORMAT}`\nНапример: {example_dt}",
                parse_mode="Markdown",
            )
            return

        if context.job_queue is None:
            await query.message.reply_text(
                "JobQueue недоступен. Установите: pip install \"python-telegram-bot[job-queue]\""
            )
            return

        if data.startswith("pub_now:"):
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
            await _publish(context, payload)
            return

        if data.startswith("pub_30m:"):
            delay = 1800
            label = "30 мин."
        else:
            delay = 3600
            label = "1 ч."
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
        context.job_queue.run_once(
            publish_job,
            when=delay,
            data=payload,
            name=f"publish_{group_message_id}",
        )
        await query.message.reply_text(f"✅ Публикация запланирована через {label}")


def main():
    if BOT_TOKEN is None:
        print('Токен не указан в .env: BOT_TOKEN = "..."')
        exit()
    if GROUP_ID is None:
        print('ID группы не указан в .env: GROUP_ID = "-100..."')
        exit()
    if CHANNEL_ID is None:
        print('ID канала не указан в .env: CHANNEL_ID = "-100..."')
        exit()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, schedule_time_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, message_handler))
    app.add_handler(CallbackQueryHandler(on_button))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()