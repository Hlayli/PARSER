# CONTRACT — обязательный интерфейс проекта PARSER

Любой агент обязан следовать этому файлу. Не менять чужие файлы. Не менять сигнатуры.

Стек: Python 3.10+ (автор проверяет на 3.14), FastAPI, uvicorn, httpx, beautifulsoup4, stdlib sqlite3. Без SQLAlchemy, без npm/сборки.

---

## 1. Структура и владение файлами

| Файл / папка | Владелец | Назначение |
|---|---|---|
| `backend/db.py` | core | подключение, схема, upsert, выборки |
| `backend/dedup.py` | core | нормализация и сравнение организаций, `brand_key` |
| `backend/merge_duplicates.py` | core | CLI поиска и склейки дубликатов в базе |
| `backend/models.py` | core | dataclass `Place`, `BBox` |
| `backend/scanner.py` | core | сетка, очередь, SSE-события |
| `backend/categories.py` | core | каталог ниш и сборка поисковых запросов |
| `backend/app.py` | core | FastAPI, роуты, статика |
| `backend/adapters/*` | adapters | 3 источника + base + registry |
| `backend/enrichment.py` | enrichment | payload'ы источников + обход сайта, факты |
| `backend/source_facts.py` | enrichment | контакты и адреса из сохранённых payload'ов |
| `backend/site_audit.py` | enrichment | технический аудит HTML без сети |
| `backend/geocode.py` | enrichment | город → bbox (Nominatim) |
| `backend/intel.py` | intel | потенциал, зацепки, оффер, контакты, сообщение |
| `backend/ai.py` | intel | опциональный OpenAI-совместимый клиент |
| `frontend/*` | frontend | HTML/CSS/JS |
| `tests/*`, `README.md`, `docs/*` | docs | тесты, документация, research |

---

## 2. Модели

```python
BBox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)

@dataclass
class Place:
    source: str                      # "google" | "yandex" | "2gis" | "demo"
    source_id: str                   # id в источнике, обязателен
    name: str
    category: str = ""
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    phone: str = ""
    website: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    price_range: str = ""
    hours: str = ""
    photos: list[str] = field(default_factory=list)
    url: str = ""                    # ссылка на карточку в источнике
    raw: dict = field(default_factory=dict)
```

`Place` не содержит выдуманных полей. Нет данных → пустая строка / None.

---

## 3. Адаптеры

`backend/adapters/base.py`:

```python
class AdapterError(Exception): ...

class Adapter:
    name: str                         # "google" | "yandex" | "2gis" | "demo"
    label: str                        # человекочитаемое, по-русски
    supports_bbox: bool = True        # у 2ГИС — False: поиск по городу

    def available(self) -> tuple[bool, str]:
        """(доступен, причина_если_нет). Проверка ключей/бинарников."""

    async def search(self, query: str, bbox: BBox, limit: int = 60) -> list[Place]:
        """Реальный поиск. Ошибку сети/парсинга кидать AdapterError."""
```

Правила:
- Никаких фейковых данных. Если источник недоступен — `AdapterError` с текстом причины.
- `raw` — только то, что реально вернул источник.
- Телефон нормализуется в `db.py`, адаптер отдаёт как есть.
- Таймаут на весь `search` — не больше 120 секунд. Исключение — браузерный
  2ГИС: он обходит город целиком и ограничен `TWOGIS_TIMEOUT` (280 с).

`backend/adapters/registry.py`:

```python
def all_adapters() -> dict[str, Adapter]     # {"google": GoogleAdapter(), ...}
def get_adapters(names: list[str]) -> list[Adapter]
```

---

## 4. db.py

```python
DB_PATH: Path
def init_db() -> None
def connect() -> sqlite3.Connection       # row_factory=sqlite3.Row, WAL
def upsert_place(conn, place: Place) -> tuple[str, bool]   # (business_id, is_new)
def list_leads(conn, **filters) -> tuple[int, list[dict]]
def get_lead(conn, business_id: str) -> dict | None
def set_status(conn, business_id: str, status: str | None = None,
               is_favorite: bool | None = None, note: str | None = None) -> bool
def set_site_audit(conn, business_id: str, audit: dict) -> None
def fill_empty_fields(conn, business_id: str, values: dict) -> list[str]
def add_location(conn, business_id, source, source_id, address,
                 phone="", lat=None, lon=None, url="") -> bool
def add_fact(conn, business_id: str, kind: str, value: str,
             source_url: str) -> bool
def stats(conn) -> dict
def find_duplicate_groups(conn) -> list[dict]
def merge_duplicate_groups(conn) -> int
def backup_database(destination: Path) -> Path
```

