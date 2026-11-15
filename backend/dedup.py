"""Сопоставление организаций между источниками.

Одна и та же кофейня у Google, Яндекса и 2ГИС может стоять в разных точках
карты, называться «SwipeCoffee» и «Swipe Coffee», а адрес приходить как
«Рязань, район Приокский, Дачная улица, 5» или «Дачная улица, 5».
Поэтому координаты — только один из сигналов, и самый слабый: решение
принимается по совокупности названия, адреса, телефона и сайта.

Правила намеренно консервативные: разные точки одной сети (одно название,
разные дома и телефоны) остаются разными карточками, потому что это разные
точки продаж, а не дубликаты.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Mapping
from urllib.parse import urlsplit

# Порог, начиная с которого считаем записи одной организацией.
MERGE_THRESHOLD = 85

# Латинские двойники кириллицы: «Mons сoffee» и «Mons coffee» — одно название.
_LOOKALIKE = str.maketrans(
    {
        "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m",
        "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
        "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԛ": "q",
        "ԝ": "w",
    }
)

_LEGAL_FORMS = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|нко|ao|llc|inc|ltd|gmbh)\b")

# (шаблон, канонический вид). Длинные шаблоны идут первыми.
_STREET_TYPES: tuple[tuple[str, str], ...] = (
    ("микрорайон", "мкр"),
    (r"мкр-?н", "мкр"),
    (r"мкр", "мкр"),
    ("набережная", "наб"),
    ("проспект", "пр"),
    (r"просп", "пр"),
    (r"пр-?кт", "пр"),
    (r"пр-?т", "пр"),
    ("площадь", "пл"),
    ("переулок", "пер"),
    ("бульвар", "бул"),
    (r"б-р", "бул"),
    ("проезд", "пр-д"),
    ("шоссе", "ш"),
    ("улица", "ул"),
    ("тупик", "туп"),
    ("аллея", "алл"),
)

# Слова, которые не влияют на адрес.
_ADDRESS_NOISE = re.compile(
    r"\b(дом|корпус|корп|строение|стр|литера|лит|офис|оф|помещение|пом|"
    r"квартира|кв|этаж|эт)\b\.?"
)

_REGION_WORDS = re.compile(
    r"^(г|город|гор|обл|область|респ|республика|край|район|р-н|округ|"
    r"поселок|посёлок|пгт|деревня|село|аул|станица)\b"
)

_SHARED_HOSTS = frozenset(
    {
        "uds.app", "instagram.com", "vk.com", "facebook.com", "t.me",
        "taplink.cc", "wa.me", "linktr.ee", "youtube.com", "ok.ru",
        "2gis.ru", "2gis.com", "yandex.ru", "google.com", "avito.ru",
    }
)

_HOUSE_RE = re.compile(r"^(?:\d+[а-яa-z]?(?:к\d+|с\d+)?|(?:к|с|стр)\d+)$")

# Родовые названия: «Кофейня» на двух улицах — это две разные кофейни, а не сеть.
GENERIC_NAMES = frozenset(
    {
        "кофейня", "кофейнясамообслуживания", "миникофейня", "кофе",
        "кафе", "кафетерий", "бистро", "бар", "ресторан", "траттория",
        "столовая", "буфет", "закусочная", "рюмочная", "чайная",
        "пекарня", "кондитерская", "пиццерия", "пицца", "бургерная",
        "шаурма", "шаурменная", "блинная", "хинкальная", "чебуречная",
        "пончиковая", "мороженое", "десерты", "выпечка", "фастфуд",
        "кофессобой", "кофес собой",
        "coffee", "cafe", "bar", "restaurant", "bakery", "pizza", "food",
    }
)


# --- название ---------------------------------------------------------------


def _fold_name(name: str) -> str:
    """Название без оргформ и пунктуации, но без замены двойников букв."""
    value = unicodedata.normalize("NFKC", str(name or "")).lower().replace("ё", "е")
    value = _LEGAL_FORMS.sub(" ", value)
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_name(name: str) -> str:
    """Каноническое название: регистр, ё, кавычки, оргформы, двойники букв."""
    return _fold_name(name).translate(_LOOKALIKE)


def compact_name(name: str) -> str:
    """Название без пробелов: «SwipeCoffee» == «Swipe Coffee»."""
    return normalize_name(name).replace(" ", "")


def brand_key(name: str) -> str:
    """Ключ сети: название без пробелов, если это не родовое слово.

    Пустая строка означает «по названию сеть не определяем» — такие записи
    склеиваются только как одна и та же точка.
    """
    compact = compact_name(name)
    folded = _fold_name(name).replace(" ", "")
    if len(compact) < 4 or folded in GENERIC_NAMES or compact in GENERIC_NAMES:
        return ""
    return compact


def name_similarity(left: str, right: str) -> float:
    """Похожесть названий 0..1 с учётом слипшихся слов."""
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    best = SequenceMatcher(None, a, b).ratio()
    ca, cb = a.replace(" ", ""), b.replace(" ", "")
    best = max(best, SequenceMatcher(None, ca, cb).ratio())
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        best = max(best, len(ta & tb) / len(ta | tb))
    return best


# --- телефон ----------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    """E.164-подобная нормализация. Пустая строка, если это не телефон.

    18 цифр подряд (склейка номера и добавочного) телефоном не считаем:
    иначе два разных бизнеса склеятся по мусору.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if len(digits) == 10:
        return "+7" + digits
    if len(digits) == 11:
        if digits[0] in "78":
            return "+7" + digits[1:]
        return "+" + digits
    return ""


