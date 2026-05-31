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

### 4. 802.11r (быстрый роуминг) — через UCI

> **Важно (исправление прежней версии этого раздела).**
> Ранее тут рекомендовалось патчить `/var/run/hostapd-*.conf` и делать `kill -HUP`.
> На практике это **НЕ активирует FT**: после `HUP` (и после `hostapd_cli ... disable/enable`)
> hostapd продолжает отдавать обычный `WPA-PSK` — строки `ieee80211r/mobility_domain`
> попадают в файл, но в эфире Fast Transition не включается.
>
> Надёжно FT включается **только через UCI**. Генератор конфига собирает строки
> `r0kh`/`r1kh`/`nas_identifier`, но MAC для них берёт из **явных полей** `ap_macaddr`/`nasid`.
> Если их не задать — в конфиг попадает пустой `r0kh=  000102...` (без MAC), и hostapd
> **отвергает весь BSS** — точка вообще перестаёт выходить в эфир (только скрытый служебный SSID).

Для каждого радио-iface (`wireless.@wifi-iface[0]`, `[1]`):

```sh
for idx in 0 1; do
    dev=$(uci get wireless.@wifi-iface[$idx].device)     # wifi0 (2.4G) / wifi1 (5G)
    mac=$(uci get wireless.$dev.macaddr)                 # MAC этого радио
    nasid=$(echo "$mac" | tr -d :)
    uci set wireless.@wifi-iface[$idx].ieee80211r='1'
    uci set wireless.@wifi-iface[$idx].mobility_domain='1A2B'   # ОДИНАКОВЫЙ на всех AP
    uci set wireless.@wifi-iface[$idx].ft_over_ds='1'
    uci set wireless.@wifi-iface[$idx].nasid="$nasid"           # ключ к рабочему r0kh
    uci set wireless.@wifi-iface[$idx].ap_macaddr="$mac"        # ключ к рабочему r0kh
    uci set wireless.@wifi-iface[$idx].nasid2='000000000000'
    uci set wireless.@wifi-iface[$idx].ap2_macaddr='00:00:00:00:00:00'
    uci set wireless.@wifi-iface[$idx].ap2_r1_key_holder='00:00:00:00:00:00'
    uci set wireless.@wifi-iface[$idx].bss_transition='1'       # 802.11v BTM
    uci set wireless.@wifi-iface[$idx].rrm='1'                  # 802.11k
    uci set wireless.@wifi-iface[$idx].wnm='1'
done
uci commit wireless
reboot   # активируется штатной генерацией при загрузке; «тёплые» рестарты на этой прошивке нестабильны
```

SSID, пароль и `mobility_domain` должны **совпадать** на всех AP; каналы — **разные** и непересекающиеся.

**Проверка, что FT реально активен** (а не только лежит в файле):

```sh
cf=$(grep -h ^ctrl_interface /var/run/hostapd-wl0.conf | cut -d= -f2)
hostapd_cli -p "$cf" -i wl0 get_config | grep key_mgmt
# Должно быть:  key_mgmt=WPA-PSK FT-PSK      (а не просто  key_mgmt=WPA-PSK)
grep -m1 ^r0kh /var/run/hostapd-wl0.conf
# r0kh должен содержать MAC радио (НЕ пустой), напр.:
#   r0kh=aa:bb:cc:dd:ee:ff aabbccddeeff 000102030405060708090a0b0c0d0e0f
# если видите  r0kh=  000102...  (без MAC) — не заданы ap_macaddr/nasid в UCI
```

### Пример: три роутера с роумингом без пересечения каналов

| Роутер | 2.4 GHz | 5 GHz | Управление |
|--------|---------|-------|------------|
| AP-1 | ch 1 | ch 36 | 192.168.1.101 |
| AP-2 | ch 6 | ch 44 | 192.168.1.102 |
| AP-3 | ch 11 | ch 149 | 192.168.1.103 |

> Управляющие IP держите **вне DHCP-пула** маршрутизатора либо зарезервируйте их там
> static-lease по MAC точки — иначе DHCP может выдать тот же адрес другому клиенту
> (классический симптом: `ping` адреса то отвечает, то нет, ARP «прыгает» между MAC).

### 5. Ширина канала на 802.11ax (важно: `HE20`, а не `HT20`)

