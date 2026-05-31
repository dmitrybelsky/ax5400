# Управление Redmi AX5400 через SSH: персистентность и возможности

После получения SSH-доступа (см. [README](README.md)) роутер становится
полностью программируемым устройством. Этот документ описывает:

- как устроено хранилище на RA74 и что выживает после перезагрузки
- как правильно закреплять изменения
- что можно сделать с роутером, недоступное через веб-интерфейс

---

## Карта файловой системы

RA74 работает **без overlayfs** — нет стандартного OpenWRT-механизма
«верхнего слоя» поверх squashfs. Каждый раздел монтируется напрямую:

```
/dev/ubi0_0  →  /           squashfs + ubifs  (read-only + rw slices)
/dev/ubi1_0  →  /data       ubifs             READ-WRITE, персистентен
MTD partitions:
  rootfs      squashfs  (прошивка, read-only)
  overlay     ubifs     (UCI config, crontabs — через /etc mount)
```

### Что переживает перезагрузку

| Путь | Тип | Персистентен | Примечание |
|------|-----|:---:|---|
| `/data/` | ubifs | **✓** | Основное rw-хранилище для кастомных файлов |
| `/etc/config/` | ubifs | **✓** | UCI-конфиги (wireless, dhcp, network…) |
| `/etc/crontabs/` | ubifs | **✓** | Cron-расписания пользователей |
| `/etc/` (остальное) | ramfs | ✗ | Генерируется при старте из squashfs |
| `/root/` | squashfs | ✗ | Read-only; нужен `mount -t tmpfs tmpfs /root` |
| `/var/run/` | tmpfs | ✗ | Сокеты, PID-файлы — только в памяти |
| `/tmp/` | tmpfs | ✗ | Только в памяти |
| `/lib/`, `/sbin/`, `/usr/` | squashfs | ✗ (ro) | Прошивка, нельзя менять |

**Правило:** всё кастомное кладём в `/data/`. UCI-конфиги меняем через
`uci set ... && uci commit` — они пишут в `/etc/config/` (ubifs).

### Особые случаи

**`/etc/dropbear/`** — ramfs. Dropbear при старте ищет
`/etc/dropbear/authorized_keys`. Файл нужно восстанавливать при каждой
загрузке из `/data/`.

**`/etc/shadow`** — ramfs. Пароль root сбрасывается. Восстанавливается
через `sed` в boot-скрипте.

**`/var/run/hostapd/`** — tmpfs. Глобальный hostapd-сокет исчезает после
перезагрузки и пересоздаётся сервисом `qca-hostapd` (init S15).

---

## Схема персистентного boot-скрипта

Всё, что нужно при каждой загрузке, делается через один скрипт в `/data/`:

```
/etc/crontabs/root  (ubifs) ──@reboot──▶  /data/fix_ssh.sh  (ubifs)
                                                │
                    ┌───────────────────────────┼──────────────────────┐
                    ▼                           ▼                      ▼
         /etc/dropbear/authorized_keys    /root/.ssh/         dropbear
         (symlink → /data/authorized_keys) (tmpfs mount)      (если упал)
```

Плюс отдельный `@reboot /data/patch_hostapd.sh &` — применяет
параметры к hostapd-конфигам после того, как wifi up завершится.

Полный `/data/fix_ssh.sh`:

```sh
#!/bin/sh
# Восстановление SSH и сетевых алиасов после перезагрузки.
# Запускается cron @reboot и каждую минуту (watchdog).

# 1. SSH-ключи и хост-ключ dropbear
ln -sf /data/authorized_keys /etc/dropbear/authorized_keys
ln -sf /data/dropbear_rsa_host_key /etc/dropbear/dropbear_rsa_host_key
chmod 600 /data/authorized_keys /data/dropbear_rsa_host_key

# 2. Firewall — разрешить SSH (на случай нестандартных правил)
iptables -I INPUT -p tcp --dport 22 -j ACCEPT 2>/dev/null

# 3. Dropbear watchdog
pidof dropbear >/dev/null || dropbear -p 22

# 4. Дополнительный IP для управления (можно не использовать)
ip addr show br-lan | grep -q 192.168.1.NNN || \
    ip addr add 192.168.1.NNN/24 dev br-lan

# 5. Глобальный hostapd-сокет (wifi up не поднимет интерфейсы без него)
[ -S /var/run/hostapd/global ] || {
    mkdir -p /var/run/hostapd
    hostapd -g /var/run/hostapd/global -B -P /var/run/hostapd-global.pid
}
```

