# Dynamic Driver Resolution: карта механизма

Этот документ — точка входа для агентов, изменяющих AUTO-подбор драйверов.
Перед изменением WinPE также прочитать [WINPE.md](WINPE.md).
Никогда не запускать `Runtime/deploy.ps1` на сервере: он стирает физический диск.

## Режимы и границы

| Режим | Подбор | Доставка и установка |
| --- | --- | --- |
| `AUTO_LOCAL` | HWID по всему включённому локальному Drivers | Только staged TAR, существующий offline DISM |
| `AUTO_LOCAL_WSUS` | Local, затем unresolved передаются WSUS stub | Те же локальные кандидаты, staged TAR |
| `MANUAL_FOLDER` | Прежний выбор одной `Vendor\Model` | Прежние direct/staged без AUTO resolver |
| `NO_DRIVERS` | Отсутствует | Инъекция пропускается |

Имена Dell/HP, модели, Manufacturer/SKU компьютера **не ограничивают** AUTO-поиск.
Папка `Vendor\Model` остаётся единицей импорта и ручного выбора, но не является
автоматически единицей AUTO-инъекции. AUTO выбирает небольшие связанные bundles
INF/файлов из любых включённых импортов. Весь Drivers в staging не копируется.

Для совместимости запрос без `driver_mode` сохраняет прежнюю семантику:
`driver_package` указан → MANUAL_FOLDER; не указан → NO_DRIVERS.
Явный MANUAL_FOLDER требует папку; остальные режимы не принимают её одновременно.
AUTO принудительно возвращает `driverApplyMode=staged`, даже если серверная
настройка для ручного режима — direct. Режим применения WIM не меняется.

## Файлы и ответственность

Пути Python ниже относительны `Core/Api/app`.

| Файл | Ответственность |
| --- | --- |
| `driver_models.py` | Типизированные PnP inventory, target OS, candidate, match, result и протокол provider |
| `driver_index/inf_parser.py` | Чтение INF, HWID/compatible mappings, исходные файлы, связи INF внутри импорта |
| `driver_index/database.py` | Отдельные SQLite connections, WAL, схема, короткие записи, batch lookup |
| `driver_index/service.py` | Четыре parser workers, initial scan/rebuild, публикация ready/error |
| `driver_providers/local.py` | Поиск по индексу, фильтры ОС/архитектуры/availability, проверка выбранных файлов |
| `driver_providers/wsus.py` | Заглушка без сетевых операций |
| `driver_resolver.py` | Оркестрация providers, объединение результатов, диагностика |
| `driver_manifest.py` | Адаптация результата к существующему manifest и staged archive contract |
| `driver_archive_selection.py` | Проверка выбранных файлов и потоковая запись TAR без копии дерева |
| `driver_archives.py` | Общая очередь архивов, лимиты места, status, restart, cleanup; прежняя manual-ветка 7-Zip |
| `drivers.py` | Существующий импорт/rename/delete; уведомляет indexer после публикации файлов |
| `main.py` | Lifecycle, index endpoints, авторизованный deployment manifest |
| `deployments.py`, `database.py` | Контракт запроса, сохранение driver mode/result, миграция 12 и совместимость старых миграций |
| `deployment_images.py` | Кешируемые метаданные выбранного WIM/index, включая version и product type |

WinPE/build:

| Файл относительно `Core` | Ответственность |
| --- | --- |
| `WinPE/Runtime/IronDeploy.DriverInventory.ps1` | Read-only CIM inventory, вызывается только для AUTO |
| `WinPE/Runtime/IronDeploy.Gui.ps1` | Две AUTO-опции в существующем single-choice выборе драйверов |
| `WinPE/Runtime/IronDeploy.Engine.ps1` | Передача JSON inventory, диагностика результата, существующие download/extraction/DISM |
| `WinPE/Build/Rebuild-IronDeployWim.ps1` | Копирует новый inventory helper при следующей запрошенной сборке |
| `WinPE/Build/Rebuild-IronDeployIso.ps1` | То же для ISO build workflow |

## Поток от импорта до первого запуска Windows