Схема:

```sql
business(
  id TEXT PK, source TEXT, source_id TEXT, name TEXT, category TEXT,
  address TEXT, lat REAL, lon REAL, phone TEXT, phone_norm TEXT,
  website TEXT, rating REAL, reviews_count INTEGER, price_range TEXT,
  hours TEXT, photos_json TEXT, site_json TEXT, url TEXT,
  name_key TEXT, compact_key TEXT, brand_key TEXT,
  address_key TEXT, phone_keys TEXT, site_key TEXT,
  status TEXT DEFAULT 'new',        -- new|in_progress|called|rejected
  is_favorite INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  is_demo INTEGER DEFAULT 0,
  first_seen TEXT, updated_at TEXT
)
business_source(business_id, source, source_id, url, payload_json,
                PRIMARY KEY(business_id, source, source_id))
business_location(business_id, source, source_id, address, phone, lat, lon, url,
                  PRIMARY KEY(business_id, source, source_id))
fact(id INTEGER PK, business_id, kind, value, source_url, fetched_at)
scan_job(id TEXT PK, query TEXT, queries_json TEXT, query_index INTEGER,
         city TEXT, bbox_json TEXT, cell_deg REAL,
         sources_json TEXT, status TEXT, cells_total INTEGER,
         cells_done INTEGER, leads_found INTEGER, error TEXT,
         created_at TEXT, updated_at TEXT)
scan_cell(id INTEGER PK, job_id TEXT, ix INTEGER, iy INTEGER,
          bbox_json TEXT, status TEXT, found INTEGER, error TEXT,
          started_at TEXT, finished_at TEXT)
```

Дедуп в `upsert_place` (`backend/dedup.py`):
1. та же запись источника (`source`+`source_id`);
2. сеть — `brand_key` (название без пробелов и оргформ, кроме родовых слов);
3. та же точка — телефон, адрес, сайт, название и расстояние; координаты —
   слабый сигнал, разные дома — стоп-сигнал;
4. демо и реальные записи не склеиваются.

При совпадении — дозаполнить пустые поля, добавить строку в `business_source`
и точку в `business_location`, `is_new=False`. `get_lead` возвращает
`locations_count` и `locations` (адрес, телефон, координаты, `sources`, url).
Одна точка, пришедшая из 2ГИС и Яндекса с разным написанием адреса, схлопывается
в одну строку, оба источника остаются в `sources`.
`get_lead` также парсит `site_json` в `site_audit` и добавляет `intel`
(`backend/intel.py`). Это чистый расчёт на чтении: без сети и без записи в БД.
Пустая карточка без названия/контактов получает низкий потенциал, а не «высокий».
Склейка накопленной базы — `python -m backend.merge_duplicates [--apply]`.

`fact.kind`: `email | phone | price | photo | social | director | description | inn | hours | address`.
`fact.value` — точный текст со страницы. `source_url` — страница, откуда взято. Обязательно.

---

## 5. Сканер

```python
class ScanManager:
    def start(self, queries: str | list[str], bbox: BBox, cell_deg: float,
              sources: list[str], limit_per_cell: int = 40,
              city: str | None = None) -> str
    def pause(self, job_id: str) -> None
    def resume(self, job_id: str) -> None
    def stop(self, job_id: str) -> None
    def job(self, job_id: str) -> dict | None
    def jobs(self) -> list[dict]
    def subscribe(self) -> asyncio.Queue
    def unsubscribe(self, q: asyncio.Queue) -> None

SCANNER: ScanManager   # глобальный синглтон
```

События в SSE (`type`):
- `cell` — `{job_id, ix, iy, status, found}`
- `lead` — `{lead: {...краткая карточка...}, is_new: bool, job_id}`
- `job` — `{job_id, status, cells_total, cells_done, leads_found,
  queries_total, query_index, current_query, reset_cells?}`
- `error` — `{job_id, source, message}`
- `hello` — при подключении

Сканирование: bbox делится на квадраты `cell_deg`. Ячейки обрабатываются с ограничением параллелизма (3). По каждой ячейке — `adapter.search(query, cell_bbox, limit)`.
Каждый запрос из списка — отдельный проход по всей сетке. Перед новым проходом
ячейки сбрасываются в `pending`, а в SSE приходит `reset_cells: true`; найденные
лиды в базе не удаляются. Поля `queries_total`, `query_index`, `current_query`
позволяют UI показать «Категория 2 из 5».
Источники с `supports_bbox=False` (2ГИС) запускаются параллельно с ячейками,
фильтруют результаты по bbox задания и получают город из `city` запроса.

---

## 6. HTTP API