Радио здесь работают в режиме 11ax (`hwmode=11axg`/`11axa`). Генератор конфига берёт
ширину из `htmode`, но **для HE-режима ждёт значения `HE20`/`HE40`/`HE80`/`HE160`** —
не `HT20`/`HT40`. Если задать `htmode=HT20`, генератор всё равно впишет `ht_capab=…[HT40+]`,
и 2.4 ГГц останется на 40 МГц.

```sh
# Сузить 2.4 ГГц до 20 МГц (рекомендуется при 3 AP на каналах 1/6/11 —
# иначе HT40 перекрывает соседние каналы и точки мешают друг другу)
uci set wireless.wifi0.htmode='HE20'
uci set wireless.wifi0.bw='20'
uci commit wireless
reboot
# Проверка:  iw dev wl1 info | grep width   →  width: 20 MHz
```

> 5 ГГц: непересекающихся 80-МГц блоков без DFS всего два (UNII-1: 36–48 и UNII-3:
> 149–165). При трёх AP две нижние точки на 5 ГГц всё равно делят 36–48 — это нормально;
> DFS-каналы (52–64, 100+) прошивка при старте блокирует.

### 6. Тюнинг роуминга: что работает, а что нет

| Механизм | Статус на этой прошивке |
|----------|--------------------------|
| **802.11r (FT)** | ✅ работает — раздел 4 (UCI с `ap_macaddr`/`nasid`) |
| **802.11v BTM** (`bss_transition=1`) | ✅ работает — точка «подсказывает» клиенту уйти |
| **802.11k** (neighbor report) | ✅ работает, но не из UCI: генератор не выставляет `rrm_neighbor_report` → надо дописать в конфиг и перечитать BSS через `wpa_cli -g` (раздел 8) |
| **disassoc_low_ack / RSSI-kick** | ⚠️ UCI-опции не пробрасываются; `disassoc_low_ack` у hostapd включён по умолчанию, но порога по RSSI нет |
| **Band steering** (5↔2.4) | ❌ нативно нет; нужен демон (`dawn`/`usteer`), а его не поставить — корневая ФС занята на 100%, репозиториев под прошивку нет |

Практический вывод: роуминг на стоке держится на **802.11r + 802.11v** плюс грамотной
радиопланировке (разные каналы, HE20 на 2.4 ГГц, при необходимости — снижение мощности
для чётких границ сот). Полноценный `usteer`/`dawn` не поставить, но активный стиринг
«прилипших» клиентов реально собрать на штатных `iwinfo` + `bss_tm_req` — см. ниже.

### 7. Активный стиринг sticky-клиентов («mini-usteer» на штатных средствах)

**802.11v BTM работает** «из коробки»: `bss_tm_req` возвращает `OK` и сам несёт клиенту
кандидатов-соседей (802.11k можно включить дополнительно — раздел 8). RSSI клиента берётся из
`iwinfo <iface> assoclist` (hostapd отдаёт `signal=0`). Этого достаточно для watchdog,
который «подталкивает» далёкого клиента на ближнюю точку (решение принимает клиент;
с активным 802.11r переход бесшовный).

```sh
#!/bin/sh
# /data/roam_assist.sh — мягкий 802.11v BTM-стиринг. @reboot + keep-alive в fix_ssh.sh.
THRESH=-78; COOLDOWN=60; INTERVAL=15
C5=/var/run/hostapd-wifi1   # wl0 = 5 ГГц (ctrl-пути инвертированы!)
C2=/var/run/hostapd-wifi0   # wl1 = 2.4 ГГц
STATE=/tmp/roam_state; mkdir -p $STATE
# Кандидаты = ДРУГИЕ точки той же полосы: neighbor=<bssid>,<bssid_info>,<opclass>,<chan>,<phy>
# opclass: 2.4ГГц=81, 5ГГц UNII-1(36-48)=115, UNII-3(149-165)=125;  phy: HT=7, VHT=8
CAND5="neighbor=AA:AA:AA:AA:AA:A0,0,115,44,8 neighbor=BB:BB:BB:BB:BB:B0,0,125,149,8"
CAND2="neighbor=AA:AA:AA:AA:AA:A1,0,81,6,7 neighbor=BB:BB:BB:BB:BB:B1,0,81,11,7"
steer() { # ctrl iface mac cands
  [ -z "$4" ] && return
  now=$(date +%s); f=$STATE/$(echo $3|tr : _); last=$(cat $f 2>/dev/null||echo 0)
  [ $((now-last)) -lt $COOLDOWN ] && return
  hostapd_cli -p $1 -i $2 bss_tm_req $3 pref=1 $4 >/dev/null 2>&1   # disassoc_imminent=0 → мягко
  echo $now > $f; logger -t roam_assist "BTM nudge $3 on $2"
}
band() { # ctrl iface cands
  iwinfo $2 assoclist 2>/dev/null | awk '/^([0-9A-Fa-f]{2}:){5}/{mac=$1; for(i=2;i<=NF;i++) if($i ~ /^-[0-9]+$/ && $(i+1)=="dBm"){print mac,$i; break}}' | \
  while read mac sig; do [ -n "$sig" ] && [ "$sig" -le "$THRESH" ] && steer "$1" "$2" "$mac" "$3"; done
}
while :; do band "$C5" wl0 "$CAND5"; band "$C2" wl1 "$CAND2"; sleep $INTERVAL; done
```

