"""Бот-часть мини-аппа: кнопка в клавиатуре, кнопка меню, секция [webapp] в config.ini."""

from aiogram.methods import SetChatMenuButton

from bginfo.bot.app import _set_menu_button
from bginfo.bot.render import keyboard
from bginfo.config import load_config
from bginfo.engine.dialog import Button


def test_keyboard_webapp_button():
    rows = [[Button("Далее", "go"), Button("📝 В форме", "webapp")]]
    kb = keyboard(rows, 1, 0, "https://form.example")
    assert kb.inline_keyboard[0][1].web_app.url == "https://form.example"
    assert kb.inline_keyboard[0][0].callback_data == "1:0:go:"
    # без адреса кнопка пропускается (а пустой ряд не создаётся)
    assert len(keyboard(rows, 1, 0).inline_keyboard[0]) == 1
    assert keyboard([[Button("x", "webapp")]], 1, 0).inline_keyboard == []


async def test_menu_button_set_only_with_url():
    class App:
        def __init__(self, url, fail=False):
            self.calls, self.fail = [], fail
            self.dialog = type("D", (), {"webapp_url": url})()
            self.bot = self

        async def set_chat_menu_button(self, **kw):
            if self.fail:
                raise RuntimeError("boom")
            self.calls.append(kw)

    a = App("https://form.example")
    await _set_menu_button(a)
    assert a.calls[0]["menu_button"].web_app.url == "https://form.example" and a.calls[0]["menu_button"].text == "Анкета"
    b = App(None)
    await _set_menu_button(b)
    assert b.calls == []
    await _set_menu_button(App("https://form.example", fail=True))  # ошибка только логируется
    assert SetChatMenuButton  # метод aiogram существует


def test_config_webapp_defaults_and_ini(tmp_path):
    cfg = load_config(tmp_path / "missing.ini")
    assert (cfg.webapp.host, cfg.webapp.port, cfg.webapp.public_url) == ("127.0.0.1", 8092, "")
    assert cfg.webapp.init_data_ttl_hours == 24 and cfg.webapp.send_result_to_chat is True
    ini = tmp_path / "c.ini"
    ini.write_text("[webapp]\npublic_url = https://f.example\nsend_result_to_chat = false\nport = 9000\n",
                   encoding="utf-8")
    cfg = load_config(ini)
    assert cfg.webapp.public_url == "https://f.example" and cfg.webapp.send_result_to_chat is False
    assert cfg.webapp.port == 9000
    real = load_config("config.ini")
    assert real.webapp.public_url == "https://form.bginfo.eu" and real.webapp.port == 8092