| Метод | Путь | Тело / параметры | Ответ |
|---|---|---|---|
| GET | `/` | — | `frontend/index.html` |
| GET | `/api/health` | — | `{ok, adapters:[{name,label,available,reason}], demo, ai:{configured,model,base_url}}` |
| GET | `/api/categories` | — | `{groups:[{id,title,categories:[{id,label,query}]}], total, default, popular}` |
| POST | `/api/scan/estimate` | `{categories?, query?, city?, bbox?, cell_deg?}` | `{queries, cells, requests, bbox}` |
| POST | `/api/scan` | `{categories?, query?, city?, bbox?, cell_deg?, sources[], limit_per_cell?}` | `{job_id, cells, queries, bbox}` |
| GET | `/api/jobs` | — | `{items:[...]}` |
| GET | `/api/jobs/{id}` | — | job + `cells:[[ix,iy,status,found],...]` |
| POST | `/api/jobs/{id}/pause` | — | `{ok}` |
| POST | `/api/jobs/{id}/resume` | — | `{ok}` |
| POST | `/api/jobs/{id}/stop` | — | `{ok}` |
| GET | `/api/leads` | `status, source, q, fav, site=no\|yes, sort, limit, offset` | `{total, items:[...]}` |
| GET | `/api/leads/{id}` | — | полная карточка + `facts`, `sources`, `locations`, `site_audit`, `intel` |
| PATCH | `/api/leads/{id}` | `{status?, is_favorite?, note?}` | `{ok, lead}` |
| POST | `/api/leads/{id}/enrich` | — | `{status, message, added, errors, site_audit, website, lead}` |
| POST | `/api/leads/{id}/pitch` | — | `{ok, source:"ai"\|"template", text, model, error}` |
| GET | `/api/stats` | — | `{total, by_status:{...}, by_source:{...}, favorites, with_site, no_site}` |
| GET | `/api/events` | — | `text/event-stream` |

Краткая карточка лида (`items[]`, SSE `lead`):
```json
{"id":"..","name":"..","category":"..","address":"..","phone":"..",
 "website":"..","rating":4.7,"reviews_count":120,"source":"google",
 "sources":["google","2gis"],"status":"new","is_favorite":0,
 "is_demo":0,"has_site":true,"lat":57.1,"lon":65.5,"updated_at":".."}
```

Полная карточка добавляет: `photos:[...]`, `price_range`, `hours`, `note`,
`facts:{email:[{value,source_url,fetched_at}], price:[...], director:[...], ...}`,
`source_links:[{source,url}]`, `first_seen`.

---

## 7. Enrichment и аналитика карточки

```python
async def enrich(business_id: str) -> dict:
    """Контакты из payload'ов источников + обход сайта + аудит.
    Возвращает {status, message, added:{kind:count}, errors:[...],
                site_audit: dict|None, website: str}"""
```

Правила:
- Сначала читаются сохранённые `business_source.payload_json` через
  `source_facts.extract_source_data(payload, source)`. Это работает без сети и
  без сайта: телефоны, email, соцсети, часы, описание, рейтинг, отзывы, адрес,
  координаты.
- `fill_empty_fields` заполняет только пустые поля бизнеса: ручные и уже
  собранные данные не перезаписываются. Адрес из 2ГИС не собирается из
  «Город/Район/Микрорайон» — улицы в payload'е нет. Адрес и координаты Яндекса
  добавляются в `business_location`; пустая техническая точка получает адрес,
  а не дублируется.
- Если сайта нет после разбора payload'ов — `status="no_site"`, это не ошибка.
  Если вместо сайта соцсеть (`vk.com`, `t.me`, `wa.me`, `ok.ru`,
  `instagram.com`, `youtube.com`) — `status="social_only"`, сайт не обходится.
- Для обычного `http(s)` сайта: максимум 6 страниц с домена (главная +
  `contacts`, `about`, `price`, `menu`, `услуги`), таймаут 10 сек на страницу,
  общий бюджет 60 сек.
- `Director`: искать в тексте `директор|руководитель|генеральный директор|ИП` + ФИО рядом.
  Сохранять только если рядом реально есть имя/ФИО. Никаких догадок.
- `price`: только явные цены (`1 500 ₽`, `1500 руб`, `от 2 000 рублей`) в контексте услуг/меню.
- `photo`: `og:image` и крупные `<img>` с абсолютным URL; варианты одного
  файла через imgproxy/ресайзер схлопываются по исходному URL, максимум 12.
- `social`: ссылки на vk.com, t.me, wa.me, ok.ru.
- `inn`: 10/12 цифр рядом с `ИНН`.
- Каждый факт — с `source_url`.
- Дедуп фактов по `(kind, value)`; телефоны — по нормализованному номеру,
  фото — по каноническому URL.