Регистрация в crontab (только через роутер — на маке crontab роутера недоступен):

```sh
crontab -l 2>/dev/null | grep -v fix_ssh | grep -v patch_hostapd > /tmp/ct
echo '@reboot /data/fix_ssh.sh'           >> /tmp/ct
echo '* * * * * pidof dropbear >/dev/null || /data/fix_ssh.sh'  >> /tmp/ct
echo '@reboot /data/patch_hostapd.sh &'   >> /tmp/ct
crontab /tmp/ct
```

---

## Особенность WiFi-стека на RA74

На этом роутере есть нетривиальный баг/особенность прошивки:

### Именование радио (противоположно ожидаемому!)

| UCI-устройство | hwmode | Создаёт интерфейс |
|----------------|--------|-------------------|
| `wifi0` | `11axg` (2.4 GHz) | **`wl1`** |
| `wifi1` | `11axa` (5 GHz) | **`wl0`** |

`wifi0` → 2.4 GHz → `wl1`. `wifi1` → 5 GHz → `wl0`. Именно так, не наоборот.

### Каналы и DFS

DFS-каналы (52, 56, 60, 64, 100, 104 …) **не работают** — прошивка блокирует
их при старте с ошибкой `sh: out of range`. Безопасные диапазоны:

| Диапазон | Рабочие каналы |
|----------|----------------|
| 2.4 GHz | 1, 6, 11 |
| 5 GHz UNII-1 | 36, 40, 44, 48 |
| 5 GHz UNII-3 | 149, 153, 157, 161 |

### Баг `iface_mgr_setup`

`/sbin/wifi up` вызывает функцию `iface_mgr_setup` из
`/lib/wifi/qcawificfg80211.sh`, но она **не определена** в прошивке 1.0.63.
Из-за этого глобальный hostapd-сокет не перезапускается после `wifi down`,
и оба радиоинтерфейса остаются нерабочими.

**Обходное решение:** вручную запускать global hostapd перед `wifi up`:

```sh
wifi down
sleep 2
kill $(pidof hostapd) 2>/dev/null
rm -f /var/run/hostapd/global /var/run/hostapd-global.pid
mkdir -p /var/run/hostapd
hostapd -g /var/run/hostapd/global -B -P /var/run/hostapd-global.pid
sleep 2
wifi up
```

---

## Dumb AP: пошаговая конфигурация

Задача: роутер как точка доступа без DHCP, шлюз — другое устройство (MikroTik, pfSense…).

### 1. Каналы и SSID

```sh
# Для каждого роутера — уникальные непересекающиеся каналы
uci set wireless.wifi0.channel=6        # 2.4 GHz (wifi0 = 2.4G!)
uci set wireless.wifi1.channel=44       # 5 GHz  (wifi1 = 5G!)
uci set wireless.@wifi-iface[0].ssid='MyNetwork'
uci set wireless.@wifi-iface[0].key='mypassword'
uci set wireless.@wifi-iface[0].encryption='psk2'
uci set wireless.@wifi-iface[1].ssid='MyNetwork'
uci set wireless.@wifi-iface[1].key='mypassword'
uci set wireless.@wifi-iface[1].encryption='psk2'
uci commit wireless
```

### 2. Отключение DHCP-сервера

```sh
uci set dhcp.lan.ignore=1
uci commit dhcp
```

### 3. IP-адрес для управления

По умолчанию роутер получает адрес 192.168.31.1. После подключения к основной
сети как dumb AP управлять им можно через дополнительный IP в той же подсети:

