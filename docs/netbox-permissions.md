# Права NetBox для vmctl

Для каждого хоста создавайте отдельного пользователя NetBox, отдельную группу и отдельный API-токен. Группа задаёт права на **одно устройство хоста и ВМ, закреплённые за ним**. Одну группу с неограниченными правами для всех хостов использовать нельзя: любой её участник получит доступ ко всем объектам указанных типов. Токен действует с правами пользователя, а не задаёт собственные ограничения на объекты. [Модель прав NetBox](https://netboxlabs.com/docs/netbox/v4.4/administration/permissions/), [модель токена](https://netboxlabs.com/docs/netbox/models/users/token/).

## Подготовка

1. Администратор NetBox создаёт кластер и устройство хоста, затем назначает устройство хостом этого кластера. `vmctl` сам их не создаёт.
2. Запишите точное имя устройства и slug площадки. Ниже они обозначены `HOST_NAME` и `SITE_SLUG`. Используйте те же значения в `netbox.device` и `netbox.site` файла `config.toml` на хосте.
3. Создайте обычного пользователя, например `vmctl-HOST_NAME`. Не включайте `Staff` и `Superuser`. Создайте группу `vmctl-HOST_NAME` и добавьте в неё только этого пользователя.

В разделе **Admin → Authentication → Permissions** создайте для группы шесть объектных разрешений. Названия разделов UI могут отличаться между версиями NetBox. В каждой строке ниже выберите **ровно один тип объекта**, указанные действия и JSON в поле **Constraints**. Подставьте имя и площадку своего хоста; кавычки и двойные подчёркивания оставьте как в примере.

| Тип объекта NetBox | Действия | Constraints |
| --- | --- | --- |
| `DCIM → Device` | `view` | `{"name":"HOST_NAME","site__slug":"SITE_SLUG"}` |
| `Virtualization → Virtual machine` | `view`, `add`, `change` | `{"device__name":"HOST_NAME","device__site__slug":"SITE_SLUG"}` |
| `Virtualization → Virtual disk` | `view`, `add` | `{"virtual_machine__device__name":"HOST_NAME","virtual_machine__device__site__slug":"SITE_SLUG"}` |
| `Virtualization → VM interface` | `view`, `add`, `change` | `{"virtual_machine__device__name":"HOST_NAME","virtual_machine__device__site__slug":"SITE_SLUG"}` |
| `DCIM → MAC address` | `view`, `add` | `{"vminterface__virtual_machine__device__name":"HOST_NAME","vminterface__virtual_machine__device__site__slug":"SITE_SLUG"}` |
| `IPAM → IP address` | `view` | `{"vminterface__virtual_machine__device__name":"HOST_NAME","vminterface__virtual_machine__device__site__slug":"SITE_SLUG"}` |

Это **шесть** разрешений: для каждого типа свой путь к устройству. У MAC и IP поле назначения полиморфное; `vminterface` — обратная связь NetBox с интерфейсом ВМ. `vmctl` создаёт и читает MAC, а затем меняет **интерфейс ВМ**, назначая MAC основным. `vmctl` читает IP, но не создаёт и не меняет их. Для дисков и интерфейсов `view` нужен для проверки существующих объектов перед `add`. `change` требуется для ВМ (статус, параметры) и интерфейса (primary MAC). `delete` нигде не требуется. [Связь MAC/IP с VM interface в модели NetBox](https://github.com/netbox-community/netbox/blob/main/netbox/virtualization/models/virtualmachines.py).

Если на одной площадке возможны два устройства с одинаковым именем, добавьте в **каждый** JSON ещё условие на tenant устройства, например `"device__tenant__slug":"TENANT_SLUG"` для ВМ, `"virtual_machine__device__tenant__slug":"TENANT_SLUG"` для диска/интерфейса и `"vminterface__virtual_machine__device__tenant__slug":"TENANT_SLUG"` для MAC/IP. Для самого Device используйте `"tenant__slug":"TENANT_SLUG"`. Аналогично задайте `netbox.tenant` в `config.toml`. Если tenant отсутствует, вместо slug можно использовать `null` с суффиксом `__isnull`: например `"device__tenant__isnull":true`.

NetBox объединяет несколько разрешений для одного типа объекта по **ИЛИ**. Поэтому проверьте, что пользователь не получает более широких прав через другие группы, прямые разрешения или `DEFAULT_PERMISSIONS`. `$user` в ограничениях означает самого пользователя, но не раскрывает его свойства вроде «привязанного device»; одной общей группе нельзя задать разные device по этому шаблону. [Правила объединения ограничений](https://netboxlabs.com/docs/netbox/v4.4/administration/permissions/).

## Токен и проверка

Создайте для этого пользователя API-токен v2 с **Write enabled**. По возможности укажите срок действия и **Allowed IPs**: внешний адрес, с которого этот хост выходит к NetBox. Токен запишите на хосте в `/opt/vmctl/netbox.key` с правами `600`; не помещайте его в Git. Если используете файл, уберите прежний `netbox.key` из `config.toml` и проверьте, что не задан `NETBOX_TOKEN`: переменная окружения и ключ в конфигурации имеют приоритет перед файлом. При замене токена сначала запишите новый на хосте, проверьте `vmctl doctor`, затем отзовите старый. [Поля токена NetBox](https://netboxlabs.com/docs/netbox/models/users/token/).

```bash
cd /opt/vmctl
chmod 600 netbox.key
vmctl doctor
vmctl audit
```

Проверьте права с новой тестовой ВМ: `vmctl --dry-run prepare ФАЙЛ` показывает план без записи; `vmctl prepare ФАЙЛ` создаёт в NetBox ВМ, диск, интерфейс и MAC. После этого назначьте IPv4/IPv6 интерфейсу **от имени администратора NetBox**, затем проверьте `vmctl audit` и `vmctl --dry-run sync ИМЯ`. Учёт IP в NetBox не настраивает адреса внутри гостевой ОС. Для контрольной проверки ограничения другим пользователем создайте тестовую ВМ на другом хосте и убедитесь, что API-пользователь первого хоста её не видит и не может изменить. Если NetBox отвергает `vminterface__...` в ограничении MAC или IP, проверьте версию NetBox и его модель обратной связи; не заменяйте ограничение глобальным правом ради прохождения проверки.

Если хосту нужны самостоятельные операции с IP из `vmctl` в будущем, матрицу прав и код потребуется расширить отдельно. Сейчас назначение адресов и выбор Primary IPv4/IPv6 выполняет администратор в NetBox.
