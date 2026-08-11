let csrfToken = "";
let lastConfig = null;

const ruTranslations = {
  "Network settings": "Сетевые настройки",
  "Storage and images": "Хранилище и образы",
  "Computers and domain": "Компьютеры и домен",
  "Deployment": "Развёртывание",
  "Configuration overview": "Обзор конфигурации",
  "Check the source files, set administrator access, and follow the steps in order.": "Проверьте исходные файлы, задайте доступ администратора и последовательно пройдите все шаги.",
  "Save and continue": "Сохранить и продолжить",
  "Configure how WinPE and regular clients connect to IronAPI.": "Настройте, как WinPE и обычные клиенты подключаются к IronAPI.",
  "Access mode": "Режим доступа",
  "Choose how clients reach IronAPI.": "Выберите способ доступа клиентов к IronAPI.",
  "Direct HTTP access": "Прямой доступ (HTTP)",
  "Clients connect directly to IronAPI over HTTP.": "Клиенты подключаются напрямую к IronAPI по HTTP.",
  "HTTPS through reverse proxy": "HTTPS через обратный прокси",
  "Clients connect securely through an external proxy.": "Клиенты подключаются по HTTPS через внешний прокси.",
  "IronAPI addressing": "Адресация IronAPI",
  "Set the internal listener and the address visible to clients.": "Задайте внутренний интерфейс и адрес, доступный клиентам.",
  "Internal TCP port": "Внутренний TCP-порт",
  "External address": "Внешний адрес",
  "Use a hostname or IP; standard HTTPS port 443 is omitted automatically.": "Укажите имя или IP; стандартный HTTPS-порт 443 автоматически не отображается.",
  "HTTPS certificate": "Сертификат HTTPS",
  "WinPE and post-install can verify the proxy certificate before sending credentials.": "WinPE и post-install могут проверить сертификат прокси до передачи учётных данных.",
  "Validate the certificate on clients": "Проверять сертификат на клиентах",
  "Recommended for HTTPS deployments.": "Рекомендуется для развёртываний по HTTPS.",
  "Check certificate": "Проверить сертификат",
  "Client network access": "Доступ клиентских сетей",
  "Limit which client networks may call IronAPI.": "Ограничьте сети, из которых клиенты могут обращаться к IronAPI.",
  "Check connection settings": "Проверить настройки соединения",
  "Connection topology": "Топология подключения",
  "Live preview for the selected access mode.": "Предпросмотр для выбранного режима доступа.",
  "WinPE clients": "WinPE-клиенты",
  "Regular clients": "Обычные клиенты",
  "Certificate check": "Проверка сертификата",
  "YES": "ДА",
  "NO": "НЕТ",
  "HTTPS reverse proxy": "HTTPS обратный прокси",
  "IronAPI server": "Сервер IronAPI",
  "Certificate validation applies to WinPE and post-install.": "Проверка сертификата применяется к WinPE и post-install.",
  "Clients connect directly over HTTP; certificate settings are hidden.": "Клиенты подключаются напрямую по HTTP; настройки сертификата скрыты.",
  "Configure the SMB source, mapped paths, image selection, and driver upload limits.": "Настройте SMB-источник, подключаемые пути, выбор образа и лимиты загрузки драйверов.",
  "Content paths are derived automatically from the selected drive.": "Пути к содержимому автоматически формируются из выбранного диска.",
  "Image application": "Применение образа",
  "Select the image index and how Windows image data is applied.": "Выберите индекс образа и способ применения данных Windows.",
  "Image index": "Индекс образа",
  "Image apply mode": "Режим применения образа",
  "Direct from SMB": "Напрямую из SMB",
  "Stage locally first": "Сначала скопировать локально",
  "Define predictable computer names and optional Active Directory integration.": "Задайте понятные имена компьютеров и необязательную интеграцию с Active Directory.",
  "Build predictable names and preview the next generated value.": "Настройте предсказуемые имена и проверьте следующее значение.",
  "Leave both groups empty to deploy without Active Directory.": "Оставьте обе группы пустыми для развёртывания без Active Directory.",
  "Directory advanced settings": "Расширенные настройки каталога",
  "Set deployment deadlines, Windows first-boot behavior, and service options.": "Задайте сроки развёртывания, поведение первого запуска Windows и параметры сервиса.",
  "Hard deadlines that regular API activity does not extend.": "Жёсткие сроки, которые не продлеваются обычной активностью API.",
  "Windows first boot": "Первый запуск Windows",
  "Choose the time zone and local administrator behavior after image application.": "Выберите часовой пояс и поведение локального администратора после применения образа.",
  "Windows time zone": "Часовой пояс Windows",
  "Setup local administrator name": "Имя локального администратора настройки",
  "Enable setup local administrator": "Включить локального администратора настройки",
  "Create the temporary setup account used by post-install.": "Создать временную учётную запись для post-install.",
  "Enable built-in Administrator": "Включить встроенного Administrator",
  "Enable the Windows built-in Administrator account.": "Включить встроенную учётную запись Administrator Windows.",
  "Show image application progress": "Показывать ход применения образа",
  "Display the WinPE graphical progress window.": "Показывать графическое окно прогресса WinPE.",
  "IronAPI service settings": "Параметры сервиса IronAPI",
  "Run the repository validation without starting IronAPI.": "Запустите проверку репозитория без запуска IronAPI.",
  "Waiting": "Ожидание",
  "Unattend template": "Шаблон unattend",
  "Back": "Назад",
  "Save configuration": "Сохранить конфигурацию",
  "Finish setup": "Завершить настройку",
  "Certificate is valid until": "Сертификат действителен до",
  "Connection settings are consistent.": "Настройки соединения согласованы.",
  "Select a certificate file before checking it.": "Выберите файл сертификата перед проверкой.",
  "Interface language": "Язык интерфейса",
  "Setup sections": "Разделы настройки",
  "Overview": "Обзор",
  "Additional": "Дополнительно",
  "Validation": "Проверка",
  "Local setup session": "Локальная сессия настройки",
  "Loading...": "Загрузка...",
  "127.0.0.1 only": "Только 127.0.0.1",
  "Save": "Сохранить",
  "Finish": "Завершить",
  "Configuration Overview": "Обзор конфигурации",
  "Current source files and session state.": "Текущие исходные файлы и состояние сессии.",
  "IronAPI environment": "Окружение IronAPI",
  "Unknown": "Неизвестно",
  "WinPE deploy config": "Конфигурация развёртывания WinPE",
  "SMB password": "Пароль SMB",
  "Not loaded into browser": "Не загружается в браузер",
  "Leave blank to keep the existing saved value.": "Оставьте пустым, чтобы сохранить текущее значение.",
  "Windows unattend template": "Шаблон автоматической установки Windows",
  "IronAPI Settings": "Настройки IronAPI",
  "Writes": "Записывает",
  ". It does not start IronAPI.": ". IronAPI при этом не запускается.",
  "Basic": "Основные",
  "Administrator access": "Доступ администратора",
  "Credentials for the account that manages IronAPI users and permissions.": "Учётные данные аккаунта, который управляет пользователями и разрешениями IronAPI.",
  "Service endpoint": "Адрес сервиса",
  "Address where IronAPI accepts connections from the deployment network.": "Адрес, на котором IronAPI принимает подключения из сети развёртывания.",
  "Access mode": "Режим доступа",
  "HTTP direct": "HTTP напрямую",
  "HTTPS through reverse proxy": "HTTPS через reverse proxy",
  "Direct HTTP exposes IronAPI on the selected network address. HTTPS reverse proxy keeps IronAPI on 127.0.0.1:8000 and expects an external proxy.": "Прямой HTTP публикует IronAPI на выбранном сетевом адресе. Режим HTTPS оставляет IronAPI на 127.0.0.1:8000 и предполагает внешний reverse proxy.",
  "Computer naming": "Имена компьютеров",
  "Build predictable names while keeping the generated result visible.": "Настройте понятную схему имён и сразу увидите результат.",
  "Next generated name": "Следующее имя",
  "Existing LDAP names are skipped automatically.": "Существующие имена LDAP пропускаются автоматически.",
  "Directory and domain join": "Каталог и ввод в домен",
  "LDAP lookup and Offline Domain Join settings are kept together.": "Параметры поиска LDAP и Offline Domain Join собраны в одном месте.",
  "Active Directory is optional. Leave these fields empty to deploy without domain join; IronAPI then rejects any deployment that requests one.": "Active Directory необязателен. Оставьте эти поля пустыми, чтобы разворачивать без ввода в домен; тогда IronAPI отклонит любое развёртывание, которое его запросит.",
  "LDAP directory": "Каталог LDAP",
  "Offline Domain Join": "Offline Domain Join",
  "IronAPI superadmin login": "Логин суперадминистратора IronAPI",
  "Initial account that exclusively manages users and permissions.": "Начальная учётная запись с исключительным доступом к управлению пользователями и разрешениями.",
  "IronAPI superadmin password": "Пароль суперадминистратора IronAPI",
  "At least 12 characters. Stored only as a salted password hash in Data.": "Не менее 12 символов. В Data хранится только хеш пароля с солью.",
  "Leave blank to keep current password": "Оставьте пустым, чтобы сохранить текущий пароль",
  "Required": "Обязательно",
  "Bind address": "Адрес привязки",
  "IP address for IronAPI. Example: 127.0.0.1, 198.51.100.10, or 0.0.0.0.": "IP-адрес IronAPI. Пример: 127.0.0.1, 198.51.100.10 или 0.0.0.0.",
  "TCP port": "TCP-порт",
  "Integer from 1 to 65535. Example: 8000.": "Целое число от 1 до 65535. Пример: 8000.",
  "Computer prefix": "Префикс компьютера",
  "1-15 letters/digits/hyphens, starts with a letter. Example: pc.": "От 1 до 15 букв, цифр или дефисов; начинается с буквы. Пример: pc.",
  "Number width": "Разрядность номера",
  "Digits after prefix. Example: 5 makes pc00001.": "Количество цифр после префикса. Например, 5 даёт pc00001.",
  "Starting number": "Начальный номер",
  "First number when no matching LDAP names exist. Example: 1.": "Первый номер, если в LDAP нет подходящих имён. Пример: 1.",
  "LDAP server": "Сервер LDAP",
  "Domain controller host or IP, no ldap:// prefix. Empty disables LDAP with empty Base DN.": "Имя или IP контроллера домена без префикса ldap://. Пустое значение вместе с пустым Base DN отключает LDAP.",
  "LDAP base DN": "Базовый DN LDAP",
  "Search root. Example: DC=example,DC=test. Empty disables LDAP with empty server.": "Корень поиска. Пример: DC=example,DC=test. Пустое значение вместе с пустым сервером отключает LDAP.",
  "LDAPS": "LDAPS",
  "true uses TCP 636 with trusted LDAP TLS; false uses TCP 389.": "true использует TCP 636 и доверенный LDAP TLS; false использует TCP 389.",
  "LDAP searches use the Windows identity running IronAPI; no separate domain credential is stored.": "LDAP-поиск использует учётную запись Windows, от которой запущен IronAPI; отдельные доменные учётные данные не сохраняются.",
  "ODJ domain": "Домен ODJ",
  "AD DNS domain passed to API-server djoin.exe. Example: example.test.": "DNS-домен AD, передаваемый djoin.exe на API-сервере. Пример: example.test.",
  "ODJ machine OU": "OU компьютеров ODJ",
  "Distinguished name for new computer accounts. Example: OU=Workstations,OU=Clients,DC=example,DC=test.": "Отличительное имя для новых учётных записей компьютеров. Пример: OU=Workstations,OU=Clients,DC=example,DC=test.",
  "Advanced settings": "Расширенные настройки",
  "Allowed client networks": "Разрешённые сети клиентов",
  "Comma-separated CIDR networks. Leave empty to accept clients from all networks. Example: 192.0.2.0/24,198.51.100.0/24.": "CIDR-сети через запятую. Оставьте поле пустым, чтобы принимать клиентов из любых сетей. Пример: 192.0.2.0/24,198.51.100.0/24.",
  "Access log": "Журнал доступа",
  "Uvicorn request log. Usually false for cleaner logs.": "Журнал запросов Uvicorn. Обычно false для более чистых логов.",
  "Database URL": "URL базы данных",
  "SQLAlchemy URL. Example: sqlite:///{IRONDEPLOY_ROOT}/Data/irondeploy.db. Empty restores the .env.example value.": "URL SQLAlchemy. Пример: sqlite:///{IRONDEPLOY_ROOT}/Data/irondeploy.db. Пустое значение восстанавливает значение из .env.example.",
  "LDAP timeout": "Тайм-аут LDAP",
  "Connection timeout in seconds, 1-60. Example: 5.": "Тайм-аут подключения в секундах, от 1 до 60. Пример: 5.",
  "ODJ timeout": "Тайм-аут ODJ",
  "djoin /provision timeout in seconds, 1-300. Example: 60.": "Тайм-аут djoin /provision в секундах, от 1 до 300. Пример: 60.",
  "ODJ blob lifetime": "Срок хранения ODJ blob",
  "Short provision-to-download lifetime in minutes, 5-1440. Default 5. It is independent of the total deployment timeout.": "Короткий срок от создания до скачивания ODJ blob в минутах, 5–1440. По умолчанию 5. Не зависит от общего тайм-аута развёртывания.",
  "ODJ blob directory": "Каталог blob-файлов ODJ",
  "Protected local API-server directory, not SMB. Example: {IRONDEPLOY_ROOT}\\\\ODJ\\\\pending.": "Защищённый локальный каталог API-сервера, не SMB. Пример: {IRONDEPLOY_ROOT}\\\\ODJ\\\\pending.",
  "API server djoin.exe path": "Путь к djoin.exe на API-сервере",
  "Absolute path on the API server, not WinPE. Example: C:\\\\Windows\\\\System32\\\\djoin.exe.": "Абсолютный путь на API-сервере, не в WinPE. Пример: C:\\\\Windows\\\\System32\\\\djoin.exe.",
  "WinPE Deploy Config": "Конфигурация развёртывания WinPE",
  ". Rebuild ISO separately when needed.": ". При необходимости ISO пересобирается отдельно.",
  "SMB deployment share": "SMB-ресурс развёртывания",
  "Network source for Windows images, drivers, and optional installers. Credentials remain server-side and are released only to an authorized deployment.": "Сетевой источник образов Windows, драйверов и дополнительных установщиков. Учётные данные остаются на сервере и выдаются только авторизованному развёртыванию.",
  "IronAPI connection": "Подключение к IronAPI",
  "Endpoint used by WinPE during deployment and by Windows during post-install.": "Адрес, используемый WinPE при развёртывании и Windows во время post-install.",
  "Certificate trust": "Доверие сертификату",
  "Trust material used when certificate validation is enabled for HTTPS.": "Доверенный сертификат для проверки HTTPS-подключения.",
  "SMB share UNC path": "UNC-путь SMB-ресурса",
  "UNC path exposed to WinPE. Example: \\\\DEPLOY-SERVER\\\\IronDeploy.": "UNC-путь, доступный из WinPE. Пример: \\\\DEPLOY-SERVER\\\\IronDeploy.",
  "Read-only SMB account": "Учётная запись SMB только для чтения",
  "Account WinPE uses to read the share. Example: DOMAIN\\\\irondeploy_ro or SERVER\\\\iron_ro.": "Учётная запись, с которой WinPE читает ресурс. Пример: DOMAIN\\\\irondeploy_ro или SERVER\\\\iron_ro.",
  "Stored server-side in Api\\.env and returned only to an authenticated deployment. Leave blank to keep existing.": "Хранится на сервере в Api\\.env и выдаётся только авторизованному развёртыванию. Оставьте пустым, чтобы сохранить текущее значение.",
  "Leave blank to keep existing": "Оставьте пустым, чтобы сохранить текущее значение",
  "IronAPI URL": "URL IronAPI",
  "URL reachable from WinPE. Example: http://198.51.100.10:8000.": "URL, доступный из WinPE. Пример: http://198.51.100.10:8000.",
  "Validate IronAPI certificate": "Проверять сертификат IronAPI",
  "When enabled, WinPE and post-install verify the HTTPS certificate chain and server name.": "Если включено, WinPE и post-install проверяют цепочку HTTPS-сертификата и имя сервера.",
  "Certificate type": "Тип сертификата",
  "For self-signed upload the server certificate. For CA upload the trusted root or private CA certificate.": "Для self-signed загрузите сертификат сервера. Для CA загрузите доверенный корневой сертификат или сертификат частного CA.",
  "Self-signed server certificate": "Self-signed сертификат сервера",
  "CA certificate": "Сертификат CA",
  "Trusted certificate": "Доверенный сертификат",
  "Accepted formats: DER or PEM .cer, .crt, or .pem. Required when validation is enabled.": "Поддерживаются DER или PEM в файлах .cer, .crt или .pem. При включённой проверке сертификат обязателен.",
  "Required when validation is enabled": "Обязателен при включённой проверке",
  "Saved; choose a file to replace it": "Сохранён; выберите файл, чтобы заменить его",
  "Select a certificate file before saving.": "Перед сохранением выберите файл сертификата.",
  "The certificate file must not exceed 64 KiB.": "Размер файла сертификата не должен превышать 64 КиБ.",
  "Yes": "Да",
  "No": "Нет",
  "Optional": "Дополнительно",
  "Additional settings": "Дополнительные настройки",
  "Optional tuning for service timeouts, driver uploads, and WinPE drive mapping.": "Дополнительные параметры тайм-аутов сервисов, загрузки драйверов и подключения диска WinPE.",
  "Deployment lifecycle": "Жизненный цикл развёртывания",
  "Hard Bearer deadlines. Regular API activity does not extend either window.": "Жёсткие сроки Bearer. Обычная активность API не продлевает ни одно из окон.",
  "Authorization window (minutes)": "Окно авторизации (минуты)",
  "Minutes allowed between successful WinPE login and /begin. Range 5-30. Default 10.": "Время между успешным входом WinPE и /begin. Диапазон 5–30, по умолчанию 10.",
  "Total deployment timeout (minutes)": "Общий тайм-аут развёртывания (минуты)",
  "Hard limit from /begin through post-install. Range 30-240. Default 90. Requests do not extend it.": "Жёсткий предел от /begin до завершения post-install. Диапазон 30–240, по умолчанию 90. Запросы его не продлевают.",
  "Service timeouts": "Тайм-ауты сервисов",
  "Limits for external LDAP and Offline Domain Join operations.": "Ограничения времени для внешних операций LDAP и Offline Domain Join.",
  "Driver upload limits": "Ограничения загрузки драйверов",
  "Safety limits for temporary driver package uploads. Changes take effect after restarting IronAPI.": "Ограничения для временных загрузок пакетов драйверов. Изменения применяются после перезапуска IronAPI.",
  "Maximum files per package": "Максимум файлов в пакете",
  "Maximum number of files in one package, 1-1000000. Recommended: 25000.": "Максимальное число файлов в одном пакете: 1–1000000. Рекомендуется: 25000.",
  "Maximum directory depth": "Максимальная глубина каталогов",
  "Maximum nested directory depth inside a package, 1-100. Recommended: 16.": "Максимальная глубина вложенных каталогов пакета: 1–100. Рекомендуется: 16.",
  "Maximum full path": "Максимальная длина полного пути",
  "Maximum final Windows path length in characters, 64-32767. Recommended: 240.": "Максимальная длина конечного пути Windows в символах: 64–32767. Рекомендуется: 240.",
  "Abandoned upload age": "Возраст заброшенной загрузки",
  "Hours without activity before an unfinished upload is marked abandoned, 1-8760. Recommended: 24.": "Часы без активности, после которых загрузка считается заброшенной: 1–8760. Рекомендуется: 24.",
  "Maximum unfinished uploads": "Максимум незавершённых загрузок",
  "Maximum unfinished driver uploads on the server, 1-100. Recommended: 3.": "Максимальное число незавершённых загрузок драйверов на сервере: 1–100. Рекомендуется: 3.",
  "Minimum free disk space": "Минимум свободного места",
  "Free space required on the Share\\Drivers volume before starting an upload, 1-10240 GiB. Recommended: 25.": "Свободное место на томе Share\\Drivers, необходимое для начала загрузки: 1–10240 ГиБ. Рекомендуется: 25.",
  "WinPE drive mapping": "Подключение диска WinPE",
  "The mapped drive is configurable. Content paths are derived automatically and shown for verification.": "Букву подключаемого диска можно изменить. Пути к содержимому вычисляются автоматически и показаны для проверки.",
  "WinPE drive": "Диск WinPE",
  "Temporary mapped drive letter in WinPE. Example: Z:.": "Буква временно подключённого диска в WinPE. Пример: Z:.",
  "Images path": "Путь к образам",
  "Derived from WinPE drive. Example: Z:\\\\Images.": "Формируется из диска WinPE. Пример: Z:\\\\Images.",
  "Drivers path": "Путь к драйверам",
  "Derived from WinPE drive. Example: Z:\\\\Drivers.": "Формируется из диска WinPE. Пример: Z:\\\\Drivers.",
  "Post-install path": "Путь post-install",
  "Derived from WinPE drive. Example: Z:\\\\PostInstall.": "Формируется из диска WinPE. Пример: Z:\\\\PostInstall.",
  "Runs": "Запускает",
  "without starting IronAPI.": "без запуска IronAPI.",
  "Run validation": "Запустить проверку",
  "Validation output will appear here.": "Результат проверки появится здесь.",
  "Created from example": "Создано из примера",
  "Configured": "Настроено",
  "Missing": "Отсутствует",
  "Saved, not displayed": "Сохранён, не отображается",
  "Required; at least 12 characters": "Обязательно; не менее 12 символов",
  "Required; at least 32 characters": "Обязательно; не менее 32 символов",
  "Configured; leave blank to keep it": "Настроено; оставьте пустым, чтобы сохранить",
  "Using example until saved": "Используется пример до первого сохранения",
  "Running validation...": "Выполняется проверка...",
  "Exit code": "Код завершения",
  "Configuration saved.": "Конфигурация сохранена.",
  "Backups": "Резервных копий",
  "Validation completed.": "Проверка завершена.",
  "Validation found issues.": "Проверка обнаружила проблемы.",
  "SetupWeb session closed. You can close this tab.": "Сессия SetupWeb завершена. Эту вкладку можно закрыть."
};