Персистентность: `@reboot /data/roam_assist.sh &` в crontab + keep-alive в `/data/fix_ssh.sh`:

```sh
ps w | grep -v grep | grep -q roam_assist.sh || /data/roam_assist.sh >/dev/null 2>&1 &
```

> Подбирайте `THRESH` под объект: слишком высоко (напр. −70) → клиенты «пинг-понгуют»;
> слишком низко (−85) → держатся за дальнюю точку. −78 dBm — разумная отправная точка.
> Кандидатов лучше задавать той же полосы (5 ГГц → 5 ГГц соседи), чтобы не гонять с 5 на 2.4.

### 8. 802.11k (neighbor report) — через `wpa_cli` reload

Генератор не выставляет `rrm_neighbor_report`, а `/lib` — read-only squashfs (не
отредактировать). Поэтому 802.11k включается **пост-загрузочным патчем**: дописать
параметр в `/var/run` конфиг и перечитать BSS **родным механизмом прошивки** —
`wpa_cli -g /var/run/hostapd/global raw ADD/REMOVE bss_config=...` (именно так wifi-скрипт
добавляет BSS, см. `/lib/wifi/hostapd.sh`). `hostapd_cli -g` в этом билде не поддержан, а
`HUP`/`disable`/`enable` параметр не перечитывают — нужен именно `wpa_cli -g raw`.

```sh
#!/bin/sh
# /data/dot11k.sh — включить 802.11k + прописать список соседей.
# @reboot (sleep 20 ждёт поднятия wifi).  Немедленно: /data/dot11k.sh now
[ "$1" = now ] || sleep 20
G=/var/run/hostapd/global
SSIDHEX=$(echo -n 'MyNetwork' | hexdump -ve '1/1 "%02x"')   # ваш SSID в hex
for ifc in wl0 wl1; do
  conf=/var/run/hostapd-$ifc.conf; [ -f "$conf" ] || continue
  grep -q '^rrm_neighbor_report=1' "$conf" || printf 'rrm_neighbor_report=1\nrrm_beacon_report=1\n' >> "$conf"
  wpa_cli -g $G raw REMOVE $ifc >/dev/null 2>&1      # снять BSS (кратко уронит клиентов)
  sleep 1
  wpa_cli -g $G raw ADD bss_config=$ifc:$conf >/dev/null 2>&1   # поднять, читая патч
done
sleep 2
C5=/var/run/hostapd-wifi1   # wl0 (ctrl-пути инвертированы)
C2=/var/run/hostapd-wifi0   # wl1
# nr = <bssid_nocolon><bssid_info=00000000><opclass><chan><phy>
#   opclass: 2.4=51(81)  5G/36-48=73(115)  5G/149-165=7d(125);  phy: HT=07 VHT=08
add() { hostapd_cli -p $C5 -i wl0 set_neighbor $1 ssid=$SSIDHEX nr=$2 >/dev/null 2>&1
        hostapd_cli -p $C2 -i wl1 set_neighbor $1 ssid=$SSIDHEX nr=$2 >/dev/null 2>&1; }
add AA:AA:AA:AA:AA:A0 aaaaaaaaaaa000000000732408   # AP-1 5G ch36
add AA:AA:AA:AA:AA:A1 aaaaaaaaaaa100000000510107   # AP-1 2.4 ch1
add BB:BB:BB:BB:BB:B0 bbbbbbbbbbb000000000732c08   # AP-2 5G ch44
add CC:CC:CC:CC:CC:C0 ccccccccccc0000000007d9508   # AP-3 5G ch149
# … все BSSID всех точек обеих полос; self-записи безвредны (hostapd добавит свою сам)
logger -t dot11k '802.11k enabled'
```