```sh
# Временно (до перезагрузки)
ip addr add 192.168.1.50/24 dev br-lan

# Постоянно — добавить в /data/fix_ssh.sh:
ip addr show br-lan | grep -q 192.168.1.50 || ip addr add 192.168.1.50/24 dev br-lan
```

### 4. 802.11r (быстрый роуминг)

UCI-параметры для 802.11r на этой прошивке не применяются надёжно.
Надёжный способ — патчить hostapd-конфиги напрямую после `wifi up`:

```sh
#!/bin/sh
# /data/patch_hostapd.sh
SSID='MyNetwork'
PASS='mypassword'
sleep 15   # ждём пока wifi up завершится

for conf in /var/run/hostapd-wl0.conf /var/run/hostapd-wl1.conf; do
    [ -f "$conf" ] || continue
    sed -i "s/^ssid=.*/ssid=${SSID}/"                "$conf"
    sed -i "s/^wpa_passphrase=.*/wpa_passphrase=${PASS}/" "$conf"
    sed -i "s/wpa_key_mgmt=WPA-PSK.*/wpa_key_mgmt=WPA-PSK FT-PSK/" "$conf"
    grep -q "mobility_domain=" "$conf" || cat >> "$conf" << 'PARAMS'
ieee80211r=1
mobility_domain=1A2B
ft_psk_generate_local=1
ft_over_ds=1
bss_transition=1
rrm_neighbor_report=1
rrm_beacon_report=1
PARAMS
done
kill -HUP $(pidof hostapd) 2>/dev/null
```

Все AP в сети должны использовать **одинаковый** `mobility_domain`.

### Пример: три роутера с роумингом без пересечения каналов

| Роутер | 2.4 GHz | 5 GHz | Управление |
|--------|---------|-------|------------|
| AP-1 | ch 1 | ch 36 | 192.168.1.100 |
| AP-2 | ch 6 | ch 44 | 192.168.1.101 |
| AP-3 | ch 11 | ch 149 | 192.168.1.103 |

---

## Что открывается с SSH-доступом

### Конфигурация сети

```sh
# Статический IP на LAN-интерфейсе вместо DHCP-клиента
uci set network.lan.proto=static
uci set network.lan.ipaddr=192.168.1.50
uci set network.lan.netmask=255.255.255.0
uci set network.lan.gateway=192.168.1.1
uci commit network

# VLAN на LAN-портах (пример: отдельный порт для гостевой сети)
# Через DSA/swconfig в зависимости от прошивки
```

### Кастомные правила firewall

```sh
# Полный контроль через iptables напрямую
iptables -I INPUT -s 192.168.1.0/24 -j ACCEPT
iptables -I FORWARD -i br-lan -o br-lan -j ACCEPT

# Блокировка определённых хостов
iptables -I FORWARD -s 192.168.1.50 -j DROP

# Или через UCI (сохраняется через перезагрузку)
uci add firewall rule
uci set firewall.@rule[-1].src=lan
uci set firewall.@rule[-1].dest_ip=8.8.8.8
uci set firewall.@rule[-1].target=DROP
uci commit firewall
```

### Мониторинг клиентов

```sh
# Все подключённые WiFi-клиенты
iw dev wl0 station dump
iw dev wl1 station dump

# RSSI, скорость, счётчики по клиенту
iw dev wl0 station get AA:BB:CC:DD:EE:FF

# Вывод активных соединений
cat /proc/net/arp            # ARP-таблица
ip neigh                     # то же, новый стиль

# Нагрузка на интерфейсы
cat /proc/net/dev
ifconfig br-lan
```

### Оптимизация WiFi

```sh
# Принудительное переключение на другой канал (например, с наименьшей нагрузкой)
hostapd_cli -i wl0 chan_switch 5 5220   # ch44 = 5220 MHz

# Сканирование окружения (показывает соседние AP и их каналы)
iw dev wl1 scan | grep -E 'SSID|freq|signal'

# Настройка мощности передатчика
iw dev wl0 set txpower fixed 2000     # 20 dBm

# Beacon interval
uci set wireless.wifi0.beacon_int=100
uci commit wireless
```