```text
Upload -> Drivers/.upload-temp/<slot>/files
       -> finalize: публикация Drivers/Vendor/Model
       -> enqueue indexing -> parse в worker
       -> короткая SQLite transaction -> bundles ready

WinPE: AUTO selected
       -> CIM PnP inventory (порядок ID сохраняется)
       -> POST /api/deploy/{id}/manifest + структурированный JSON
IronAPI: выбранный WIM/index -> target architecture/version/product type
       -> LocalDriverProvider -> batch HWID lookup -> кандидаты/диагностика
       -> [AUTO_LOCAL_WSUS] unresolved -> WsusDriverProvider stub
       -> deployment.driver_resolution + manifest
       -> существующая очередь TAR -> Drivers/.irondeploy-archives/{id}/drivers.tar
WinPE: Apply-WIM -> driver_download -> driver_injection
       -> 7za extraction + byte/file/INF count validation
       -> DISM /Image:C:\ /Add-Driver /Driver:<extracted-directory> /Recurse
       -> cleanup -> первый запуск Windows, Windows PnP выбирает драйверы
```

Подготовка TAR начинается при выдаче manifest и перекрывается по времени с
копированием/применением WIM. Драйверы целевой ОС не загружаются через drvload в
работающий WinPE. Bootstrap-драйверы из `Load-WinPEDrivers.ps1` — отдельная задача.

## Inventory и target OS

`Win32_PnPEntity` даёт `instance_id`, упорядоченные `hardware_ids` и
`compatible_ids`, `device_class`, `status`, `problem_code`.
`Win32_ComputerSystem` даёт manufacturer/model/family/SKU, переменная окружения
WinPE — architecture. Ошибки CIM возвращаются как warnings; пустой inventory
не мешает продолжить deployment. Manual/NO_DRIVERS этот helper не вызывают.

Для matching приоритет имеет архитектура выбранного WIM, а не архитектура WinPE.
`Get-WindowsImage -Index` читает подробную версию и InstallationType каждого
индекса. Метаданные сохраняются существующим механизмом image metadata;
`driverMetadataVersion=1` запускает одноразовое обновление прежних записей.
Если version недоступна, resolver явно предупреждает и не угадывает Windows 10/11
по имени файла. Build restrictions используются только при известных данных.

## Что парсер считает bundle

Поддержаны UTF-16 BOM, UTF-8 и fallback Windows-1252, строки продолжения,
комментарии вне кавычек, Strings substitutions, Manufacturer/Models decorations.
Первый ID в Models — hardware mapping, последующие — compatible mappings.
Сохраняются provider/class/DriverVer/date/version и OS decorations.

Файловое замыкание включает INF, CatalogFile, SourceDisksFiles с путями из
SourceDisksNames, а также CopyFiles, включая прямой `@file`. Сохраняется исходная
структура подкаталогов. Папка модели не копируется целиком даже при плоском
расположении всех INF. Отсутствующий архитектурный CatalogFile исключает mappings
этой архитектуры, не ломая присутствующий вариант другой архитектуры.

INF объединяются в connected groups по общим исходным файлам/CAT, локальным
Include и ComponentIDs → SWC mappings. Extension INF также самостоятельно
попадает в кандидаты по аппаратным ID. Повреждённый INF диагностируется и
исключается; связанные с ним по локальному Include/CAT bundles также исключаются.
Другие корректные bundles того же импорта могут быть опубликованы с предупреждением.
Если не осталось ни одного корректного bundle, import получает `error`.

Индексатор не изменяет, не перемещает, не переименовывает и не удаляет исходные
файлы. Изменение размера/mtime во время parsing отменяет публикацию. При подборе
и перед/после упаковки снова проверяются выбранные файлы. Reparse points,
выход за repository и пути с traversal не допускаются. Повреждённый bundle
исключается отдельно, не обнуляя остальные результаты.

## Индекс и concurrency

Индекс: `Core/Data/drivers_index.sqlite`. Он disposable/rebuildable и не заменяет
основную БД `Core/Data/irondeploy.db` или source of truth `Core/Share/Drivers`.

| Таблица | Содержимое |
| --- | --- |
| `imports` | `path` PK COLLATE NOCASE, indexing/ready/error, диагностика, время |
| `packages` | bundle ID, FK import_path, ready, JSON candidate metadata/file manifest |
| `infs` | FK package_id, исходный INF path, JSON metadata |
| `hardware_ids` | FK inf_id, нормализованный ID, hardware/compatible kind, OS decoration |

Hardware и compatible mappings лежат в одной lookup-таблице с различным `kind`.
Индексы: `hardware_ids(hardware_id)`, `infs(package_id)`, `packages(import_path)`.
Foreign keys с cascade удаляют только индексные строки.