Object.assign(ruTranslations, {
  "1. Access mode": "1. Режим доступа",
  "2. IronAPI addressing": "2. Адресация IronAPI",
  "3. HTTPS certificate": "3. Сертификат HTTPS",
  "4. Client network access": "4. Доступ клиентских сетей",
  "HTTPS through reverse proxy": "HTTPS через обратный прокси",
  "Self-signed server certificate": "Самоподписанный сертификат",
  "HTTPS reverse proxy": "HTTPS обратный прокси",
  "Choose file": "Выбрать файл",
  "No file chosen": "Файл не выбран",
  "Saved certificate": "Сохранённый сертификат",
  "Unsorted": "Не прошедшее сортировку",
  "Unsorted settings": "Не прошедшие сортировку настройки",
  "Continue": "Продолжить",
  "Current source files and local setup session state.": "Текущие файлы конфигурации и состояние локальной сессии настройки.",
  "WinPE deploy config": "Конфигурация развёртывания WinPE",
  "Existing WinPE deployment settings. Network connectivity is configured in step 2.": "Существующие параметры развёртывания WinPE. Сетевое подключение настраивается на шаге 2.",
  "Existing SetupWeb controls that have not yet been assigned to the new flow.": "Существующие настройки SetupWeb, которые ещё не распределены по новому процессу.",
  "Existing optional tuning for service timeouts, driver uploads, and WinPE drive mapping.": "Существующие дополнительные параметры тайм-аутов, загрузки драйверов и подключения диска WinPE.",
  "5. Administrator access": "5. Доступ администратора",
  "Choose the credentials that IronAPI should use for its superadministrator.": "Укажите данные, которые IronAPI должен использовать для суперадминистратора.",
  "Choose the login you want to create or use for the IronAPI superadministrator.": "Укажите желаемый логин суперадминистратора IronAPI.",
  "Choose a password of at least 12 characters. It is stored only as a salted password hash.": "Укажите желаемый пароль длиной не менее 12 символов. Он хранится только в виде хеша с солью.",
  "This is not a sign-in.": "Это не авторизация.",
  "You are defining the credentials you want IronAPI to use.": "Вы задаёте произвольные учётные данные, которые хотите использовать в IronAPI.",
  "SMB server": "SMB-сервер",
  "SetupWeb currently configures a share only on this computer.": "Сейчас SetupWeb настраивает SMB-ресурс только на этом компьютере.",
  "SMB connection address": "Адрес подключения SMB",
  "Hostname, FQDN, or IPv4 address WinPE should use to reach this computer.": "Имя компьютера, FQDN или IPv4-адрес, по которому WinPE будет обращаться к этому компьютеру.",
  "DEPLOY01 or 192.168.1.10": "DEPLOY01 или 192.168.1.10",
  "Defaults to this computer name; replace it when DNS is unavailable.": "По умолчанию используется имя этого компьютера; если DNS недоступен, укажите IP-адрес.",
  "Share name": "Имя общего ресурса",
  "Windows SMB share name. The physical folder is always IronDeploy Core\\Share.": "Имя общего ресурса Windows SMB. Физическая папка всегда IronDeploy Core\\Share.",
  "Resulting UNC path": "Итоговый UNC-путь",
  "Fixed local folder": "Фиксированная локальная папка",
  "Result": "Итог",
  "Connection and source paths": "Пути подключения и источника",
  "SMB configuration result": "Итог настройки SMB",
  "Use an existing Windows account in SERVER\\user or DOMAIN\\user format. SetupWeb does not create this account.": "Используйте существующую учётную запись Windows в формате SERVER\\user или DOMAIN\\user. SetupWeb не создаёт эту учётную запись.",
  "For a local account, COMPUTERNAME\\user is preferred.": "Для локальной учётной записи предпочтителен формат COMPUTERNAME\\user.",
  "Stored server-side in Api\\.env and returned only to an authenticated deployment. Leave blank to use the saved password.": "Хранится на сервере в Api\\.env и выдаётся только авторизованному развёртыванию. Оставьте пустым, чтобы использовать сохранённый пароль.",
  "Leave blank to use saved password": "Оставьте пустым, чтобы использовать сохранённый пароль",
  "Password is required": "Требуется пароль",
  "Saved password will be used when this field is empty": "Если поле пустое, будет использован сохранённый пароль",
  "The SMB account must already exist.": "Учётная запись SMB должна уже существовать.",
  "Use SERVER\\user for a local account or DOMAIN\\user for a domain account.": "Для локальной учётной записи используйте SERVER\\user, для доменной — DOMAIN\\user.",
  "Local share creation always targets this computer; the connection address controls how WinPE and the access check reach it.": "Локальный ресурс всегда создаётся на этом компьютере; адрес подключения определяет, как к нему обращаются WinPE и проверка доступа.",
  "Check SMB access": "Проверить доступ SMB",
  "Create or configure local share": "Создать или настроить локальный ресурс",
  "Enter a share name": "Укажите имя общего ресурса",
  "Enter an SMB address and share name": "Укажите адрес SMB и имя общего ресурса",
  "Use 1-80 letters, digits, dots, underscores, or hyphens.": "Используйте 1–80 букв, цифр, точек, подчёркиваний или дефисов.",
  "Use a hostname, FQDN, or IPv4 address.": "Укажите имя компьютера, FQDN или IPv4-адрес.",
  "Use SERVER\\user or DOMAIN\\user format.": "Используйте формат SERVER\\user или DOMAIN\\user.",
  "The SMB share name is invalid.": "Недопустимое имя общего ресурса SMB.",
  "The SMB connection address is invalid.": "Недопустимый адрес подключения SMB.",
  "The SMB account must use SERVER\\user or DOMAIN\\user format.": "Учётная запись SMB должна иметь формат SERVER\\user или DOMAIN\\user.",
  "Checking SMB access...": "Проверяется доступ SMB...",
  "Configuring local SMB share...": "Настраивается локальный SMB-ресурс...",
  "SMB access verified.": "Доступ SMB подтверждён.",
  "Missing folders": "Отсутствуют папки",
  "Local SMB share created.": "Локальный SMB-ресурс создан.",
  "Local SMB share permissions updated.": "Права локального SMB-ресурса обновлены.",
  "Permissions assigned to": "Права назначены для",
});