- Технический аудит главной страницы сохраняется в `business.site_json`
  (`set_site_audit`) и отдаётся как `lead.site_audit`: `url, final_url, status,
  ok, https, redirected, elapsed_ms, html_kb, title, description,
  has_viewport, has_h1, has_form, has_booking, has_menu, has_prices,
  has_contacts, cms, error, checked_at`. Если сайт не открылся, сохраняется
  `error`, чтобы карточка могла показать «сайт недоступен».

`backend/intel.py` (чистые функции, без сети/БД):

```python
def build_intel(lead: dict) -> dict
def build_pitch(lead: dict, intel: dict) -> str
```

`intel` содержит `score` (5..98), `label`, `summary`, `hooks`, `strengths`,
`gaps`, `offer{title,why,bullets}`, `pitch`, `contacts` (phone/whatsapp/
telegram/vk/website/email, дедуп по href), `site{has_site,url,social_only,
checks,score}`. Оценка выше для бизнеса без нормального сайта, сети и хорошего
рейтинга; для сильного сайта с HTTPS, мобильной версией, описанием и формой —
ниже. Только факты из карточки, без выдуманных цифр и гарантий.

`backend/ai.py` (опционально):

- `status() -> {configured, model, base_url}`; ключ никогда не отдаётся.
- `build_messages(lead, intel) -> list[dict]` — системный промпт запрещает
  выдумывать имена, цены, гарантии и конкурентов.
- `async generate_pitch(lead, intel) -> {ok, text, error, model}`; без ключа
  `error="not_configured"`, сетевых запросов нет. Любая ошибка возвращается
  словарём, исключения наружу не выходят.
- Переменные: `LLM_API_KEY` (или `OPENAI_API_KEY`), `LLM_BASE_URL`
  (по умолчанию `https://api.openai.com/v1`), `LLM_MODEL` (`gpt-4o-mini`),
  `LLM_TIMEOUT` (25 сек, 5..60). Ключ читается при вызове.
- `POST /api/leads/{id}/pitch` при отсутствии ключа или сбое возвращает
  `source="template"` и `intel.pitch`; приложение работает без ИИ.

`geocode.py`:
```python
async def city_bbox(city: str) -> tuple[BBox, str]:  # (bbox, display_name)
```
Nominatim, `User-Agent: PARSER/1.0`, таймаут 10 с, `limit=1`.

---

## 8. Frontend

- Один `index.html`, один `styles.css`, один `app.js`. Без сборки, без CDN (офлайн).
- Русский язык. Тёмная тема: `#0d0f11`, `#15181c`, `#1d2127`, текст `#e6e8ea`, акцент `#7c8794`.
- Три зоны: слева панель сканирования + сетка ячеек, центр список лидов, справа карточка.
- «Что ищем» — кнопка, открывающая модальное меню категорий: поиск, переключатели,
  «Хиты», «Все», «Сбросить», выбор группы и свой запрос. Выбор хранится в
  `localStorage` (`parser.selectedCategories.v1`).
- Под формой показывается оценка объёма (`/api/scan/estimate`), в прогрессе —
  «Категория N из M» и кнопка «Стоп» для долгого скана.
- Мини-меню на каждой карточке: `★` избранное, `☎` отзвонено, `⚙` в работе, `✕` отказ.
- Карточка показывает продажный блок из `lead.intel`: потенциал и зацепки,
  оффер, готовое сообщение с кнопками «Копировать» и «Сгенерировать с ИИ»,
  быстрые контакты, аудит сайта из `lead.site_audit`.
- «Обогатить» вызывает `/enrich`; сообщение результата сохраняется после
  перерисовки карточки. Для `no_site`/`social_only` это не ошибка.
- ИИ не обязателен: без ключа кнопка показывает шаблон и подпись «шаблон из
  карточки»; при наличии ключа — текст от модели и её имя.
- Показ источника и бейджа `ДЕМО` для `is_demo`.
- Пустые поля: писать «нет данных», не придумывать.
- Фото: `<img loading="lazy">` с `onerror` → скрыть.
- SSE через `EventSource('/api/events')`, при обрыве — реконнект.
- Никаких inline-скриптов, кроме подключения `app.js`.

---

## 9. Запреты

1. Не выдумывать данные о бизнесе. Пусто — значит пусто.
2. Не писать в чужие файлы.
3. Не добавлять зависимости вне списка.
4. Не использовать платные API без ключа из переменной окружения.
5. Не логировать персональные данные в файлы.
