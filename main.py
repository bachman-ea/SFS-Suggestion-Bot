from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)
from dotenv import load_dotenv
from datetime import datetime
import html
import os


load_dotenv()

TOKEN = os.environ.get("TOKEN")
GROUP_ID = int(os.environ.get("GROUP_ID"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))

TIME_FORMAT = "%d.%m.%Y %H:%M"


async def _publish(context: ContextTypes.DEFAULT_TYPE, data: dict):
    try:
        sent = await context.bot.copy_message(
            chat_id=CHANNEL_ID,
            from_chat_id=GROUP_ID,
            message_id=data["group_message_id"],
            reply_markup=None,
        )
        message_id = sent.message_id

        content = context.bot_data.get("post_content", {}).get(data["user_message_id"], {})
        original_text = content.get("text")
        original_caption = content.get("caption")

        user_id = data["user_id"]
        try:
            chat = await context.bot.get_chat(user_id)
            name = chat.full_name or chat.first_name or "Автор"
        except Exception:
            name = "Автор"

        author_line = f'\n\nОт: <a href="tg://user?id={user_id}">{html.escape(name)}</a>'

        if original_text is not None:
            try:
                await context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=message_id,
                    text=html.escape(original_text) + author_line,
                    parse_mode="HTML",
                )
            except Exception as e:
                print(f"Не удалось добавить автора: {e}")
        else:
            if original_caption:
                caption = html.escape(original_caption) + author_line
            else:
                caption = author_line.strip()
            try:
                await context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=message_id,
                    caption=caption,
                    parse_mode="HTML",
                )
            except Exception as e:
                print(f"Не удалось добавить подпись: {e}")
                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=author_line.strip(),
                        parse_mode="HTML",
                    )
                except Exception as e2:
                    print(f"Не удалось отправить автора отдельно: {e2}")

        await context.bot.send_message(
            chat_id=user_id,
            text="✅ Ваш пост опубликован в канале",
            reply_to_message_id=data["user_message_id"],
        )
        if data.get("admin_chat_id"):
            await context.bot.send_message(
                chat_id=data["admin_chat_id"],
                text="Пост опубликован в канале",
                reply_to_message_id=data["group_message_id"],
            )
    except Exception as e:
        print(f"Ошибка публикации: {e}")
        if data.get("admin_chat_id"):
            try:
                await context.bot.send_message(
                    chat_id=data["admin_chat_id"],
                    text=f"⚠️ Не удалось опубликовать пост: {e}",
                    reply_to_message_id=data["group_message_id"],
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
        dt = datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        example_dt = datetime.now().strftime(TIME_FORMAT)
        await update.message.reply_text(
            "Неверный формат. Отправьте дату и время так:\n"
            f"`{TIME_FORMAT}`\nНапример: {example_dt}",
            parse_mode="Markdown",
        )
        return

    now = datetime.now()
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
        f"✅ Публикация запланирована на {dt.strftime(TIME_FORMAT)}"
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message_id = update.message.message_id

    context.bot_data.setdefault("post_content", {})[user_message_id] = {
        "text": update.message.text,
        "caption": update.message.caption,
    }

    await update.message.reply_text("Пост принят в обработку администрацией")

    await context.bot.copy_message(
        chat_id=GROUP_ID,
        from_chat_id=update.message.chat_id,
        message_id=user_message_id,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅", callback_data=f"accept:{user_id}:{user_message_id}"),
                InlineKeyboardButton("❌", callback_data=f"reject:{user_id}:{user_message_id}"),
            ]
        ]),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("accept:"):
        _, user_id_str, user_message_id_str = data.split(":")
        group_message_id = query.message.message_id
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
        await query.edit_message_reply_markup(reply_markup=None)
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
            await query.edit_message_reply_markup(reply_markup=None)
            context.user_data["pending_schedule"] = payload
            example_dt = datetime.now().strftime(TIME_FORMAT)
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
            await query.edit_message_reply_markup(reply_markup=None)
            await _publish(context, payload)
            return

        if data.startswith("pub_30m:"):
            delay = 1800
            label = "30 мин."
        else:
            delay = 3600
            label = "1 ч."
        await query.edit_message_reply_markup(reply_markup=None)
        context.job_queue.run_once(
            publish_job,
            when=delay,
            data=payload,
            name=f"publish_{group_message_id}",
        )
        await query.message.reply_text(f"✅ Публикация запланирована через {label}")


def main():
    if TOKEN is None:
        print('Токен не указан в .env: TOKEN = "..."')
        exit()
    if GROUP_ID is None:
        print('ID группы не указан в .env: GROUP_ID = "-100..."')
        exit()
    if CHANNEL_ID is None:
        print('ID канала не указан в .env: CHANNEL_ID = "-100..."')
        exit()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, schedule_time_handler))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, message_handler))
    app.add_handler(CallbackQueryHandler(on_button))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()