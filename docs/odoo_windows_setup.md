# Установка и запуск Odoo 20 на Windows

Проверенная командой конфигурация: Windows, Python 3.12 (64-bit), PostgreSQL 18 и Odoo 20.0. Команды выполняются в PowerShell. После клонирования работайте из корня репозитория Odoo, где находятся `odoo-bin` и `requirements.txt`.

## 1. Клонировать репозиторий Odoo

Установите [Git для Windows](https://git-scm.com/downloads/win), если он ещё не установлен. Откройте новый терминал в папке, где хотите разместить Odoo, и выполните:

```powershell
git clone --depth 1 --branch 20.0 https://github.com/odoo/odoo
cd odoo
```

Git создаст папку `odoo`, а команда `cd odoo` откроет корень репозитория. Не переходите ещё раз во вложенную папку `odoo`: она содержит исходный код приложения.

Если репозиторий уже клонирован, пропустите клонирование и откройте терминал в его корневой папке. Убедитесь, что используется версия Odoo 20.0.

## 2. Установить Python 3.12

Установите Python 3.12 (64-bit) с [официального сайта](https://www.python.org/downloads/windows/). В установщике включите `pip` и Python Launcher. Также можно использовать Windows Package Manager:

```powershell
winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
```

После установки откройте новый терминал и проверьте версию:

```powershell
py -3.12 --version
```

Для этой инструкции используйте именно Python 3.12: для него доступен готовый Windows-пакет `rl-renderPM==4.0.3`, указанный в зависимостях использованной версии Odoo. Сборка из исходников не требуется.

## 3. Создать окружение и установить зависимости

Если окружение `.venv312` ещё не создано, выполните:

```powershell
py -3.12 -m venv .venv312
```

Установите инструменты и зависимости:

```powershell
.\.venv312\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

Активировать окружение не нужно: команды используют Python непосредственно из `.venv312`. Изменять `requirements.txt`, отключать `rl-renderPM` или устанавливать другую версию `reportlab` не нужно. Дополнительный файл ограничений инструментов сборки не требуется.

Проверьте результат:

```powershell
.\.venv312\Scripts\python.exe -m pip check
.\.venv312\Scripts\python.exe odoo-bin --version
```

Ожидаемый результат: `No broken requirements found.` и `Odoo Server 20.0`.

## 4. Установить PostgreSQL

Если PostgreSQL уже установлен и его служба работает, повторная установка не нужна. Инструкция проверена с PostgreSQL 18; минимальная версия для Odoo 20.0: PostgreSQL 16.

При новой установке скачайте установщик с [официальной страницы PostgreSQL](https://www.postgresql.org/download/windows/).

1. Установите PostgreSQL Server и pgAdmin 4.
2. Запомните пароль пользователя `postgres`, заданный при установке.
3. Оставьте порт `5432`, если он свободен.
4. Stack Builder и дополнительные пакеты для первого запуска не нужны.

Не удаляйте и не переинициализируйте папку PostgreSQL `data`.

## 5. Создать пользователя PostgreSQL для Odoo

1. Откройте pgAdmin 4.
2. Подключитесь к серверу PostgreSQL от имени `postgres` с паролем, заданным при установке.
3. Выберите базу `postgres` и откройте **Tools → Query Tool**.
4. Выполните SQL, заменив `YOUR_DATABASE_PASSWORD` своим паролем:

```sql
CREATE USER odoo WITH LOGIN PASSWORD 'YOUR_DATABASE_PASSWORD' CREATEDB;
```

Если пользователь `odoo` уже существует, новый создавать не нужно. Если необходимо изменить его пароль или выдать право создания баз, выполните:

```sql
ALTER USER odoo WITH LOGIN PASSWORD 'YOUR_DATABASE_PASSWORD' CREATEDB;
```

Используйте одинаковый пароль здесь и в параметре `db_password` файла настроек. Для работы Odoo права суперпользователя не требуются.

## 6. Создать файл настроек

Создайте `odoo.conf` рядом с `odoo-bin`:

```ini
[options]
addons_path = addons
data_dir = .odoo-data

db_template = template1
db_host = localhost
db_port = 5432
db_user = odoo
db_password = YOUR_DATABASE_PASSWORD

http_interface = 127.0.0.1
http_port = 8069

admin_passwd = YOUR_MASTER_PASSWORD
pg_path = C:\Program Files\PostgreSQL\18\bin
```

Замените `YOUR_DATABASE_PASSWORD` на пароль роли `odoo`, а `YOUR_MASTER_PASSWORD` на отдельный надёжный мастер-пароль управления базами. Не оставляйте значения-заглушки. В примере `pg_path` указан стандартный путь установки PostgreSQL 18 на Windows; при другой версии или расположении исправьте его.

В этой конфигурации используется `template1`: Odoo наследует согласованные `LC_COLLATE` и `LC_CTYPE` из шаблона. Вариант проверен с PostgreSQL 18 и не требует смены локали сервера. С `template0` использованная копия Odoo задаёт только `LC_COLLATE=C`, что на Windows при отличающемся `LC_CTYPE` может приводить к ошибке несовпадения локалей.

`http_interface = 127.0.0.1` ограничивает доступ к Odoo текущим компьютером. Не публикуйте `odoo.conf` с паролями в Git: проверьте, что локальный файл исключён в вашем клоне, особенно если конфигурация хранится в отдельном репозитории команды.

## 7. Запустить Odoo

Из корня репозитория Odoo выполните:

```powershell
.\.venv312\Scripts\python.exe odoo-bin -c odoo.conf
```

Дождитесь сообщения о запуске HTTP-сервера и откройте `http://localhost:8069`. Не закрывайте терминал, пока сервер нужен. Для остановки нажмите **Ctrl+C**. Не запускайте второй экземпляр на том же порту, если сервер уже работает в фоне.

## 8. Создать базу через браузер

Этот шаг выполняется только при первой установке. Если база уже создана, переходите ко входу в Odoo.

| Поле | Значение |
|---|---|
| Master Password | Значение `admin_passwd` из `odoo.conf` |
| Database Name | Например, `odoo_local` |
| Email | Логин будущего администратора |
| Password | Новый пароль для входа администратора в Odoo |
| Language | Нужный язык; в проверенной установке использовался English (US) |
| Country | Нужная страна |
| Demo Data | Отключить, если демонстрационные данные не нужны |

Нажмите **Create database** и дождитесь завершения. Затем войдите с указанными Email и Password. При необходимости откройте страницу управления базами напрямую: `http://localhost:8069/web/database/manager`.

Пароль PostgreSQL, мастер-пароль управления базами и пароль пользователя Odoo являются разными учётными данными.

## 9. Последующие запуски

Повторно устанавливать зависимости, создавать роль PostgreSQL и создавать базу не нужно.

1. Убедитесь, что служба PostgreSQL запущена.
2. Откройте терминал в корне репозитория Odoo.
3. Запустите сервер:

```powershell
.\.venv312\Scripts\python.exe odoo-bin -c odoo.conf
```

Откройте `http://localhost:8069` и войдите в свою базу.

## 10. Папка для собственных модулей

При необходимости создайте рядом с `odoo-bin` папку, например `custom_addons`. В неё помещаются отдельные каталоги модулей; сама папка модулем не является.

После добавления первого модуля измените настройку и перезапустите Odoo:

```ini
addons_path = addons,custom_addons
```

Пока папка пуста, оставляйте `addons_path = addons`, чтобы избежать предупреждения о некорректном каталоге дополнений. Шаги проверки Project AI Conductor описаны отдельно в [руководстве по разработке](development.md#установка-каркаса).