Парсинг выполняют до четырёх workers; очередь принимает и 23 задания.
Каждая операция открывает собственное соединение. WAL позволяет SELECT идти
параллельно writer. `busy_timeout=10000`; внутри процесса writer сериализован
RLock. Транзакция публикации заменяет только один импорт и включает все его
bundles/INF/IDs; нет commit на каждый HWID и writer transaction на весь repository.

Первый импорт не виден до commit. При повторном индексировании прежнее поколение
ready остаётся доступным, пока новая транзакция не опубликована. Ошибка повторного
парсинга сохраняет старое поколение, но его изменённые файлы не пройдут проверку
перед использованием. `imports.status` описывает последнее задание, а
`packages.status` — опубликованное поколение: это намеренно разные состояния.

Lookup собирает ID всех устройств, читает их порциями до 800 параметров в одной
read transaction. JSON больших bundles читается отдельно один раз на уникальный
package, а не повторяется для каждой HWID-строки. Затем Python сопоставляет
результаты устройствам. Resolver не запускает scan/parse при deployment.

## Initial scan, import и rebuild

После успешного `finalize_driver_package_upload` задача индексации ставится в
очередь. Upload response не ждёт parsing; manual package уже доступен прежним
путём, AUTO увидит bundles после commit. Лимит незавершённых uploads не изменён
(по умолчанию 3); это не лимит числа parser jobs.

При старте IronAPI indexer перечисляет только Vendor/Model directories и ставит
в очередь отсутствующие/не-ready импорты. Ready INF повторно не парсятся.
Rename/delete уведомляют indexer: исчезнувшие импорты удаляются из индекса,
новые пути ставятся в очередь. Disabled imports исключаются при lookup через
существующую `.irondeploy-drivers.json` без полного обхода файлов.

- `GET /api/drivers/index` — статусы импортов и ошибки.
- `POST /api/drivers/index/rebuild` — повторно индексировать существующие папки,
  сохраняя прежние поколения на время работы. Использует существующую проверку
  прав записи и same-origin/X-Requested-With convention.
- При ручном копировании/изменении файлов вне IronAPI вызвать rebuild.
- Удалённый индекс восстанавливается initial scan или rebuild. Для ручного
  удаления SQLite остановить IronAPI и удалить только `drivers_index.sqlite`
  и относящиеся к нему `-wal`/`-shm`, затем запустить штатным способом.
  Не удалять основной `irondeploy.db` и пользовательские Drivers.

Прямого нового web-экрана управления индексом нет; endpoints доступны для
администрирования и будущего UI. Не запускать IronAPI ради агента без запроса пользователя.

## Matching и manifest

Для каждого устройства сохраняется лучшая специфичность каждого bundle:
HardwareID[0], HardwareID[1] … CompatibleID[0], CompatibleID[1] … .
`specificity` — позиция в этом порядке; меньше означает точнее.
Exact SUBSYS предшествует generic VEN/DEV, но generic candidate может остаться
в наборе для Windows PnP. Не реализуется полный Windows PnP ranking или выбор
ровно одного INF. Повторы bundles между ID/устройствами убираются.

Отбрасываются известные несовпадения architecture, минимальной NT version/build
и product type. Version/date драйвера хранятся для диагностики, но не заменяют
PnP ranking. Результат содержит detected/matched counts, unresolved devices,
matches с Instance ID/HWID/specificity/source и candidate packages.

Manifest и deployment record содержат `driverMode` / `driverResolution`.
В основной БД это nullable `driver_mode` / `driver_resolution`, миграция 12.
Deployment detail API возвращает эти данные для диагностики.
`driverPackage` у AUTO — адаптер существующего staged transport с виртуальным
`AUTO\<deployment-id>` и `sourceFiles`; такой каталог физически не создаётся.

Потоковая запись TAR читает выбранные исходные файлы без промежуточного дерева.
В TAR пути `Vendor/Model/...` предотвращают коллизии одинаковых имён разных
пакетов. Готовность публикуется только после проверки и rename partial → final.
Используются существующие лимит места, reservations, polling, restart и cleanup.

`Drivers/.upload-temp` хранит незавершённые uploads. `Drivers/.irondeploy-archives`
хранит временные deployment TAR — это разные каталоги и разные жизненные циклы.
После driver_download существующий stage cleanup удаляет серверный архив;
после injection/обработанной ошибки удаляется локальный staging WinPE.