### Диагностика качества соединения

```sh
# Тест пропускной способности по сети (если iperf3 есть)
iperf3 -s &                     # сервер
iperf3 -c 192.168.1.1           # клиент

# Latency до шлюза
ping -c 10 192.168.1.1

# DNS и маршрутизация
nslookup google.com 8.8.8.8
traceroute 1.1.1.1
ip route show
```

### Работа с logs

```sh
# Системный лог (в памяти, только текущая сессия)
logread
logread -f   # follow, как tail -f

# Логи dropbear
logread | grep dropbear

# hostapd events (подключения/отключения клиентов)
logread | grep hostapd
```

### Автозапуск произвольных демонов

Любой бинарник из прошивки или скачанный через `wget`/`opkg` можно запустить
при загрузке через crontab или init-скрипт в `/etc/init.d/`:

```sh
# Пример: tcpdump-дамп трафика в /data/
@reboot tcpdump -i br-lan -w /data/capture.pcap -C 10 -W 3 &

# Пример: ntpd с альтернативным сервером
@reboot ntpd -d -n -S /usr/share/zoneinfo/Europe/Moscow -p pool.ntp.org &
```

### UCI: полное управление конфигом

UCI (Unified Configuration Interface) — слой над конфигами в `/etc/config/`.
Изменения через UCI автоматически сохраняются в ubifs:

```sh
# Просмотр всей конфигурации
uci show

# Конкретная секция
uci show wireless
uci show network
uci show dhcp
uci show firewall

# Поиск
uci show | grep ssid

# Изменение
uci set wireless.@wifi-iface[0].disabled=1   # выключить 2.4GHz
uci commit wireless
wifi reload    # применить без полного рестарта (осторожно — см. баг ниже)
```

### Управление WiFi без полного рестарта

`wifi reload` и `hostapd_cli reload` меняют конфиг без разрыва
клиентских соединений. Но из-за бага `iface_mgr_setup` это работает
только для некоторых параметров. Для смены канала или SSID нужен
полный рестарт через последовательность из раздела выше.

---

## Полезные однострочники

```sh
# Узнать версию прошивки
cat /etc/openwrt_release

# Узнать модель и серийный номер
cat /proc/sys/kernel/hostname
nvram get SN

# Свободная RAM
free

# Использование /data (ubifs)
df -h /data

# Количество клиентов на каждом радио
iw dev wl0 station dump | grep -c Station
iw dev wl1 station dump | grep -c Station

# Кто передаёт больше всего
iw dev wl0 station dump | grep -E 'Station|tx bytes' | paste - -

# Форс-перезагрузка
reboot

# Сброс WiFi без перезагрузки роутера (c обходом бага iface_mgr_setup)
wifi down; sleep 2; kill $(pidof hostapd); rm -f /var/run/hostapd/global; \
  mkdir -p /var/run/hostapd; \
  hostapd -g /var/run/hostapd/global -B -P /var/run/hostapd-global.pid; \
  sleep 2; wifi up
```

---

## Ограничения

- **Нет opkg/пакетного менеджера** — он есть, но репозиторий Xiaomi не содержит
  большинства пакетов. Бинарники для IPQ5018/aarch64 можно собрать через
  OpenWRT buildroot или скопировать статически скомпилированные.
- **Нет overlayfs** — нельзя менять файлы в squashfs. Только `/data/` и `/etc/config/`.
- **Кастомная прошивка невозможна** — secure boot + подписанный bootloader.
  Альтернативный OpenWRT на RA74 пока не поддерживается.
- **OTA-обновления сбрасывают crontab и `/data/fix_ssh.sh`** —
  после обновления прошивки доступ теряется (или изменится фирменная логика).
  Рекомендуется отключить OTA: `uci set system.@system[0].auto_update=0 && uci commit system`.