Проверка: `hostapd_cli -p /var/run/hostapd-wifi1 -i wl0 show_neighbor` — должен вернуть
список (а не `FAIL`). Персист — `@reboot /data/dot11k.sh &` в crontab.

> **Цена:** на каждой загрузке точки `REMOVE/ADD` кратко (~1–2 c) роняет оба BSS —
> клиенты сразу возвращаются (с FT бесшовно). НЕ кладите запуск в ежеминутный watchdog
> `fix_ssh.sh` — только `@reboot`, иначе клиентов будет рвать каждую минуту.

---

## Эксплуатационная закалка (чтобы конфиг пережил недели)

### Отключить автообновление прошивки (OTA)

Прошивка по cron ежедневно дёргает `otapredownload` — апдейт может **снести root, SSH и
всю конфигурацию**. Для рутованного сетапа OTA нужно глушить:

```sh
crontab -l | grep -v otapredownload | crontab -
# Самовосстановление в /data/fix_ssh.sh (cron @reboot + ежеминутно):
crontab -l 2>/dev/null | grep -q otapredownload && crontab -l | grep -v otapredownload | crontab -
```

### Починить системное время (dumb AP сам не синхронизируется)

У точки в режиме dumb-AP нет шлюза и DNS (только статический IP), поэтому `ntpd` не
достучится до серверов, и часы «висят» на заводской дате (ломает TLS-валидацию и таймеры
ключей WPA, путает логи).

```sh
uci set system.@system[0].timezone='MSK-3'         # пример — Москва
uci set system.@system[0].zonename='Europe/Moscow'
uci -q delete system.ntp.server
uci add_list system.ntp.server='0.ru.pool.ntp.org'
uci add_list system.ntp.server='pool.ntp.org'
uci commit system

# Маршрут/DNS/таймзону держать в /data/fix_ssh.sh (ramfs /etc обнуляется при ребуте):
ip route | grep -q '^default' || ip route add default via 192.168.1.1
grep -q 'nameserver 192.168.1.1' /etc/resolv.conf || echo 'nameserver 192.168.1.1' > /etc/resolv.conf
echo 'MSK-3' > /etc/TZ

ntpd -q -n -p 192.168.1.1 -p 0.ru.pool.ntp.org     # разовая принудительная синхронизация
```

> `/etc` — ramfs (обнуляется при перезагрузке), поэтому `/etc/resolv.conf`, `/etc/TZ` и
> default-route нужно восстанавливать из `/data/fix_ssh.sh` (основной IP точки —
> 192.168.31.1, поэтому маршрут в persistent-конфиге не лежит).

## IGMP snooping (мультикаст: IPTV, DLNA, mDNS)

По умолчанию мост рассылает мультикаст во все порты и всем Wi-Fi клиентам (на низкой
скорости — «съедает» эфир). Snooping ограничивает рассылку только подписчиками.

- **Главное — на L2-магистрали (роутер-шлюз).** На MikroTik: `/interface bridge set
  bridge igmp-snooping=yes` (RouterOS 7.x поднимает и querier сам).
- На самой точке ядро snooping поддерживает; включается одной строкой (эффект — на
  локальных портах моста точки):

  ```sh
  echo 1 > /sys/devices/virtual/net/br-lan/bridge/multicast_snooping
  # персист (ramfs обнуляется): добавить эту же строку в /data/fix_ssh.sh
  ```
- **Без IGMP-querier на шлюзе snooping может «отрезать» мультикаст** (мост решит, что
  подписчиков нет). Включать согласованно: querier на шлюзе + snooping. На MikroTik
  `igmp-snooping=yes` поднимает querier сам — проверить: `/interface bridge print detail`
  (`querier=yes`). Link-local мультикаст (224.0.0.0/24, в т.ч. mDNS/Bonjour) snooping
  не трогает — он всегда флудится, так что AirPlay/Chromecast/HomeKit-дискавери не страдают.

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
