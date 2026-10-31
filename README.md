# vmctl

`vmctl` создаёт и обслуживает виртуальные машины libvirt/KVM на отдельном Linux-хосте. Вы получаете команды для создания ВМ из ISO или Cloud Images Ubuntu, Debian и Arch Linux, управления дисками, сетью и питанием, а также для проверок. NetBox подключается по желанию: с ним описание ВМ и требуемое состояние хранятся там; без ключа всё работает локально. Каждый хост независим.

1. [Установка на HOST](#1-установка-на-host)
2. [Краткое использование без NetBox](#2-краткое-использование-без-netbox)
3. [Краткое использование с NetBox](#3-краткое-использование-с-netbox)
4. [Справочник команд vmctl](#4-справочник-команд-vmctl)
5. [NetBox: данные и права](#5-netbox-данные-и-права)
6. [Как устроена виртуализация в Linux](#6-как-устроена-виртуализация-в-linux)
7. [Cloud Images и cloud-init](#7-cloud-images-и-cloud-init)

## 1. Установка на HOST

Нужны Linux с KVM, Python 3.11+, работающий libvirt с соединением `qemu:///system`, `virsh`, `qemu-img`, место для дисков и настроенный сетевой мост. Для `man vmctl` нужна утилита `man` и локаль UTF-8. Приведённые команды рассчитаны на `root`.

Для cloud images дополнительно нужны `virt-install`, `xorriso`, `ip` и одна из команд `xorrisofs`/`genisoimage`/`mkisofs`, а для загрузки — UEFI firmware (OVMF/EDK II). Пакеты устанавливаются **на хост**, независимо от выбранной гостевой ОС:

```bash
# Ubuntu / Debian:
apt install virtinst xorriso iproute2 ovmf
# Gentoo:
emerge --ask app-emulation/virt-manager dev-libs/libisoburn sys-apps/iproute2 sys-firmware/edk2
# Arch Linux:
pacman -S virt-install libisoburn iproute2 edk2-ovmf
```

В Gentoo `virt-install` входит в `app-emulation/virt-manager`; графический интерфейс для работы vmctl не требуется (пакет поддерживает USE `-gui`).

```bash
git clone https://github.com/mikmayorov/vmctl.git /opt/vmctl
cd /opt/vmctl
cp config.example.toml config.toml
chmod 600 config.toml
ln -s /opt/vmctl/vmctl /usr/local/bin/vmctl
mkdir -p /usr/local/share/man/man1
ln -s /opt/vmctl/man/man1/vmctl.1 /usr/local/share/man/man1/vmctl.1
```

В `config.toml` проверьте `host.libvirt_uri`, `storage.directory` и `network.bridge`. Каталог и мост должны существовать. Секция `[hardware.*]` задаёт предлагаемые размеры ВМ: `vcpus`, `memory_mb`, `disk_gb` (ГиБ = 2³⁰ байт). Имя профиля выбирается в файле запроса: `hardware = "small"`.

Для NetBox дополнительно укажите `netbox.url` и положите API-токен в `/opt/vmctl/netbox.key` с правами `600`. Также укажите имя заранее созданного устройства в `netbox.device`; если имена повторяются, добавьте `netbox.site`. Остальные подробности — в [разделе 5](#5-netbox-данные-и-права). Без ключа обращений к NetBox нет.

```bash
vmctl check                 # инструменты, libvirt, хранилище, мост, пулы и сети
vmctl list                  # список ВМ хоста
vmctl -h                    # краткая справка; vmctl КОМАНДА -h — параметры
man vmctl                   # полное руководство по командам на русском
```

Если терминал использует локаль `C`/`POSIX`, откройте страницу через `LANG=C.UTF-8 man vmctl`.

`check` проверяет команды как базового, так и cloud-сценария. Отсутствующие cloud-инструменты выводятся как `MISSING cloud images: ...` и дают ненулевой код, даже если libvirt и NetBox доступны. Создание из ISO не требует этих дополнительных инструментов. Выбор подходящей UEFI firmware проверяет libvirt/virt-install при создании XML; наличие всех команд само по себе не проверяет firmware и загрузку гостя.

Обновление: `cd /opt/vmctl && git pull --ff-only && vmctl check`. Ссылка на man-страницу при обновлении остаётся актуальной. Файлы запросов ВМ удобно хранить в `local/`.

## 2. Краткое использование без NetBox

Режим действует, когда нет ключа в `config.toml`, `netbox.key` и `NETBOX_TOKEN`. `prepare`, `sync`, `adopt`, `audit` и `delete netbox` требуют NetBox. `delete local` и `purge` всегда работают локально, даже при настроенном NetBox.

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

`create` определяет выключенную ВМ; запуск отдельный. Вместо `hardware` можно указать `vcpus`, `memory_mb`, `disk_gb` непосредственно в запросе. `vm.software` пока недоступен: каталога готовых дистрибутивов нет.

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

**NetBox — источник истины** для описания ВМ и требуемого состояния. `prepare` создаёт описание в NetBox; `create` читает его и развёртывает ВМ локально, после успеха отмечает `planned` как `staged`. Команды изменения параметров сначала записывают NetBox, затем меняют libvirt. Если изменение существующей ВМ не удалось, запись NetBox остаётся: исправьте причину и повторите `sync`. Если ещё не завершено первоначальное локальное создание, проверяйте и повторяйте `create`; `sync` его не заменяет. Фоновой синхронизации нет.

Удаление разделено по сущностям: `delete local` удаляет локальную ВМ с отложенным удалением дисков и не обращается к NetBox; `delete netbox` удаляет только запись NetBox; `purge` очищает отложенные локальные файлы.

### 3.1. Первый запуск, включая хост с существующими ВМ

Администратор заранее создаёт кластер и Device хоста в NetBox, связывает их, выдаёт права API-пользователю и указывает `netbox.device` в `config.toml`.

```bash
vmctl check
vmctl audit                 # расхождения до импорта
vmctl --dry-run adopt       # план для всех локальных ВМ
vmctl adopt                 # локальные ВМ → NetBox
vmctl audit
```

`adopt ИМЯ` импортирует одну ВМ. Команда переносит CPU, память, UUID в Serial, Description, статус, автозапуск, диски и подключённые ISO как Virtual Disks с размером и полным путём, интерфейсы и MAC. Для каждого VM Interface она записывает в Context имя TAP на хосте и мост. Существующие VM Interface, их имена и назначенные IP сохраняются. Если у ВМ по одному локальному NIC и VM Interface в NetBox, `adopt` сопоставит их даже при разных именах и ещё не назначенном Primary MAC. При нескольких неоднозначных интерфейсах задайте Primary MAC в NetBox для сопоставления; IP снимать не требуется. Команда не меняет libvirt и не создаёт IP: их назначают вручную в NetBox IPAM. Уже заполненный Serial с другим UUID блокирует импорт. Для импортированной ВМ `sync` может применять поддерживаемые изменения, но не воссоздаёт утраченную локальную ВМ.

При массовых `adopt` и `sync` ошибка одной ВМ выводится с её именем, после чего обработка продолжается. В конце показано число успешных ВМ и ошибок; при любой ошибке команда возвращает ненулевой код. Если недоступен сам NetBox или не найден Device хоста, обработка не начинается.

При ошибке vmctl печатает строку `Что сделать:` с конкретным следующим действием. Для существующей локальной ВМ проверьте запись и компоненты в NetBox, XML через `vmctl cat ИМЯ`, затем выполните `vmctl check ИМЯ` и пробный `vmctl --dry-run sync ИМЯ` или `vmctl --dry-run adopt ИМЯ`. Для новой ВМ, ещё не созданной локально, проверяйте `vmctl --dry-run create ИМЯ`; после частичной ошибки создания также проверьте оставшиеся файлы. Не запускайте полный `adopt` только для исправления одной настройки: он переносит в NetBox всё наблюдаемое состояние, включая дисплей.

### 3.2. Создание ВМ

```bash
cp examples/ubuntu-iso.toml local/guest.toml
# Измените имя, hardware, путь ISO и Description.
vmctl --dry-run prepare local/guest.toml
vmctl prepare local/guest.toml
# В NetBox назначьте IP интерфейсу, настройте display.
vmctl --dry-run create ИМЯ
vmctl create ИМЯ
vmctl start ИМЯ
vmctl check ИМЯ
```

Команды имеют отдельные задачи:

| Команда | Назначение |
| --- | --- |
| `prepare ФАЙЛ.toml` | Создать новую запись ВМ, Virtual Disk, интерфейс и MAC **только в NetBox**. |
| `create ИМЯ` | Прочитать подготовленную запись NetBox и **создать новую ВМ локально**: диски, XML и, для cloud image, cloud-init seed. Оставить ВМ выключенной. |
| `sync ИМЯ` | Перенести поддерживаемые изменения из NetBox в **уже существующую локальную ВМ**. |

**ТОЛЬКО `create` выполняет развёртывание cloud images.** `prepare` не создаёт локальных файлов, `create` не выполняет `prepare`, а `sync` не создаёт отсутствующую ВМ и не запускает развёртывание образа. Запуск гостя — отдельная команда `start`.

После `prepare` источником параметров становится NetBox, а не TOML-запрос. В режиме NetBox `create` принимает **имя**, а не файл. Без NetBox используется `create ФАЙЛ.toml`, как в разделе 2.

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

`reboot` не применяет ожидающий XML. `sync` не увеличивает существующий qcow2 и не переносит файл диска. Для Cloud Images начальная сеть гостя настраивается из NetBox через cloud-init; последующие изменения сети внутри гостя выполняются отдельно. `disk add/remove`, `nic add/remove`, команды питания и автозапуска также сначала обновляют NetBox.

`sync` можно выполнить при работающей ВМ: размер подключённого диска читается через libvirt (`virsh domblkinfo`), без повторного открытия занятого qcow2 через `qemu-img`. Для выключенных или ещё не подключённых файлов используется `qemu-img info`; при ошибке выводится её текст. Размер существующего диска по-прежнему проверяется, но не меняется.

Например, после изменения в NetBox с 4 vCPU / 8000 МиБ на 8 vCPU / 16000 МиБ успешный `sync` подготовит XML с новыми ресурсами. `check` покажет новые значения в NetBox и XML, прежние — в работающей ВМ, с отметкой ожидания перезапуска. Полное `shutdown` → `start` применит ресурсы; обычный `reboot` гостя для этого не подходит.

### 3.4. Проверки и направление обмена

```bash
vmctl list                  # локальное состояние + primary IPv4/IPv6 из NetBox
vmctl check ИМЯ            # NetBox → постоянный XML → работающая ВМ
vmctl audit                 # все ВМ: пропуски, отличия, ожидающий перезапуск
vmctl --dry-run sync        # план для всех ВМ устройства хоста
vmctl sync                  # NetBox → libvirt для всех ВМ устройства хоста
vmctl --dry-run adopt ИМЯ   # обратный импорт одной ВМ
```

`sync` переносит изменения **NetBox → существующая локальная ВМ**, `adopt` переносит **наблюдаемое локальное состояние → NetBox** только по явному запросу, `audit` и `check` читают и сравнивают. Для работающей ВМ `check` отдельно показывает NetBox, XML следующего запуска и текущее состояние; отличия XML от памяти отмечаются `PENDING RESTART`. `audit` сообщает `MISSING NETBOX`, `MISSING LOCAL`, `DIFF` и `INVALID`. Если локальной ВМ нет, `sync` сообщает об ошибке и предлагает `create ИМЯ` (для импортированной ВМ — восстановление). Массовый `sync` продолжает обработку остальных ВМ и возвращает ненулевой код при ошибках; запланированные записи автоматически не развёртываются.

### 3.5. Удаление ВМ и отложенная очистка дисков

```bash
vmctl shutdown ИМЯ
vmctl --dry-run delete local ИМЯ
vmctl delete local ИМЯ
# Запись NetBox, её интерфейсы и назначенные IP пока сохраняются.
# Для удаления записи снимите IP с VM Interfaces в NetBox IPAM:
vmctl --dry-run delete netbox ИМЯ
vmctl delete netbox ИМЯ
# Когда сохранённые диски больше не нужны, проверьте ВСЕ отложенные файлы:
vmctl --dry-run purge
vmctl purge
```

Каждая команда выполняет только свою операцию. После `delete local` ожидаемо остаётся `MISSING LOCAL` при аудите; после `delete netbox` при сохранённой локальной ВМ — `MISSING NETBOX`. Локальное удаление не требует снимать IP или иметь доступный NetBox. Команда `purge` не ограничивается последней удалённой ВМ: она очищает все подходящие отложенные файлы в текущем `storage.directory`.

## 4. Справочник команд vmctl

Синтаксис: `vmctl [--config ФАЙЛ] [--dry-run] КОМАНДА ...`. `--config` выбирает другой файл конфигурации, `--dry-run` показывает план без записи. Полная справка: `man vmctl`; точные аргументы: `vmctl КОМАНДА -h`, например `vmctl disk add -h`.

### Просмотр и сверка

#### `list`

`vmctl list` выводит локальные ВМ: питание, CPU, RAM, диски, автозапуск, host-интерфейс, IP из NetBox, URL дисплея и UUID. Работающая ВМ показывается по живым параметрам libvirt, выключенная — по стартовой конфигурации. Обновляет ссылки `current-guest/`.

#### `cat`

`vmctl cat guest` печатает постоянный XML; `vmctl cat guest --live` — XML работающей ВМ. XML может содержать пароль дисплея.

#### `check`

`vmctl check` проверяет зависимости, libvirt/NetBox, хранилище, мост, пулы и сети. `vmctl check guest` показывает таблицу из колонок NetBox, постоянные настройки XML/libvirt и работающая ВМ: UUID, CPU/RAM/HDD, `NIC guest` с MAC гостя, `NIC host` с именем TAP и его master-мостом, Primary IPv4/IPv6, дисплей, питание, автозапуск, Description и носители. IP-адреса читаются только из NetBox и с XML/памятью не сравниваются. Итог сравнения указан в жирном заголовке ВМ; пути XML, дисков и ISO идут сразу после таблицы. Питание читается из `virsh domstate` и показано как текущее состояние. Автозапуск читается из `virsh dominfo` и показан среди постоянных настроек libvirt: в XML домена этого поля нет. В колонке работающей ВМ master читается из Linux. Отличающиеся строки помечаются `!` и на терминале окрашиваются красным. Расхождение NetBox с локальными настройками даёт ненулевой код; отличия XML от памяти ожидают полного выключения и запуска. Пароль дисплея выводится в таблице.

#### `audit`

`vmctl audit` проверяет весь хост относительно NetBox без изменений; нужен API-ключ. При расхождении возвращает код `1`. После исправления записи: `vmctl --dry-run sync guest`, затем `vmctl sync guest`.

### Виртуальные машины

#### `prepare`

`vmctl prepare local/guest.toml` создаёт новую запланированную ВМ и начальные компоненты **только в NetBox**. Затем задайте IP, Contacts, Context и дисплей, выполните `vmctl create guest`. Для завершения подготовки после частичной ошибки можно повторить `prepare` для записи со статусом `planned`; развёрнутые записи он не переподготавливает. Файл запроса указывает `hardware = "small"` либо собственные `vcpus`, `memory_mb`, `disk_gb`; вместе их указывать нельзя. `software` пока не принимается.

#### `create`

С NetBox: `vmctl create guest` создаёт локальную ВМ из **существующей** записи NetBox. Сначала выполните `prepare`, затем настройте запись. `create` не создаёт запись или компоненты в NetBox; после успешного локального создания переводит `planned` в `staged`. Без NetBox: `vmctl create local/guest.toml` читает локальный TOML. В обоих режимах ВМ остаётся выключенной, даже если в NetBox указан `active`.

**ТОЛЬКО через `create` выполняется развёртывание cloud image**: клонирование образа в самостоятельный диск, подготовка cloud-init seed и определение нового домена. `vmctl --dry-run create guest` проверяет данные и показывает план. Если локальная ВМ уже есть, `create` останавливается без повторного развёртывания: для изменений используйте `sync`. Утраченную импортированную (`source = "existing"`) ВМ команда не пересоздаёт.

Перед развёртыванием `create` и `--dry-run create` выводят сводку: UUID, ресурсы, исходный hardware-профиль (если сохранён), образ/ISO, диски, кластер и Device, мосты/TAP/MAC всех интерфейсов, адреса с префиксами, шлюзы, DNS и search domains из сгенерированного seed. Для шлюзов/DNS показан источник итогового значения: уровень Context с именем поля либо маршруты хоста. Отсутствующие шлюзы/DNS явно помечаются как не заданные. Также показаны число ключей root, режим консольного доступа и настройки, отложенные до первого запуска. Сами ключи, пароли дисплея и содержимое произвольного пользовательского `user_data` не выводятся.

Имена видимых Config Contexts запрашиваются отдельно, в порядке `weight`/имя. Назначения на конкретный кластер проверяются отдельно от назначений на тип кластера; учитываются остальные доступные селекторы. Если для проверки не хватает метаданных (например, предков роли/региона либо типа кластера), контекст помечается как **не подтверждённый**, а не объявляется применённым. Список имён зависит от прав токена и может быть неполным. Применяемые значения всегда берутся из итогового `config_context` NetBox и локальных переопределений, а не пересобираются по диагностическому списку шаблонов.

Успешное завершение `create` подтверждает запись seed и определение выключенной ВМ. Оно не подтверждает применение настроек внутри гостя: это проверяется после `start` через консоль/SSH и `cloud-init status`. Новые `prepare` сохраняют имя исходного hardware-профиля для этой сводки; фактические ресурсы по-прежнему читаются из полей ВМ NetBox.

#### `sync`

`vmctl sync guest` применяет изменения NetBox к одной существующей локальной ВМ, `vmctl sync` — ко всем ВМ устройства хоста. Новые ВМ, системные диски из образов и cloud-init seed **не развёртывает**; при отсутствии локальной ВМ возвращает ошибку до изменений. `vmctl --dry-run sync guest` показывает план изменений. `vmctl sync guest --purge` также удаляет управляемые файлы дисков, исключённых из NetBox, если ВМ выключена; сначала проверьте план. Дополнительные диски могут создаваться пустыми при синхронизации компонентов; это не развёртывание ОС. Изменения XML работающей ВМ ждут полного выключения и запуска. Питание по статусам `active`/`offline` синхронизируется как прежде.

CPU/RAM записываются согласованно в максимальные и текущие стартовые значения XML (`memory`/`currentMemory`, `vcpu`/`vcpu current`). При явной CPU-топологии сохраняются sockets/dies/clusters/threads и подбирается число cores; если заданное число vCPU не укладывается в эту схему, команда просит скорректировать топологию. Горячее изменение CPU/RAM не выполняется.

#### `adopt`

`vmctl adopt guest` переносит фактические параметры одной локальной ВМ в NetBox, `vmctl adopt` — всех. `vmctl --dry-run adopt` показывает план. Полный `adopt` обновляет также дисплей и остальные наблюдаемые параметры ВМ. Он заполняет `vmctl.interfaces` и переводит старую запись Context на новую схему, сохраняя имена VM Interfaces и их IP. Если нужно **только** перевести сетевое описание на новую схему и сохранить дисплей, ресурсы и прочие поля NetBox, выполните `vmctl --dry-run adopt --interfaces-only`, затем `vmctl adopt --interfaces-only` (можно указать одну ВМ). Этот режим также заполняет пустой Description VM Interface данными о TAP и мосте; написанное вручную описание сохраняет. Libvirt и IPAM команды не меняют. Device хоста и кластер должны уже существовать.

#### `delete local`

`vmctl delete local guest` удаляет определение **выключенной локальной ВМ** из libvirt и переименовывает её рабочие диски в `storage.directory`, сохраняя их содержимое:

```text
guest.qcow2      → guest.qcow2.20261029T143012123456Z.purge
guest-data.qcow2 → guest-data.qcow2.20261029T143012123456Z.purge
```

Формат `<date>` — `YYYYMMDDTHHMMSSffffffZ`: UTC, включая микросекунды. Все диски одной операции получают одну метку времени. Диски из незавершённого удаления компонентов (`state/pending-purge/`) также переводятся в отложенные файлы. Запись NetBox, её статус, интерфейсы, IP и Contacts не меняются; API NetBox не используется.

UEFI NVRAM удаляется при снятии определения. Сгенерированные vmctl cloud-init seed и каталог `state/cloud-init/guest/`, сохранённый XML и локальное состояние операции очищаются. Исходный cloud image и установочный ISO не удаляются. Поддерживаются обычные файлы дисков непосредственно в `storage.directory`; диски другого типа/каталога и файлы, подключённые другой ВМ, блокируют операцию до снятия определения.

План: `vmctl --dry-run delete local guest`. Если операция прервалась после снятия определения или переименования части дисков, повторите **ту же** `delete local guest`: продолжение хранится в `state/local-delete/guest.json`, а уже переименованные файлы повторно не переименовываются. До завершения этого удаления `create` с тем же именем и очистка его отложенных дисков блокируются. После успешного удаления можно явно выполнить новый `create guest` из сохранённой записи NetBox; старые диски остаются отдельно до `purge`.

#### `delete netbox`

`vmctl delete netbox guest` удаляет **только запись ВМ в NetBox** и зависимые компоненты по правилам NetBox. Нужен API-ключ и права на эту ВМ; сначала снимите IP со всех VM Interfaces. Сами IP-объекты, Contacts и исходные образы не удаляются. Команда не обращается к libvirt, не выключает и не удаляет локальную ВМ, не переименовывает диски и не выполняет `delete local`. План: `vmctl --dry-run delete netbox guest`.

#### `purge`

`vmctl --dry-run purge` показывает список, `vmctl purge` окончательно удаляет **все** обычные файлы вида `*.<date>.purge` с корректной датой указанного выше формата непосредственно в `storage.directory`. Возраст файлов не учитывается. NetBox не используется. Обычные рабочие диски, файлы с произвольным суффиксом `.purge`, символические ссылки и файлы вне каталога не удаляются.

Перед очисткой проверяются постоянные и текущие XML всех локальных ВМ. Если отложенный файл подключён к ВМ или относится к незавершённому `delete local`, команда останавливается до удаления файлов. Для этой проверки нужен доступный libvirt. После частичной ошибки файловой очистки можно повторить `purge`.

Прежние формы `delete ИМЯ` и `delete ИМЯ --purge` больше не принимаются: сущность нужно выбрать явно. Параметр `--purge` у `disk remove`/`sync` относится к удалению компонентов и сохраняет прежнее поведение; он не заменяет отдельную команду `purge` для отложенных файлов ВМ.

### Диски и интерфейсы

#### `disk add`, `disk remove`

`vmctl disk add guest data --size-gb 20` создаёт дополнительный диск 20 ГиБ. `vmctl disk remove guest data` отключает его, сохраняя файл; `--purge` удаляет управляемый файл. Загрузочный или последний диск удалить нельзя. ВМ должна быть выключена.

#### `nic add`, `nic remove`

`vmctl nic add guest backup` создаёт VM Interface `backup` и MAC гостя на мосту по умолчанию; имя TAP и мост записывает в `vmctl.interfaces.backup`. `vmctl nic add guest backup --bridge br1 --mac 52:54:00:12:34:56` задаёт мост и MAC. `vmctl nic remove guest backup` удаляет интерфейс и MAC при выключенной ВМ; сначала снимите назначенные IP в NetBox.

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
| Интерфейсы и MAC | `VM Interfaces` и Primary MAC | Имя VM Interface обозначает гостевой NIC; для новой ВМ по умолчанию `eth0`. Primary MAC — адрес **гостевого** адаптера. Имя хостового TAP и мост хранятся отдельно в Context. `sync` применяет значения к постоянному XML; работающей ВМ нужен следующий запуск. |
| IPv4/IPv6 | IPAM: адрес на интерфейсе, затем Primary IP ВМ при необходимости | Адрес назначает администратор вручную. При создании Cloud Image все адреса интерфейсов передаются cloud-init; для существующего гостя сеть автоматически не меняется. |
| Мост, источник установки, пути, дисплей | `local_context_data.vmctl` ВМ | `sync` строит постоянную конфигурацию. Для нового запроса значения по умолчанию берутся из `config.toml`. |
| Шлюзы, DNS и search domains для первого запуска | `vmctl.cloud_init` в Context ВМ/хоста и общем Config Context кластера | Читаются при создании cloud image. Итоговый `config_context` приходит в ответе API ВМ/Device. |
| SSH-ключи root для первого запуска | Contacts с ролью `coreroot`, custom field `sshpublickey`; дополнительные ключи в Context ВМ | Читаются назначения Contacts на ВМ, Device и кластере, затем полные записи контактов. |
| Пароль root для консоли | `vmctl.cloud_init.root_password` в Context | Строка задаёт пароль; отсутствие поля или пустая строка выбирает пустой пароль. Применяется при первом запуске через cloud-init; SSH остаётся по ключам. |

У одного подключения два MAC-адреса:

```text
guest NIC: 52:54:00:a8:0d:88  — адаптер внутри ВМ; этот MAC хранится в NetBox VM Interface
host NIC:  fe:54:00:a8:0d:88  — TAP на хосте; его показывает ip link show dev vm-...
```

libvirt задаёт TAP другой первый байт (`fe` вместо `52`), чтобы у двух сторон не было одинакового MAC и не нарушалась передача кадров. MAC TAP не записывается в NetBox: он относится к хостовой реализации интерфейса. `vmctl check ИМЯ` показывает MAC гостя, имя TAP и его master-мост в таблице. [Объяснение разработчика libvirt](https://lists.libvirt.org/archives/list/devel%40lists.libvirt.org/thread/IYBXRAMRWTCDKV23MJQPONKPLOFCBM5F/).

`local_context_data.vmctl.display` задаёт `type` (`vnc`, `spice`, `none`), `listen` (IP **хоста**), `port` (`"auto"` или 5900–65535), `password`. Пример: `{"type":"vnc","listen":"127.0.0.1","port":"auto"}`. На внешнем адресе пароль обязателен; VNC использует максимум 8 байт пароля. Для удалённого доступа удобно оставить loopback и открыть SSH-туннель. Пароль хранится в NetBox и XML libvirt открытым текстом; `check guest` тоже его показывает. Ограничивайте права чтения. Меняйте объект `vmctl.display`, сохраняя остальные поля `vmctl`; затем выполните `vmctl --dry-run sync guest` и `vmctl sync guest`. Работающей ВМ нужен следующий запуск.

Каждый VM Interface новой схемы имеет запись в `local_context_data.vmctl.interfaces` по своему имени. Например: `{"eth0":{"host_dev":"vm-guest-a1b2c3","bridge":"br1"}}`. `host_dev` — постоянное имя TAP в XML libvirt, `bridge` — Linux-мост хоста. При создании интерфейса vmctl копирует эти значения в его Description: `Host interface: vm-guest-a1b2c3; bridge: br1`. Основные значения находятся в Context. При изменении Context `sync` применяет его к постоянному XML; текущая работающая ВМ изменится после полного выключения и запуска. Для старых записей версии 3 `vmctl` по-прежнему читает `interface_bridges`/`bridge`; полный `adopt` переводит их на новую схему. Имя `eth0` в NetBox — логическое обозначение NIC: само по себе оно не переименовывает интерфейс внутри гостевой ОС.

Отдельные Device Interfaces для TAP создавать не требуется. Поле NetBox **Related Interfaces / Bridged Interface** связывает только интерфейсы внутри одной ВМ, поэтому оно не может ссылаться на мост Device хоста. [Модель VM Interface](https://netboxlabs.com/docs/netbox/v4.5/models/virtualization/vminterface/).

Статусы: `planned` — запись до создания на хосте; `staged` — ВМ определена, запуск не запрошен; `active` — должна работать; `offline` — должна быть выключена. `create` всегда оставляет новую ВМ выключенной. `sync` применяет питание для `active`/`offline` только у существующей локальной ВМ. Если NetBox недоступен, управляемая операция останавливается до локального изменения. После ошибки изменения существующей ВМ проверьте `audit` и повторите `sync`; после ошибки первоначального создания проверяйте `create` и оставшиеся файлы.

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

Для создания из cloud image добавьте **два отдельных правила** чтения:

| Object Types | Действия | Constraints |
| --- | --- | --- |
| `tenancy.contactassignment` | view | `{"role__slug":"coreroot"}` |
| `tenancy.contact` | view | `{"assignments__role__slug":"coreroot"}` |

Это общие правила группы: они разрешают читать назначения с ролью `coreroot` и полные записи участвующих в них контактов, **в том числе назначенных другим хостам**. Ограничения Owner в основной таблице продолжают действовать на операции с ВМ, но не ограничивают эти два правила. Полная запись Contact включает не только публичный ключ, но и остальные поля контакта. vmctl запрашивает назначения только для выбранной ВМ, её Device и кластера; это фильтрация клиента, а не граница прав токена.

Если контакты должны быть изолированы между хостами, вместо этих двух групповых правил назначьте отдельному пользователю хоста `view` на конкретные записи: для `tenancy.contactassignment` — `{"pk__in":[101,102,103]}`, для `tenancy.contact` — `{"pk__in":[11,12]}`. Замените числа ID нужных назначений и контактов всех трёх уровней; обновляйте список при добавлении назначений. Общее групповое правило при этом должно отсутствовать: разрешения NetBox складываются. У `ContactAssignment.object` полиморфная связь (`object_type` + `object_id`), поэтому ограничение вроде `object__device__owner__users` здесь не работает; в отличие от VM Interface, прямого пути к Device у назначения нет.

Адресами управляет IPAM-администратор. Проверьте, что пользователь не получает широкие права через другую группу или прямое разрешение: NetBox объединяет подходящие правила. Owner должен содержать конкретного пользователя хоста, а не всю группу. После настройки проверьте `vmctl check`, создание/удаление компонентов тестовой ВМ и отсутствие доступа токена к ВМ другого хоста.

### Какие дополнительные данные читает cloud-init

Список ниже соответствует запросам текущего vmctl. Дополнительные данные для cloud-init читаются **только через `create`** при локальном развёртывании cloud-ВМ, в том числе при `--dry-run create`. `sync` существующей ВМ не собирает ключи Contacts и не генерирует seed.

| Данные | Запрос относительно `/api/` | Необходимое право |
| --- | --- | --- |
| Context ВМ, включая общие настройки кластера | `GET virtualization/virtual-machines/<vm_id>/` | `view` нужной `virtualization.virtualmachine`, уже есть в основной таблице |
| Context гипервизора | `GET dcim/devices/<device_id>/` | `view` своего `dcim.device`, уже есть в основной таблице |
| IP с префиксами каждого гостевого NIC | `GET ipam/ip-addresses/?assigned_object_type=virtualization.vminterface&assigned_object_id=<interface_id>` | `view` адресов этой ВМ, уже есть в основной таблице |
| Назначения Contacts ВМ | `GET tenancy/contact-assignments/?object_type=virtualization.virtualmachine&object_id=<vm_id>` | `view` соответствующих `tenancy.contactassignment` |
| Назначения Contacts хоста | `GET tenancy/contact-assignments/?object_type=dcim.device&object_id=<device_id>` | То же, включая назначения на Device |
| Назначения Contacts кластера | `GET tenancy/contact-assignments/?object_type=virtualization.cluster&object_id=<cluster_id>` | То же, включая назначения на Cluster |
| Имя Contact и `custom_fields.sshpublickey` | `GET tenancy/contacts/<contact_id>/` | `view` каждого используемого `tenancy.contact` |
| Имена Config Contexts для диагностической сводки | `GET extras/config-contexts/?is_active=true` | Необязательное `view extras.configcontext`; отсутствие доступа не блокирует создание |

Отдельные разрешения `view` на `tenancy.contactrole`, `tenancy.contactgroup`, `extras.customfield` и `virtualization.cluster` **для этих запросов не нужны**. Роль со slug приходит вложенной в ContactAssignment, значение custom field — в Contact, а итоговый Context — в ВМ/Device. Для применения итогового Context право `extras.view_configcontext` также не требуется; оно используется только дополнительным запросом для отображения имён в сводке. Можно ограничить это правило конкретными контекстами, например `{"pk__in":[11,12]}` с их реальными ID. При отсутствии права/API-доступа сводка сообщит, что имена недоступны, и создание продолжится с уже прочитанными значениями. vmctl не обращается к API справочников ролей/групп, определениям custom fields или detail кластера. Право создавать/редактировать эти объекты API-пользователю vmctl не требуется: данные заранее готовит администратор. Для настройки их через UI администратору нужны собственные соответствующие права.

При подготовке данных проверьте: Contact Role имеет **slug** `coreroot`; custom field с техническим именем **`sshpublickey`** назначен модели Contact и содержит публичный ключ. Общий Config Context должен быть активен и назначен нужному кластеру. Для настроек vmctl читает уже объединённый `config_context`; дополнительная проверка назначений используется только для диагностического списка имён.

Схема сверена с моделями и сериализаторами NetBox 4.5: [Contact и ContactAssignment](https://github.com/netbox-community/netbox/blob/v4.5.0/netbox/tenancy/models/contacts.py), [вложенные Contact/Role в API](https://github.com/netbox-community/netbox/blob/v4.5.0/netbox/tenancy/api/serializers_/contacts.py), [сборка Config Context](https://github.com/netbox-community/netbox/blob/v4.5.0/netbox/extras/models/configs.py). Для установленного NetBox окончательная проверка выполняется под токеном хоста.

### Проверка чтения под токеном хоста

`vmctl check` проверяет доступ к Device и списку ВМ, но не проверяет назначения Contacts и их ключи. Для подготовленной cloud-ВМ, которой ещё нет в libvirt, выполните:

```bash
vmctl --dry-run create ИМЯ
```

Для отдельной проверки API, в том числе уже существующей ВМ, запустите из `/opt/vmctl` следующий пример. Он использует те же `config.toml`, `netbox.key`/`NETBOX_TOKEN`, что и vmctl, не меняет NetBox/libvirt и не печатает ключи или содержимое Context. Замените `ИМЯ` именем ВМ. Если используется другой файл конфигурации, замените `DEFAULT_CONFIG` в вызове `load_config` на `Path("/путь/config.toml")` и импортируйте `Path` из `pathlib`.

```bash
python3 - ИМЯ <<'PY'
import sys
import urllib.parse
from vmctl import DEFAULT_CONFIG, load_config
from netbox import (
    _list_results, _request, find_device, get_vm, root_ssh_authorized_keys,
)

config = load_config(DEFAULT_CONFIG)
device_id, cluster_id = find_device(config)
vm = get_vm(config, sys.argv[1], cluster_id, device_id)
host = _request(config, "GET", f"dcim/devices/{device_id}/")
for label, record in (("VM", vm), ("Device", host)):
    fields = [name for name in ("config_context", "local_context_data") if name in record]
    print(f"{label}: доступны поля {', '.join(fields) or 'Context отсутствует'}")

for object_type, object_id in (
    ("virtualization.virtualmachine", vm["id"]),
    ("dcim.device", device_id),
    ("virtualization.cluster", cluster_id),
):
    query = urllib.parse.urlencode({"object_type": object_type, "object_id": object_id})
    assignments = _list_results(config, f"tenancy/contact-assignments/?{query}")
    contacts = [item["contact"] for item in assignments
                if (item.get("role") or {}).get("slug") == "coreroot"]
    print(f"{object_type} #{object_id}: видно назначений coreroot: {len(contacts)}")
    for item in contacts:
        contact = _request(config, "GET", f"tenancy/contacts/{item['id']}/")
        value = (contact.get("custom_fields") or {}).get("sshpublickey")
        status = "заполнен" if value else "пуст или отсутствует"
        print(f"  Contact #{contact['id']} {contact['name']}: sshpublickey {status}")

keys = root_ssh_authorized_keys(config, vm)
print(f"Всего уникальных ключей root с учётом Context: {len(keys)}")
if not keys:
    raise SystemExit("Нет ключей root: проверьте назначения, права и Context ВМ")
PY
```

Сверьте количество и имена видимых контактов на **каждом из трёх уровней** со списком, который видит администратор. При ограничении объектов NetBox может вернуть `200` с пустым или неполным списком назначений/IP, а не `403`. Успешное чтение одного Contact или наличие ключа из Context не доказывает, что видны все ожидаемые `coreroot`. `403` означает недостаточное право на endpoint; `404` при чтении известного Contact может означать, что объект скрыт ограничениями. После исправления прав повторите пробный `create` новой ВМ. Для уже созданной ВМ используйте отдельную проверку API выше: `create` её не пересоздаст, а `sync` не обновит гостевой `authorized_keys` автоматически.

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

## 7. Cloud Images и cloud-init

Для **Ubuntu, Debian и Arch Linux** используется единый режим `source = "cloud_image"`. **Развёртывание cloud images выполняется ТОЛЬКО командой `create`**, после создания записи через `prepare` и настройки NetBox. `sync` служит независимой синхронизацией изменений уже существующей локальной ВМ и не заменяет `create`.

Дистрибутив выбирается самим образом в поле `image`; отдельный параметр ОС не нужен. Cloud-init использует настройки дистрибутива и сетевого backend из образа. vmctl не задаёт `system_info.distro`, не устанавливает пакеты Ubuntu в другие ОС и передаёт общий сетевой формат cloud-init v2 (статические адреса, MAC, маршруты и DNS), который cloud-init преобразует в конфигурацию доступного backend.

Исходный образ хранится, например, в `/var/lib/libvirt/images-ro`. vmctl создаёт самостоятельный qcow2 через `qemu-img convert`, увеличивает его до размера из `hardware` и подключает NoCloud seed. Исходный образ остаётся шаблоном. Выбор по `vm.software` пока недоступен: укажите полный путь `image`.

| Гостевая ОС | Образ с установленным cloud-init | Полный пример запроса |
| --- | --- | --- |
| Ubuntu Server 26.04 | [`ubuntu-26.04-server-cloudimg-amd64v3.img`](https://cloud-images.ubuntu.com/releases/resolute/release/) | [`examples/ubuntu-cloud.toml`](examples/ubuntu-cloud.toml) |
| Debian 13 (trixie) | [`debian-13-genericcloud-amd64.qcow2`](https://cloud.debian.org/images/cloud/trixie/latest/) или `generic-amd64` | [`examples/debian-cloud.toml`](examples/debian-cloud.toml) |
| Arch Linux (rolling) | [`Arch-Linux-x86_64-cloudimg.qcow2`](https://fastly.mirror.pkgbuild.com/images/latest/) | [`examples/arch-cloud.toml`](examples/arch-cloud.toml) |

У Debian образ `nocloud` **не запускает cloud-init**; для этого сценария используйте `generic`/`genericcloud`. Имя Debian-образа `nocloud` не означает поддержку datasource NoCloud. У Arch нужен вариант `cloudimg` из официального проекта [arch-boxes](https://wiki.archlinux.org/title/Arch_Linux_on_a_VPS#Official_Arch_Linux_cloud_image), а не `basic` или установочный ISO. Имена `latest` обновляются; путь `image` можно направить на сохранённую конкретную сборку.

На хосте нужны `virt-install`, `xorriso`, одна из `xorrisofs`/`genisoimage`/`mkisofs`, `ip`, `qemu-img`, libvirt/KVM и UEFI firmware (OVMF). Например, на Ubuntu/Debian: `apt install virtinst xorriso ovmf`. Дистрибутив хоста не обязан совпадать с гостем. Используются CPU `host-passthrough` и машина q35 с UEFI **без Secure Boot**, чтобы поддержать в том числе загрузчик Arch. Отключение Secure Boot задаётся явно при выборе firmware. Для выбранного Ubuntu-образа `amd64v3` процессор хоста должен поддерживать x86-64-v3.

```bash
cp examples/ubuntu-cloud.toml local/guest.toml
# Измените имя, hardware и путь image.
vmctl prepare local/guest.toml
# В NetBox назначьте статические IPv4/IPv6 с префиксами всем VM Interfaces.
# Назначьте Contacts coreroot и при необходимости дополните Context.
vmctl --dry-run create example-ubuntu
vmctl create example-ubuntu
vmctl start example-ubuntu
```

`virt-install --cloud-init` принимает пути к `user-data`, `meta-data` и `network-config` и создаёт временный NoCloud ISO. vmctl использует `--print-xml=1`, чтобы получить XML первого запуска без запуска ВМ. Поскольку virt-install удаляет временные файлы при завершении, vmctl также создаёт постоянный seed `<storage.directory>/<имя>-seed.iso` и заменяет его путь в XML. XML определяется через `virsh define`; запуск остаётся отдельной командой. Сетевые адаптеры подключаются к мостам из `vmctl.interfaces`, а не к NAT-сети libvirt `default`.

Cloud-init в перечисленных образах уже установлен. При первом запуске он задаёт hostname, записывает ключи root, запрещает SSH-аутентификацию паролем и расширяет корневую файловую систему. Доступ во всех трёх ОС настраивается для **root**, независимо от стандартных cloud-пользователей `ubuntu`, `debian` или `arch`. Файлы входных данных сохраняются в `state/cloud-init/<имя>/`. `sync` существующей ВМ не пересоздаёт seed и не обновляет ключи или сеть внутри гостя: это первоначальная настройка. При ошибке после создания диска vmctl не перезаписывает его при повторе; проверьте оставшиеся диск, seed и каталог состояния перед повторным созданием.

В локальном режиме можно передать свой абсолютный путь `user_data` в запросе; в нём нужно самостоятельно описать доступ и статическую сеть. Автоматическое получение ключей и адресов работает в режиме NetBox.

### Резервный вход через консоль

Все параметры первичной настройки гостя сгруппированы в **`vmctl.cloud_init`**: пароль root, дополнительные SSH-ключи, DNS, search domains и шлюзы интерфейсов. Параметры создания домена и хранилища (`version`, `source`, `image`, `interfaces`, `storage_directory`, `display`) остаются на уровне `vmctl`; IP с префиксами по-прежнему берутся из NetBox IPAM.

Пароль root для консольного входа задаётся строковым полем **`vmctl.cloud_init.root_password`**. Добавьте поле к существующему Context, сохраняя остальные данные:

```json
{
  "vmctl": {
    "cloud_init": {
      "root_password": "ЗАМЕНИТЕ_СВОИМ_ПАРОЛЕМ"
    }
  }
}
```

Непустая строка передаётся в стандартный модуль cloud-init `chpasswd` как текстовый пароль root (`type: text`, `expire: false`), без включения пароля в shell-команды. **Отсутствующее поле или `"root_password": ""` означает пустой пароль**: в этом случае cloud-init выполняет `runcmd: [["passwd", "-d", "root"]]` на финальном этапе первого запуска. `null`, числа, логические значения, переводы строк и NUL не принимаются.

Если пароль был записан непосредственно в `vmctl.root_password`, перенесите поле внутрь `cloud_init` и удалите старое расположение. `create` явно сообщит о старом поле, чтобы вместо заданного пароля не применился пустой по умолчанию.

Наследование такое же, как у остальных начальных настроек: итоговый Context ВМ/кластера → Context Device → локальный Context ВМ. Явная пустая строка на уровне ВМ переопределяет унаследованный пароль и выбирает вход без пароля. В сводке `create` показаны только режим и источник политики; само значение скрыто.

Подключитесь на хосте:

```bash
virsh -c qemu:///system console ИМЯ
# В приглашении login: введите root. Выход из консоли: Ctrl + ].
```

SSH-парольная аутентификация остаётся отключённой (`ssh_pwauth: false`); для SSH используются ключи из NetBox. Пароль устанавливается или удаляется один раз при первоначальной настройке, а не при каждом запуске ВМ. Пока cloud-init не выполнил выбранную настройку, root остаётся с заблокированным паролем; если первый запуск не дошёл до этого шага, для входа потребуется восстановительный shell.

Пароль хранится открытым текстом в Context NetBox и в сгенерированном `user-data`/seed. Файлы cloud-init и seed создаются с правами `600`, каталог состояния — `700`; для чтения seed гостем используется стандартное управление владельцем файлов libvirt. Сводка `create` не выводит пароли и ключи.

Уже созданным ВМ обновление vmctl или `sync` пароль не меняет. Войдите в такую ВМ под root и выполните `passwd root` для установки пароля либо `passwd -d root` для пустого пароля. Проверка: `passwd -S root` показывает `P` (пароль задан), `NP` (пустой пароль) или `L` (заблокирован). При собственном локальном `user_data` добавьте нужную настройку самостоятельно.

### Статическая сеть

Все интерфейсы, их Primary MAC и назначенные IP с префиксами читаются из NetBox. В cloud-init используется сопоставление по MAC, независимо от имени NIC внутри гостевой ОС. DHCP и принятие IPv6 RA отключены.

Настройки `vmctl.cloud_init` объединяются в порядке: итоговый Config Context ВМ (включая назначенные кластеру контексты) → Context Device хоста → локальный Context ВМ. Кластер не имеет отдельного поля Context: общие параметры задайте через Config Context с назначением кластеру.

```json
{
  "vmctl": {
    "cloud_init": {
      "dns_servers": ["192.0.2.53"],
      "search_domains": ["example.org"],
      "network": {
        "eth0": {"gateway4": "192.0.2.1", "gateway6": null}
      }
    }
  }
}
```

Ключи `network` — имена VM Interfaces; также можно задать общие параметры по имени моста, например `br0`. Настройка по имени интерфейса имеет приоритет. DNS и search domains можно переопределить внутри интерфейса. Если шлюз не задан явно, vmctl ищет однозначный default route хоста через соответствующий мост и проверяет принадлежность next-hop подсети гостя (IPv6 link-local также разрешён). Адрес самого моста шлюзом автоматически не считается. При отсутствии подходящего маршрута интерфейс создаётся без default route; неоднозначные шлюзы отклоняются. `gateway4: null` / `gateway6: null` явно отключает поиск шлюза на интерфейсе. Поддерживается один default route каждого семейства на ВМ.

### SSH-ключи для cloud-init

При создании cloud image читаются назначения Contacts с ролью (slug) `coreroot` для самой ВМ, Device гипервизора и кластера. Публичные ключи находятся в custom field контакта с техническим именем `sshpublickey` (несколько ключей можно записать отдельными строками). Комментарий каждого такого ключа заменяется именем контакта. Ключи записываются root; для создания требуется хотя бы один ключ.

Дополнительные ключи задаются списком `vmctl.cloud_init.root_ssh_authorized_keys` в Context ВМ. Они сохраняются без изменения. Одинаковые ключи сравниваются по типу и ключевым данным; вариант из Context имеет приоритет над вариантом из Contacts. Сборщик также читает итоговый `config_context`, включая общие Config Context, назначенные кластеру; локальное значение списка ВМ имеет приоритет.

Для этого API-пользователю нужны права чтения `tenancy.contactassignment` и `tenancy.contact` с доступом к соответствующим назначениям всех трёх уровней и контактам. Отдельное право `tenancy.contactrole` не требуется. Точные Constraints и проверка токеном хоста приведены в [разделе 5](#групповые-права-api-пользователей).
