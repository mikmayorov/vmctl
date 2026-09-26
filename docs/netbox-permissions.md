# Права NetBox для vmctl

Для всех хостов создайте **одну группу** пользователей `vmctl-hosts` и назначьте ей пять разрешений ниже. На каждый хост создавайте отдельного пользователя и API-токен. Создайте для хоста запись **Owner**, включите в неё этого пользователя и назначьте Owner устройству хоста (`Device`). Разрешения через `$user` проверяют владельца устройства, к которому привязана ВМ. Эта схема требует NetBox 4.5 или новее. [Owner и ограничения прав](https://netboxlabs.com/blog/netbox-object-owner-functionality/).

## Настройка общей группы

В **Admin → Authentication → Permissions** создайте пять разрешений, назначив **группу `vmctl-hosts` каждому**. Не оставляйте поля Users и Groups пустыми: тогда правило не действует. Не назначайте правила напрямую отдельному API-пользователю. В Constraints вводите JSON с двойными кавычками.

| Тип объекта | Действия | Constraints |
| --- | --- | --- |
| `dcim.device` | `view` | `{"owner__users":"$user"}` |
| `virtualization.virtualmachine` | `view`, `add`, `change`, `delete` | `{"device__owner__users":"$user"}` |
| `virtualization.virtualdisk`, `virtualization.vminterface` | `view`, `add`, `change`, `delete` | `{"virtual_machine__device__owner__users":"$user"}` |
| `dcim.macaddress` | `view`, `add`, `change`, `delete` | `{"vminterface__virtual_machine__device__owner__users":"$user"}` |
| `ipam.ipaddress` | `view` | `{"vminterface__virtual_machine__device__owner__users":"$user"}` |

Здесь Virtual disk и VM interface объединены в одно правило, потому что путь к устройству у них одинаковый. MAC и IP имеют полиморфную связь с интерфейсом ВМ; `vminterface` — обратная связь модели NetBox. `vmctl` читает IP, но не назначает и не меняет их. Права `change` и `delete` нужны для управления компонентами и удаления ВМ. [Объектные разрешения NetBox](https://netboxlabs.com/docs/netbox/v4.4/administration/permissions/), [связи VM interface](https://github.com/netbox-community/netbox/blob/main/netbox/virtualization/models/virtualmachines.py).

Проверьте, что API-пользователь не получает широкие права через другие группы, прямые назначения или `DEFAULT_PERMISSIONS`: NetBox объединяет подходящие разрешения по «ИЛИ». Запись Owner должна включать **конкретного пользователя хоста**, а не общую группу `vmctl-hosts`: иначе все хосты станут владельцами всех таких устройств.

## Подключение нового хоста

1. Администратор создаёт кластер и устройство хоста, привязывает устройство к кластеру.
2. Создаёт обычного пользователя `vmctl-ИМЯ_ХОСТА`, добавляет его в `vmctl-hosts`. Флаги Staff и Superuser ему не нужны.
3. Создаёт отдельный Owner для хоста, добавляет в Owner этого пользователя и назначает Owner устройству хоста. Owner самой ВМ назначать не требуется: право проходит через её `device`.
4. Создаёт пользователю API-токен v2 с **Write enabled**. При необходимости задаёт Allowed IPs и срок действия. Кладёт токен на хост в `/opt/vmctl/netbox.key` с правами `600`.
5. Указывает в `config.toml` URL NetBox, имя device и при необходимости slug site/tenant; запускает `vmctl doctor`.

Если используете файл `netbox.key`, уберите ключ из `config.toml` и проверьте, что не задан `NETBOX_TOKEN`: они имеют приоритет перед файлом. [Поля API-токена](https://netboxlabs.com/docs/netbox/models/users/token/).

## Проверка доступа и операций

На тестовой ВМ проверьте `vmctl prepare`, `vmctl sync` и `vmctl audit`, затем добавление и удаление дополнительного диска и интерфейса. IP назначьте интерфейсу вручную в NetBox IPAM; перед удалением интерфейса снимите назначенные IP там же. `vmctl` остановит удаление интерфейса с IP. Убедитесь также, что токен одного хоста не видит ВМ другого и не может её изменить. Если NetBox отвергает `vminterface__...` в ограничениях MAC/IP, проверьте версию и модель обратной связи NetBox; не заменяйте это правило глобальным правом.

ВМ целиком удаляйте через `vmctl delete`, а не удалением записи через UI NetBox: отсутствие записи для `sync` означает расхождение, а не команду удалить локальную ВМ.