def split_phones(raw: str) -> tuple[str, ...]:
    """Разобрать поле с несколькими номерами через ; , / | или двойной пробел."""
    if not raw:
        return ()
    found: list[str] = []
    for chunk in re.split(r"[;,/|]|\s{2,}", str(raw)):
        phone = normalize_phone(chunk)
        if phone and phone not in found:
            found.append(phone)
    return tuple(found)


def is_toll_free(phone: str) -> bool:
    """8-800 часто один на всю сеть — слабый признак для склейки точек."""
    return phone.startswith("+7800")


# --- сайт -------------------------------------------------------------------


def site_key(website: str) -> str:
    """Домен без www. Площадки-конструкторы (uds.app и соцсети) не считаем."""
    host = urlsplit(str(website or "").strip()).netloc.lower()
    if not host and str(website or "").strip():
        # Источники иногда отдают «site.ru» без схемы.
        host = urlsplit("//" + str(website).strip()).netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return ""
    for shared in _SHARED_HOSTS:
        if host == shared or host.endswith("." + shared):
            return ""
    return host


# --- адрес ------------------------------------------------------------------


def _street_type(segment: str) -> tuple[str, str]:
    """Выделить тип улицы из сегмента и вернуть (тип, остаток)."""
    for pattern, canonical in _STREET_TYPES:
        match = re.search(r"(?<![а-яa-z])" + pattern + r"(?![а-яa-z])", segment)
        if match:
            rest = segment[: match.start()] + " " + segment[match.end() :]
            return canonical, rest
    return "", segment


def _is_region(segment: str) -> bool:
    """Город, район, область — то, что не относится к самой улице."""
    if re.search(r"\d", segment):
        return False
    if _REGION_WORDS.match(segment):
        return True
    if not re.fullmatch(r"[а-я\- ]{3,}", segment):
        return False
    return not any(
        re.search(r"(?<![а-яa-z])" + pattern + r"(?![а-яa-z])", segment)
        for pattern, _ in _STREET_TYPES
    )


def address_parts(address: str) -> tuple[str, str]:
    """Вернуть (улица, полный адрес) в каноническом виде.

    «Рязань, район Приокский, Дачная улица, 5» и «Дачная улица, 5»
    дают одинаковую пару.
    """
    value = unicodedata.normalize("NFKC", str(address or "")).lower().replace("ё", "е")
    value = re.sub(r"[«»\"'`]", " ", value)
    segments = [seg.strip(" .") for seg in re.split(r"[,;]", value) if seg.strip(" .")]
    while len(segments) > 1 and _is_region(segments[0]):
        segments.pop(0)
    if not segments:
        return "", ""

    street_type = ""
    name_tokens: list[str] = []
    house_tokens: list[str] = []
    for segment in segments:
        segment = _ADDRESS_NOISE.sub(" ", segment)
        # «49 к 1» → «49 к1»: дом и корпус должны попасть в один токен.
        segment = re.sub(r"\bк\s*(\d)", r"к\1", segment)
        segment = re.sub(r"\b(?:стр|ст)\s*(\d)", r"с\1", segment)
        if not street_type:
            street_type, segment = _street_type(segment)
        else:
            _, segment = _street_type(segment)
        text = re.sub(r"[^0-9a-zа-я\- ]+", " ", segment)
        for token in text.split():
            if _HOUSE_RE.match(token):
                house_tokens.append(token)
            else:
                name_tokens.append(token)

    street = " ".join(
        part for part in (street_type, " ".join(sorted(set(name_tokens)))) if part
    )
    house = "".join(house_tokens)
    return street, f"{street} {house}".strip()


def split_address(address: str) -> tuple[str, str]:
    """(улица, дом). Дом пустой, если его нет в адресе."""
    street, full = address_parts(address)
    house = full[len(street):].strip() if street and full.startswith(street) else ""
    return street, house


def address_key(address: str) -> str:
    return address_parts(address)[1]