## Providers, ошибки и ограничения

`DriverProvider.resolve(devices, target) -> DriverResolution` — общий контракт.
Resolver знает Local и external providers, но не знает SQL/WSUS API/архиватор.
WSUS stub получает только unresolved devices, логирует вызов, возвращает
`provider_status.wsus=not_implemented` и warning. Нет подключения, authentication,
Microsoft Update, CAB download или WSUS cache. Замена provider не требует
изменения оркестрации resolver; будущий transport должен материализовать и
проверять файлы кандидатов в разрешённом runtime-хранилище.

Недоступность индекса, пустой inventory, unresolved devices, повреждённые
bundles и ошибка внешнего provider не прерывают resolution/deployment.
Ошибка подготовки архива до выдачи manifest в AUTO превращается в warning и
пропуск drivers. Ошибки последующего скачивания/извлечения/DISM сохраняют
существующую строгую обработку deployment pipeline.

Ограничения/TODO:

- Это консервативный parser обычных INF, не полная реализация Windows SetupAPI.
  CAB payload внутри импортируемой папки не распаковывается индексатором.
- Include без локального INF считается зависимостью от целевой Windows и
  диагностируется. Произвольные OEM installers и неявные зависимости не исполняются.
- Include группируется внутри одного импорта. ComponentIDs дополнительно
  разрешаются по всему индексу batched-запросами, даже если SWC-устройство ещё
  не существует в WinPE. Защита от циклов и предел 16 уровней ограничивают обход.
  Include между независимыми импортами автоматически не связывается.
- SuiteMask/FeatureScore/CHID и полный PnP ranking не реализованы. Windows PnP
  остаётся ответственным за окончательный выбор.
- Требуется практический прогон нового WinPE на тестовом ПК/VM после отдельно
  запрошенной сборки. Unit-тесты не доказывают успешную загрузку конкретного железа.
- Будущие задачи: реальный WSUS provider, web-индикация index status/rebuild,
  отдельно согласованное удаление legacy direct для manual drivers.

Логи INFO показывают import start/result, INF/ID counts, parsing/commit time,
deployment ID, число устройств/ID, query/matching time, unresolved, matched HWID
и bundle. DEBUG показывает исключения по OS/architecture. SQL строки не логируются.

## Проверки

Результат реализации (2026-09-09): полный набор IronAPI — **328 tests, OK**.
Пять изменённых/добавленных PowerShell runtime/build файлов прошли AST parsing.
Проверены реальное извлечение тестового AUTO TAR штатным `7za.exe` (включая
кириллицу в пути) и inventory helper с подставным CIM, сохраняющим порядок ID.
В общем наборе остаются ResourceWarning о закрытии SQLite connections из
существующих тестов; ошибок тестов нет.

Read-only проверка существующего `Hp/HP All-in-One Desktop 27-crOxxx` обработала
27 INF в 23 bundles. `netrtwlane613.inf` исключён: заявленный `Rtlihvs.dll`
отсутствует в исходной папке. Ожидаемые inbox Include отражены предупреждениями.
Пользовательские драйверы и действующие БД не изменялись этой проверкой.
IronAPI не запускался, WIM/ISO не пересобирались, реальный deployment не выполнялся.

`Core/Api/tests/test_dynamic_drivers.py` проверяет matching, ordered IDs, SUBSYS,
compatible fallback, architecture/OS/product type, dedup, multi-INF и component
bundles, isolated corruption, 23 jobs с параллельными readers, ready publication,
initial/rebuild/deleted index, import hook, provider stub, manual/no-driver bypass,
отсутствие rescan в AUTO, содержимое TAR и общий archive worker/cleanup.
`test_deployment_timeout.py` дополнительно проверяет AUTO manifest и forced staged.
Существующие driver contract/archive/migration tests остаются частью регрессии.

Команда из `Core/Api`: `.venv\Scripts\python.exe -m unittest discover -s tests -q`.
PowerShell проверяется через AST Parser; destructive engine не выполняется.
В sandbox Windows Python 3.14 TemporaryDirectory может создавать каталоги с ACL,
недоступными тому же процессу: это отдельная проблема окружения тестов.

Справочные контракты Microsoft:
[Models](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/inf-models-section),
[Manufacturer](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/inf-manufacturer-section),
[AddComponent](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/inf-addcomponent-directive).
