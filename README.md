# vmctl

`vmctl` создаёт и обслуживает виртуальные машины libvirt/KVM на отдельном Linux-хосте. Вы получаете команды для создания ВМ из ISO или cloud image, управления дисками, сетью и питанием, а также для проверок. NetBox подключается по желанию: с ним описание ВМ и требуемое состояние хранятся там; без ключа всё работает локально. Каждый хост независим.

1. [Установка на HOST](#1-установка-на-host)
2. [Краткое использование без NetBox](#2-краткое-использование-без-netbox)
3. [Краткое использование с NetBox](#3-краткое-использование-с-netbox)
4. [Справочник команд vmctl](#4-справочник-команд-vmctl)
5. [NetBox: данные и права](#5-netbox-данные-и-права)
6. [Как устроена виртуализация в Linux](#6-как-устроена-виртуализация-в-linux)
7. [Автоматическая установка Linux: план](#7-автоматическая-установка-linux-план)

## 1. Установка на HOST

Нужны Linux с KVM, Python 3.11+, работающий libvirt с соединением `qemu:///system`, `virsh`, `qemu-img`, место для дисков и настроенный сетевой мост. Для cloud image дополнительно нужен `xorriso`. Приведённые команды рассчитаны на `root`.

```bash
git clone https://github.com/mikmayorov/vmctl.git /opt/vmctl
cd /opt/vmctl
cp config.example.toml config.toml
chmod 600 config.toml
ln -s /opt/vmctl/vmctl /usr/local/bin/vmctl
```

В `config.toml` проверьте `host.libvirt_uri`, `storage.directory` и `network.bridge`. Каталог и мост должны существовать. Секция `[hardware.*]` задаёт предлагаемые размеры ВМ: `vcpus`, `memory_mb`, `disk_gb` (ГиБ = 2³⁰ байт). Имя профиля выбирается в файле запроса: `hardware = "small"`.

Для NetBox дополнительно укажите `netbox.url` и положите API-токен в `/opt/vmctl/netbox.key` с правами `600`. Также укажите имя заранее созданного устройства в `netbox.device`; если имена повторяются, добавьте `netbox.site`. Остальные подробности — в [разделе 5](#5-netbox-данные-и-права). Без ключа обращений к NetBox нет.

```bash
vmctl check                 # инструменты, libvirt, хранилище, мост, пулы и сети
vmctl list                  # список ВМ хоста
vmctl -h                    # краткая справка; vmctl КОМАНДА -h — параметры
```

Обновление: `cd /opt/vmctl && git pull --ff-only && vmctl check`. Файлы запросов ВМ удобно хранить в `local/`.

## 2. Краткое использование без NetBox

Режим действует, когда нет ключа в `config.toml`, `netbox.key` и `NETBOX_TOKEN`. `prepare`, `sync`, `adopt` и `audit` требуют NetBox.

### 2.1. Первый запуск, включая хост с существующими ВМ

```bash
vmctl check
vmctl list
vmctl check ИМЯ            # одна существующая ВМ
vmctl cat ИМЯ              # постоянный XML следующего запуска
vmctl cat ИМЯ --live       # XML работающей ВМ
```

Существующие ВМ уже есть в libvirt; импортировать их в отдельную локальную базу не нужно. `list` обновляет ссылки `/opt/vmctl/current-guest/ИМЯ` на постоянные XML. Если libvirt хранит XML не в `/etc/libvirt/qemu`, укажите `host.domain_xml_directory`.

### 2.2. Создание ВМ

```bash
cd /opt/vmctl
mkdir -p local
cp examples/ubuntu-iso.toml local/guest.toml
# Укажите имя, hardware, Description и путь к имеющемуся ISO.
vmctl --dry-run create local/guest.toml
vmctl create local/guest.toml
vmctl start ИМЯ
vmctl check ИМЯ
```

Для cloud image используйте `examples/ubuntu-cloud.toml`, путь к образу и собственный `user_data` с SSH-ключом. `create` определяет выключенную ВМ; запуск отдельный. Вместо `hardware` можно указать `vcpus`, `memory_mb`, `disk_gb` непосредственно в запросе. `vm.software` пока зарезервирован: каталога готовых дистрибутивов нет.

### 2.3. Изменение параметров запущенной ВМ

```bash
vmctl check ИМЯ
vmctl cat ИМЯ              # следующий запуск
vmctl cat ИМЯ --live       # сейчас в работе
vmctl shutdown ИМЯ
vmctl disk add ИМЯ data --size-gb 20
vmctl nic add ИМЯ backup --bridge br1
vmctl start ИМЯ
```

`disk` и `nic` требуют выключенную ВМ. Если редактировать постоянный XML через `virsh edit ИМЯ`, он вступит в силу после полного выключения и запуска. `vmctl reboot` для этого недостаточно. `shutdown` ждёт до 60 секунд; `shutdown --force` выключает немедленно с риском потери незаписанных данных.

### 2.4. Проверки

```bash
vmctl list                  # текущие параметры работающих, стартовые выключенных
vmctl check                 # здоровье хоста
vmctl check ИМЯ            # постоянный XML и живое состояние, если ВМ запущена
```

RAM и диск в `list` показаны в десятичных GB (1 GB = 10⁹ байт), округлены вверх до 0,001 GB. Без NetBox колонки IPv4/IPv6 остаются пустыми.

## 3. Краткое использование с NetBox

**NetBox — источник истины** для описания ВМ и требуемого состояния. Команды изменения сначала записывают NetBox, затем меняют libvirt. Если локальный шаг не удался, запись NetBox остаётся: исправьте причину и повторите `sync`. Фоновой синхронизации нет.

### 3.1. Первый запуск, включая хост с существующими ВМ

Администратор заранее создаёт кластер и Device хоста в NetBox, связывает их, выдаёт права API-пользователю и указывает `netbox.device` в `config.toml`.

```bash
vmctl check
vmctl audit                 # расхождения до импорта
vmctl --dry-run adopt       # план для всех локальных ВМ
vmctl adopt                 # локальные ВМ → NetBox
vmctl audit
```

`adopt ИМЯ` импортирует одну ВМ. Команда переносит CPU, память, UUID в Serial, Description, статус, автозапуск, диски и подключённые ISO как Virtual Disks с размером и полным путём, интерфейсы и MAC. Имя VM Interface приводится к стабильному имени TAP на хосте (`vm-...`); интерфейс и его назначенные IP сохраняются. Команда не меняет libvirt и не создаёт IP: их назначают вручную в NetBox IPAM. Уже заполненный Serial с другим UUID блокирует импорт. Для импортированной ВМ `sync` может применять поддерживаемые изменения, но не воссоздаёт утраченную локальную ВМ.

### 3.2. Создание ВМ

```bash
cp examples/ubuntu-iso.toml local/guest.toml
# Измените имя, hardware, путь ISO и Description.
vmctl --dry-run prepare local/guest.toml
vmctl prepare local/guest.toml
# В NetBox назначьте IP интерфейсу, настройте display.
vmctl --dry-run sync ИМЯ
vmctl sync ИМЯ
vmctl start ИМЯ
vmctl check ИМЯ
```

`prepare` создаёт только в NetBox ВМ, Virtual Disk, интерфейс и MAC. `sync` читает NetBox и создаёт локальную ВМ. Если пауза для настройки в NetBox не нужна, `vmctl create local/guest.toml` выполняет **prepare + первый sync** подряд. `create` не запускает ВМ. TOML-запрос после создания уже не является источником параметров.

### 3.3. Изменение параметров запущенной ВМ

Меняйте требуемые значения в NetBox согласно [таблице ниже](#где-менять-значения), затем:

```bash
vmctl --dry-run sync ИМЯ
vmctl sync ИМЯ              # обновить постоянный XML
vmctl check ИМЯ             # увидеть XML и прежнюю работающую конфигурацию
# В окно обслуживания:
vmctl shutdown ИМЯ
vmctl start ИМЯ             # применить постоянный XML
vmctl audit
```

`reboot` не применяет ожидающий XML. `sync` не увеличивает существующий qcow2 и не переносит файл диска. IP в NetBox документирует интерфейс, но сеть гостевой ОС настраивается отдельно. `disk add/remove`, `nic add/remove`, команды питания и автозапуска также сначала обновляют NetBox.

### 3.4. Проверки и направление обмена

```bash
vmctl list                  # локальное состояние + primary IPv4/IPv6 из NetBox
vmctl check ИМЯ            # NetBox → постоянный XML → работающая ВМ
vmctl audit                 # все ВМ: пропуски, отличия, ожидающий перезапуск
vmctl --dry-run sync        # план для всех ВМ устройства хоста
vmctl sync                  # NetBox → libvirt для всех ВМ устройства хоста
vmctl --dry-run adopt ИМЯ   # обратный импорт одной ВМ
```

`sync` переносит **NetBox → libvirt**, `adopt` переносит **наблюдаемое локальное состояние → NetBox** только по явному запросу, `audit` и `check` читают и сравнивают. Для работающей ВМ `check` отдельно показывает NetBox, XML следующего запуска и текущее состояние; отличия XML от памяти отмечаются `PENDING RESTART`. `audit` сообщает `MISSING NETBOX`, `MISSING LOCAL`, `DIFF` и `INVALID`. `sync` без имени продолжает обработку остальных ВМ после ошибки одной.

## 4. Справочник команд vmctl

Синтаксис: `vmctl [--config ФАЙЛ] [--dry-run] КОМАНДА ...`. `--config` выбирает другой файл конфигурации, `--dry-run` показывает план без записи. Точные аргументы: `vmctl КОМАНДА -h`, например `vmctl disk add -h`.

### Просмотр и сверка

#### `list`

`vmctl list` выводит локальные ВМ: питание, CPU, RAM, диски, автозапуск, host-интерфейс, IP из NetBox, URL дисплея и UUID. Работающая ВМ показывается по живым параметрам libvirt, выключенная — по стартовой конфигурации. Обновляет ссылки `current-guest/`.

#### `cat`

`vmctl cat guest` печатает постоянный XML; `vmctl cat guest --live` — XML работающей ВМ. XML может содержать пароль дисплея.

#### `check`

`vmctl check` проверяет зависимости, libvirt/NetBox, хранилище, мост, пулы и сети. `vmctl check guest` показывает таблицу из колонок NetBox, постоянные настройки XML/libvirt и работающая ВМ: UUID, CPU/RAM/HDD, `NIC guest:` с MAC гостя, `NIC host:` с именем TAP и его master-мостом, дисплей, питание, автозапуск, Description и носители. Питание читается из `virsh domstate` и показано как текущее состояние. Автозапуск читается из `virsh dominfo` и показан среди постоянных настроек libvirt: в XML домена этого поля нет. В колонке работающей ВМ master читается из Linux. Отличающиеся строки помечаются `!` и на терминале окрашиваются красным. Расхождение NetBox с локальными настройками даёт ненулевой код; отличия XML от памяти ожидают полного выключения и запуска. Пароль дисплея выводится в таблице.

#### `audit`

`vmctl audit` проверяет весь хост относительно NetBox без изменений; нужен API-ключ. При расхождении возвращает код `1`. После исправления записи: `vmctl --dry-run sync guest`, затем `vmctl sync guest`.

### Виртуальные машины

#### `prepare`, `create`

`vmctl prepare local/guest.toml` создаёт ВМ и начальные компоненты только в NetBox; затем задайте IP/дисплей и выполните `vmctl sync guest`. `vmctl create local/guest.toml` в режиме NetBox выполняет `prepare` и первый `sync` подряд, в локальном режиме создаёт ВМ только в libvirt. ВМ остаётся выключенной. Файл запроса указывает `hardware = "small"` либо собственные `vcpus`, `memory_mb`, `disk_gb`; вместе их указывать нельзя. `software` пока не принимается.

#### `sync`

`vmctl sync guest` применяет запись NetBox к одной ВМ, `vmctl sync` — ко всем ВМ устройства хоста. `vmctl --dry-run sync guest` показывает план. `vmctl sync guest --purge` также удаляет управляемые файлы дисков, исключённых из NetBox, если ВМ выключена; сначала проверьте план. Изменения XML работающей ВМ ждут полного выключения и запуска.

#### `adopt`

`vmctl adopt guest` переносит фактические параметры одной локальной ВМ в NetBox, `vmctl adopt` — всех. `vmctl --dry-run adopt` показывает план. Полный `adopt` обновляет также дисплей и остальные наблюдаемые параметры ВМ. Если требуется **только** привести имена существующих VM Interfaces к именам TAP, используйте `vmctl --dry-run adopt --interfaces-only`, затем `vmctl adopt --interfaces-only` (или добавьте имя ВМ): остальные поля NetBox сохраняются. Libvirt и IPAM команды не меняют. Device хоста и кластер должны уже существовать.

#### `delete`

`vmctl delete guest` удаляет выключенную ВМ из libvirt и NetBox, сохраняя файлы. `vmctl --dry-run delete guest --purge` показывает план с удалением управляемых файлов; `vmctl delete guest --purge` его выполняет. IP предварительно снимите с интерфейсов в NetBox. Запись ВМ удаляйте командой `vmctl delete`, чтобы действие можно было повторить после ошибки.

### Диски и интерфейсы

#### `disk add`, `disk remove`

`vmctl disk add guest data --size-gb 20` создаёт дополнительный диск 20 ГиБ. `vmctl disk remove guest data` отключает его, сохраняя файл; `--purge` удаляет управляемый файл. Загрузочный или последний диск удалить нельзя. ВМ должна быть выключена.

#### `nic add`, `nic remove`

`vmctl nic add guest backup` создаёт интерфейс и MAC на мосту по умолчанию. `vmctl nic add guest backup --bridge br1 --mac 52:54:00:12:34:56` задаёт мост и MAC. В режиме NetBox имя `backup` служит коротким аргументом команды, а запись VM Interface получает стабильное имя TAP `vm-ИМЯ-хеш`; его также видно через `ip link`/`vmctl list`. `vmctl nic remove guest backup` или `vmctl nic remove guest vm-ИМЯ-хеш` удаляет интерфейс и MAC при выключенной ВМ; сначала снимите назначенные IP в NetBox.

### Питание

#### `start`, `shutdown`, `reboot`

`vmctl start guest` запускает ВМ. `vmctl shutdown guest` просит гостя выключиться и ждёт до 60 секунд; `vmctl shutdown guest --force` выключает немедленно. `vmctl reboot guest` перезагружает гостя в его текущей конфигурации. Для применения нового постоянного XML нужны `shutdown` и `start`.

#### `autostart`, `autostart-off`

`vmctl autostart guest` включает запуск вместе с хостом; `vmctl autostart-off guest` выключает. При наличии NetBox команды меняют также `Start on boot` записи ВМ.

## 5. NetBox: данные и права

### Связь хоста с NetBox

Создайте кластер и Device хоста в NetBox, привяжите Device к кластеру. `vmctl` ищет Device по `netbox.device` (имя); при одинаковых именах укажите `netbox.site` (slug площадки), при необходимости `netbox.tenant`. ID и кластер в конфигурации не дублируются. Неоднозначный поиск даёт ошибку. У каждого хоста собственный API-токен. Его можно хранить в `/opt/vmctl/netbox.key` с правами `600`; `NETBOX_TOKEN` и `netbox.key` в конфигурации имеют приоритет над файлом.

### Где менять значения

| Значение | Место в NetBox | Действие `vmctl` |
| --- | --- | --- |
| Имя, Device/Cluster, статус, vCPU, память, Description | Поля Virtual Machine | `sync` применяет поддерживаемые значения, Description пишет в XML. Автоматического переноса/переименования ВМ нет. |
| UUID | `Serial` ВМ | Новая ВМ получает UUID до создания в libvirt; `adopt` переносит существующий. Несовпадающий Serial блокирует `sync`/`adopt`. |
| Автозапуск | `Start on boot` ВМ | Меняйте через `autostart`/`autostart-off`; обычный `sync` автозапуск не переключает. |
| Диски и ISO | `Virtual Disks`: `Size` в МиБ, полный путь в `Description` | `adopt` переносит оба вида носителей. Размер ISO входит в общий Disk ВМ. `sync` не меняет размер существующего qcow2 и пока не переподключает импортированные ISO. |
| Интерфейсы и MAC | `VM Interfaces` и Primary MAC | Имя созданного `vmctl` VM Interface совпадает с именем TAP на хосте (`vm-...`). Primary MAC — адрес **гостевого** адаптера; MAC TAP, видимый как `link/ether` на хосте, может отличаться. `sync` применяет значения к постоянному XML; работающей ВМ нужен следующий запуск. |
| IPv4/IPv6 | IPAM: адрес на интерфейсе, затем Primary IP ВМ при необходимости | Адрес назначает администратор вручную. `vmctl` читает и проверяет привязку; сеть гостя не настраивает. |
| Мост, источник установки, пути, дисплей | `local_context_data.vmctl` ВМ | `sync` строит постоянную конфигурацию. Для нового запроса значения по умолчанию берутся из `config.toml`. |

У одного подключения два MAC-адреса:

```text
guest NIC: 52:54:00:a8:0d:88  — адаптер внутри ВМ; этот MAC хранится в NetBox VM Interface
host NIC:  fe:54:00:a8:0d:88  — TAP на хосте; его показывает ip link show dev vm-...
```

libvirt задаёт TAP другой первый байт (`fe` вместо `52`), чтобы у двух сторон не было одинакового MAC и не нарушалась передача кадров. Имя TAP при этом совпадает с именем VM Interface в NetBox. `vmctl check ИМЯ` показывает MAC гостя в таблице и MAC хостового TAP отдельно. [Объяснение разработчика libvirt](https://lists.libvirt.org/archives/list/devel%40lists.libvirt.org/thread/IYBXRAMRWTCDKV23MJQPONKPLOFCBM5F/).

`local_context_data.vmctl.display` задаёт `type` (`vnc`, `spice`, `none`), `listen` (IP **хоста**), `port` (`"auto"` или 5900–65535), `password`. Пример: `{"type":"vnc","listen":"127.0.0.1","port":"auto"}`. На внешнем адресе пароль обязателен; VNC использует максимум 8 байт пароля. Для удалённого доступа удобно оставить loopback и открыть SSH-туннель. Пароль хранится в NetBox и XML libvirt открытым текстом; `check guest` тоже его показывает. Ограничивайте права чтения. Меняйте объект `vmctl.display`, сохраняя остальные поля `vmctl`; затем выполните `vmctl --dry-run sync guest` и `vmctl sync guest`. Работающей ВМ нужен следующий запуск.

Мост ВМ (`br1` и т. п.) хранится в `local_context_data.vmctl.bridge` или `interface_bridges`; это имя локального Linux-моста, к которому libvirt подключает TAP. Поле NetBox **Related Interfaces / Bridged Interface** связывает только интерфейсы внутри одной ВМ, поэтому оно не может ссылаться на `br1` в Device хоста. Для видимой в NetBox ссылки на интерфейс Device можно вручную создать необязательное объектное custom field на VM Interface с типом связанного объекта `dcim.interface`; текущий `vmctl` продолжает брать имя моста из `local_context_data.vmctl`. Для просмотра такого поля API-пользователю понадобится дополнительное право `dcim.interface: view` с ограничением `{"device__owner__users":"$user"}`. [Модель VM Interface](https://netboxlabs.com/docs/netbox/v4.2/models/virtualization/vminterface/), [объектные custom fields](https://netboxlabs.com/docs/netbox/customization/custom-fields/).

Статусы: `planned` — запись до создания на хосте; `staged` — ВМ определена, запуск не запрошен; `active` — должна работать; `offline` — должна быть выключена. `sync` применяет питание для `active`/`offline`. Если NetBox недоступен, управляемая операция останавливается до локального изменения. Если ошибка случилась после записи NetBox, проверьте `audit` и повторите `sync`.

### Групповые права API-пользователей

Пример для beer: отдельный пользователь `vmctl-beer`, отдельный API-токен v2 с **Write enabled**, общая группа `vmctl-hosts`, отдельный объект **Owner** для Device beer, включающий именно `vmctl-beer`. Для нового хоста создаются новый пользователь, токен и Owner, но используются те же правила группы. Схема Owner и `$user` требует NetBox 4.5 или новее. [Объектные разрешения NetBox](https://netboxlabs.com/docs/netbox/v4.4/administration/permissions/), [Owner](https://netboxlabs.com/blog/netbox-object-owner-functionality/).

В **Admin → Authentication → Permissions** назначьте группе `vmctl-hosts` следующие правила. В Constraints вводите JSON с двойными кавычками. Поля Users и Groups нельзя оставлять оба пустыми.

| Object Types | Действия | Constraints |
| --- | --- | --- |
| `dcim.device` | view | `{"owner__users":"$user"}` |
| `virtualization.virtualmachine` | view, add, change, delete | `{"device__owner__users":"$user"}` |
| `virtualization.virtualdisk`, `virtualization.vminterface` | view, add, change, delete | `{"virtual_machine__device__owner__users":"$user"}` |
| `dcim.macaddress` | view, add, change, delete | `{"vminterface__virtual_machine__device__owner__users":"$user"}` |
| `ipam.ipaddress` | view | `{"vminterface__virtual_machine__device__owner__users":"$user"}` |

Адресами управляет IPAM-администратор. Проверьте, что пользователь не получает широкие права через другую группу или прямое разрешение: NetBox объединяет подходящие правила. Owner должен содержать конкретного пользователя хоста, а не всю группу. После настройки проверьте `vmctl check`, создание/удаление компонентов тестовой ВМ и отсутствие доступа токена к ВМ другого хоста.

## 6. Как устроена виртуализация в Linux

Минимальная цепочка: **KVM в ядре** даёт аппаратное ускорение через `/dev/kvm`; **QEMU** запускает процесс ВМ и эмулирует устройства; **libvirt** хранит постоянное описание ВМ и управляет QEMU; **virsh** — простая командная оболочка libvirt; `vmctl` добавляет сценарии и, при необходимости, NetBox. Сетевой мост подключает TAP-интерфейс гостя к сети; qcow2/raw хранит данные. Графический менеджер не требуется. [QEMU: system emulation](https://www.qemu.org/docs/master/system/introduction.html), [libvirt: daemons](https://libvirt.org/daemons.html).

```bash
ls -l /dev/kvm
virsh -c qemu:///system list --all
virsh -c qemu:///system pool-list --all
virsh -c qemu:///system net-list --all
ip link show br0
ps -ef | grep '[q]emu-system'
systemctl status libvirtd              # монолитный вариант, если установлен
systemctl status virtqemud.socket       # модульный вариант с socket activation
```

В разных дистрибутивах libvirt работает через `libvirtd` либо модульные `virtqemud` и сокеты; проверяйте установленные unit-файлы. Обычно постоянные XML находятся в `/etc/libvirt/qemu/`, runtime-сокеты — в `/run/libvirt/`, диски — в настроенном каталоге (часто `/var/lib/libvirt/images/`), логи QEMU — в `/var/log/libvirt/qemu/`; пути зависят от дистрибутива. `virsh dumpxml guest` читает XML через libvirt, `virsh dumpxml --inactive guest` — постоянное описание. [Справочник virsh](https://www.libvirt.org/manpages/virsh.html).

Самый простой менеджер без графического интерфейса — `virsh`: с готовым XML выполните `virsh -c qemu:///system define guest.xml`, `virsh -c qemu:///system start guest`, `virsh -c qemu:///system domstate guest`, `virsh -c qemu:///system shutdown guest`. `define` регистрирует постоянную ВМ, `start` запускает QEMU. Если **libvirt вообще нет**, можно вызвать QEMU напрямую, но постоянной конфигурацией, автозапуском и учётом придётся управлять самим:

```bash
qemu-img create -f qcow2 /var/lib/libvirt/images/demo.qcow2 20G
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 \
  -drive file=/var/lib/libvirt/images/demo.qcow2,format=qcow2 \
  -cdrom /path/to/installer.iso -boot d -nic user,model=virtio \
  -vnc 127.0.0.1:0
```

Прямой QEMU работает, пока жив его процесс; VNC в примере слушает локальный порт 5900. С другой машины подключайтесь через SSH-туннель. Для регулярной эксплуатации используйте libvirt и `virsh`/`vmctl`.

## 7. Автоматическая установка Linux: план

Сейчас доступны **универсальные** запросы из локального ISO и готового cloud image с указанным `user_data`. Выбора дистрибутива и версии по имени, каталога подготовленных образов и автоматической финальной настройки гостя **пока нет**. Поле `vm.software` зарезервировано и отклоняется.

Планируемый процесс: подготовить проверенный образ каждой версии Linux, задать первоначальные параметры (пользователь, SSH-ключ, сеть, пакеты) через cloud-init либо автоматический установщик, затем после первого запуска проверить и завершить настройку гостя. Профиль «софт» будет ссылаться на такой образ и способ настройки; профиль «железо» уже выбирает CPU, память и начальный диск. Пока каталога образов нет, администратор сам указывает путь к ISO/cloud image и готовит свой `user_data` с SSH-ключом.