# --- расстояние -------------------------------------------------------------


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в метрах."""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# --- сравнение --------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """Минимум полей, нужных для сравнения. Не зависит от Place и sqlite3.Row."""

    name: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    lat: float | None = None
    lon: float | None = None
    category: str = ""
    is_demo: int = 0

    @classmethod
    def from_place(cls, place: object) -> "Candidate":
        return cls(
            name=getattr(place, "name", "") or "",
            address=getattr(place, "address", "") or "",
            phone=getattr(place, "phone", "") or "",
            website=getattr(place, "website", "") or "",
            lat=getattr(place, "lat", None),
            lon=getattr(place, "lon", None),
            category=getattr(place, "category", "") or "",
            is_demo=1 if getattr(place, "source", "") == "demo" else 0,
        )

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "Candidate":
        return cls(
            name=str(row["name"] or ""),
            address=str(row["address"] or ""),
            phone=str(row["phone"] or ""),
            website=str(row["website"] or ""),
            lat=row["lat"] if row["lat"] is not None else None,
            lon=row["lon"] if row["lon"] is not None else None,
            category=str(row["category"] or ""),
            is_demo=int(row["is_demo"] or 0),
        )


@dataclass(frozen=True)
class Match:
    """Результат сравнения двух записей."""

    score: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)
    blocked: bool = False

    @property
    def is_duplicate(self) -> bool:
        return not self.blocked and self.score >= MERGE_THRESHOLD


def _text(value: object) -> str:
    return str(value or "").strip()


def compare(left: Candidate, right: Candidate) -> Match:
    """Оценить, одна ли это организация. Больше score — больше уверенности."""
    score = 0
    reasons: list[str] = []

    name_a = normalize_name(left.name)
    name_b = normalize_name(right.name)
    similarity = name_similarity(left.name, right.name)
    if name_a and name_a == name_b:
        score += 55
        reasons.append("название")
    elif similarity >= 0.9:
        score += 45
        reasons.append(f"название ~{similarity:.0%}")
    elif similarity >= 0.75:
        score += 25
        reasons.append(f"название ~{similarity:.0%}")

    street_a, address_a = address_parts(left.address)
    street_b, address_b = address_parts(right.address)
    _, house_a = split_address(left.address)
    _, house_b = split_address(right.address)
    if address_a and address_a == address_b:
        score += 40
        reasons.append("адрес")
    elif street_a and street_a == street_b:
        score += 10
        reasons.append("улица")

    phones_a, phones_b = set(split_phones(left.phone)), set(split_phones(right.phone))
    strong_a = {p for p in phones_a if not is_toll_free(p)}
    strong_b = {p for p in phones_b if not is_toll_free(p)}
    if strong_a & strong_b:
        # Телефон — самый надёжный признак: одна точка, разные источники.
        score = max(score, 100)
        reasons.append("телефон")
    elif phones_a & phones_b:
        score += 15
        reasons.append("8-800")

    site_a, site_b = site_key(left.website), site_key(right.website)
    if site_a and site_a == site_b:
        # Общий сайт у сети не должен склеивать разные точки: учитываем,
        # только если точки рядом или адрес совпал.
        if address_a and address_a == address_b:
            score += 20
            reasons.append("сайт")
        elif _distance(left, right) is not None and _distance(left, right) <= 500:
            score += 20
            reasons.append("сайт")

    distance = _distance(left, right)
    if distance is not None:
        if distance <= 100:
            score += 35
        elif distance <= 300:
            score += 30
        elif distance <= 1000:
            score += 12
        reasons.append(f"{distance:.0f} м")

    if _text(left.category) and normalize_name(left.category) == normalize_name(right.category):
        score += 5

    blocked = False
    if left.is_demo != right.is_demo:
        # Демо-записи живут отдельно от реальных.
        blocked = True
        reasons.append("демо/реальные")
    elif house_a and house_b and house_a != house_b:
        # Разные дома — это разные точки, даже если название и телефон общие:
        # так устроены сети кофеен и вендинга.
        blocked = True
        reasons.append("разные дома")
    elif (
        strong_a
        and strong_b
        and not (strong_a & strong_b)
        and address_a
        and address_b
        and address_a != address_b
    ):
        # Разные телефоны и разные дома — это филиалы сети, а не дубликат.
        blocked = True
        reasons.append("разные телефоны и адреса")

    return Match(score=min(score, 200), reasons=tuple(reasons), blocked=blocked)


def _distance(left: Candidate, right: Candidate) -> float | None:
    if None in (left.lat, left.lon, right.lat, right.lon):
        return None
    return haversine(left.lat, left.lon, right.lat, right.lon)


def is_duplicate(left: Candidate, right: Candidate) -> bool:
    return compare(left, right).is_duplicate


def best_match(
    place: object, rows: Iterable[Mapping[str, object]]
) -> tuple[Mapping[str, object], Match] | None:
    """Самая уверенная пара для новой записи среди кандидатов из БД."""
    source = Candidate.from_place(place)
    best: tuple[Mapping[str, object], Match] | None = None
    for row in rows:
        match = compare(source, Candidate.from_row(row))
        if not match.is_duplicate:
            continue
        if best is None or match.score > best[1].score:
            best = (row, match)
    return best