window.SetupWebTranslations = ruTranslations;

// The current task-oriented runtime lives in setup-flow.js. Keep the legacy
// runtime inert here so the mature translation catalog remains available while
// the new flow uses the same bilingual copy.
if (false) {
let currentLanguage = (() => {
  try {
    const saved = localStorage.getItem("setupweb-language");
    if (saved === "ru" || saved === "en") return saved;
  } catch {
    // Storage may be unavailable in a restricted browser session.
  }
  return navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
})();

const originalTextNodes = new WeakMap();
const originalAttributes = new WeakMap();

function t(value) {
  return currentLanguage === "ru" ? (ruTranslations[value] ?? value) : value;
}

function applyLanguage() {
  document.documentElement.lang = currentLanguage;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (!originalTextNodes.has(node)) originalTextNodes.set(node, node.nodeValue);
    const source = originalTextNodes.get(node);
    const trimmed = source.trim();
    if (!trimmed) continue;
    const leading = source.match(/^\s*/)[0];
    const trailing = source.match(/\s*$/)[0];
    node.nodeValue = `${leading}${t(trimmed)}${trailing}`;
  }

  document.querySelectorAll("[title], [placeholder], [aria-label]").forEach((element) => {
    if (!originalAttributes.has(element)) {
      originalAttributes.set(element, {
        title: element.getAttribute("title"),
        placeholder: element.getAttribute("placeholder"),
        ariaLabel: element.getAttribute("aria-label"),
      });
    }
    const source = originalAttributes.get(element);
    if (source.title !== null) element.setAttribute("title", t(source.title));
    if (source.placeholder !== null) element.setAttribute("placeholder", t(source.placeholder));
    if (source.ariaLabel !== null) element.setAttribute("aria-label", t(source.ariaLabel));
  });

  document.querySelectorAll("[data-language]").forEach((button) => {
    const active = button.dataset.language === currentLanguage;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function setLanguage(language, persist = true) {
  currentLanguage = language === "ru" ? "ru" : "en";
  if (persist) {
    try {
      localStorage.setItem("setupweb-language", currentLanguage);
    } catch {
      // The language still applies for the current page.
    }
  }
  applyLanguage();
  if (lastConfig) renderConfig(lastConfig);
}

const messageArea = document.querySelector("#messageArea");

function showMessage(text, type = "success") {
  messageArea.hidden = false;
  messageArea.className = `notice ${type}`;
  messageArea.textContent = text;
}

function clearMessage() {
  messageArea.hidden = true;
  messageArea.textContent = "";
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (options.method && options.method !== "GET") {
    headers.set("x-csrf-token", csrfToken);
  }
  const response = await fetch(url, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed.detail || text;
    } catch {
      detail = text;
    }
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function setField(selector, values) {
  document.querySelectorAll(selector).forEach((input) => {
    const key = input.dataset.api || input.dataset.winpe || input.dataset.unattend || input.dataset.auth;
    if (key in values) {
      if (input.type === "checkbox") {
        input.checked = String(values[key]).toLowerCase() === "true";
      } else {
        input.value = values[key] ?? "";
      }
    }
  });
}

function collectFields(selector, datasetName) {
  const values = {};
  document.querySelectorAll(selector).forEach((input) => {
    values[input.dataset[datasetName]] =
      input.type === "checkbox" ? input.checked : input.value;
  });
  return values;
}

function renderComputerNamePreview() {
  const prefixInput = document.querySelector("[data-api='IRONAPI_NAME_PREFIX']");
  const widthInput = document.querySelector("[data-api='IRONAPI_NAME_WIDTH']");
  const startInput = document.querySelector("[data-api='IRONAPI_NAME_START']");
  const preview = document.querySelector("#computerNamePreview");
  if (!prefixInput || !widthInput || !startInput || !preview) return;

  const prefix = prefixInput.value.trim() || "pc";
  const parsedWidth = Number.parseInt(widthInput.value, 10);
  const width = Number.isInteger(parsedWidth) && parsedWidth > 0
    ? Math.min(parsedWidth, 20)
    : 5;
  const parsedStart = Number.parseInt(startInput.value, 10);
  const start = Number.isInteger(parsedStart) && parsedStart >= 0 ? parsedStart : 1;
  preview.textContent = `${prefix}${String(start).padStart(width, "0")}`;
}

function applyAccessMode() {
  const mode = document.querySelector("[data-api='IRONAPI_ACCESS_MODE']");
  const bindHost = document.querySelector("[data-api='IRONAPI_BIND_HOST']");
  const port = document.querySelector("[data-api='IRONAPI_PORT']");
  if (!mode || !bindHost || !port) return;

  const proxyMode = mode.value === "https_proxy";
  if (proxyMode) {
    if (!bindHost.disabled && bindHost.value !== "127.0.0.1") {
      bindHost.dataset.directValue = bindHost.value;
    }
    if (!port.disabled && port.value !== "8000") {
      port.dataset.directValue = port.value;
    }
    bindHost.value = "127.0.0.1";
    port.value = "8000";
  } else {
    if (bindHost.disabled) {
      bindHost.value = bindHost.dataset.directValue || "0.0.0.0";
    }
    if (port.disabled) {
      port.value = port.dataset.directValue || "8000";
    }
  }
  bindHost.disabled = proxyMode;
  port.disabled = proxyMode;
}

function applyApiCertificateValidation() {
  const validation = document.querySelector("[data-winpe='ValidateApiServerCertificate']");
  const certificateType = document.querySelector("[data-winpe='ApiServerCertificateType']");
  const certificateFile = document.querySelector("#apiServerCertificateFile");
  const certificateStatus = document.querySelector("#apiServerCertificateStatus");
  if (!validation || !certificateType || !certificateFile || !certificateStatus) return;

  const enabled = validation.value === "true";
  const hasSavedCertificate = Boolean(lastConfig?.secrets?.hasApiServerCertificate);
  certificateType.disabled = !enabled;
  certificateFile.disabled = !enabled;
  certificateFile.required = enabled && !hasSavedCertificate;
  certificateStatus.textContent = hasSavedCertificate
    ? t("Saved; choose a file to replace it")
    : t("Required when validation is enabled");
}

async function readCertificateBase64() {
  const input = document.querySelector("#apiServerCertificateFile");
  const file = input?.files?.[0];
  if (!file) return "";
  if (file.size > 64 * 1024) {
    throw new Error(t("The certificate file must not exceed 64 KiB."));
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return btoa(binary);
}

function renderConfig(config) {
  lastConfig = config;
  document.querySelector("#rootPath").textContent = config.root;
  document.querySelector("#apiEnvStatus").textContent = config.files.apiEnvCreated
    ? t("Created from example")
    : config.files.apiEnvExists
      ? t("Configured")
      : t("Missing");
  document.querySelector("#apiEnvPath").textContent = config.files.apiEnv;
  document.querySelector("#winpeStatus").textContent = config.files.winpeConfigCreated
    ? t("Created from example")
    : config.files.winpeConfigExists
      ? t("Configured")
      : t("Missing");
  document.querySelector("#winpePath").textContent = config.files.winpeConfig;
  document.querySelector("#passwordStatus").textContent = config.secrets
    .hasWinpeSharePassword
    ? t("Saved, not displayed")
    : t("Required");
  document.querySelector("#superadminPasswordStatus").textContent = config.secrets
    .hasSuperadminPassword
    ? t("Configured; leave blank to keep it")
    : t("Required; at least 12 characters");
  document.querySelector("#unattendStatus").textContent = config.files.unattendExists
    ? t("Configured")
    : t("Using example until saved");
  document.querySelector("#unattendPath").textContent = config.files.unattend;

  setField("[data-api]", config.api);
  setField("[data-winpe]", config.winpe);
  setField("[data-auth]", config.auth);
  applyAccessMode();
  applyApiCertificateValidation();
  renderComputerNamePreview();
}

async function load() {
  const session = await apiFetch("/api/session");
  csrfToken = session.csrfToken;
  document.querySelector("#rootPath").textContent = session.root;
  renderConfig(await apiFetch("/api/config"));
}

async function save() {
  clearMessage();
  const payload = {
    api: collectFields("[data-api]", "api"),
    winpe: collectFields("[data-winpe]", "winpe"),
    unattend: collectFields("[data-unattend]", "unattend"),
    auth: collectFields("[data-auth]", "auth"),
  };
  const certificateBase64 = await readCertificateBase64();
  const validationEnabled =
    payload.winpe.ValidateApiServerCertificate === "true";
  if (
    validationEnabled &&
    !certificateBase64 &&
    !lastConfig?.secrets?.hasApiServerCertificate
  ) {
    throw new Error(t("Select a certificate file before saving."));
  }
  payload.winpe.ApiServerCertificateBase64 = certificateBase64;
  const result = await apiFetch("/api/config", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderConfig(result.config);
  document.querySelector("#apiServerCertificateFile").value = "";
  const backupText = result.backups.length
    ? ` ${t("Backups")}: ${result.backups.length}.`
    : "";
  showMessage(`${t("Configuration saved.")}${backupText}`);
}

async function validate() {
  clearMessage();
  const output = document.querySelector("#validationOutput");
  output.textContent = t("Running validation...");
  const result = await apiFetch("/api/validate", { method: "POST" });
  output.textContent = [
    `${t("Exit code")}: ${result.exitCode}`,
    "",
    result.stdout || "",
    result.stderr ? `\nSTDERR:\n${result.stderr}` : "",
  ].join("\n");
  showMessage(
    result.exitCode === 0 ? t("Validation completed.") : t("Validation found issues."),
    result.exitCode === 0 ? "success" : "error"
  );
}

async function finish() {
  clearMessage();
  await apiFetch("/api/finish", { method: "POST" });
  showMessage(t("SetupWeb session closed. You can close this tab."));
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    document.querySelectorAll(".view").forEach((view) => {
      view.classList.toggle("is-visible", view.id === button.dataset.section);
    });
  });
});

document.querySelectorAll("[data-language]").forEach((button) => {
  button.addEventListener("click", () => setLanguage(button.dataset.language));
});

document.querySelectorAll(
  "[data-api='IRONAPI_NAME_PREFIX'], [data-api='IRONAPI_NAME_WIDTH'], [data-api='IRONAPI_NAME_START']"
).forEach((input) => input.addEventListener("input", renderComputerNamePreview));

document.querySelector("[data-api='IRONAPI_ACCESS_MODE']")
  .addEventListener("change", applyAccessMode);

document.querySelector("[data-winpe='ValidateApiServerCertificate']")
  .addEventListener("change", applyApiCertificateValidation);

document.querySelector("#saveButton").addEventListener("click", () => {
  save().catch((error) => showMessage(error.message, "error"));
});

document.querySelector("#validateButton").addEventListener("click", () => {
  validate().catch((error) => showMessage(error.message, "error"));
});

document.querySelector("#finishButton").addEventListener("click", () => {
  finish().catch((error) => showMessage(error.message, "error"));
});

document.querySelectorAll("[data-winpe='ShareDrive']").forEach((input) => {
  input.addEventListener("input", () => {
    const drive = input.value || "Z:";
    document.querySelector("[data-winpe='ImagesPath']").value = `${drive}\\Images`;
    document.querySelector("[data-winpe='DriversPath']").value = `${drive}\\Drivers`;
  });
});

setLanguage(currentLanguage, false);
load().catch((error) => showMessage(error.message, "error"));
}
