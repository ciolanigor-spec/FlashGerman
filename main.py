import os
import json
import csv
import re
import urllib.request
from urllib.parse import quote
import gzip
import ssl
import time
from html.parser import HTMLParser

import certifi

# ==========================================
# НАСТРОЙКИ (КОНФИГУРАЦИЯ)
# ==========================================
# Файлове и урли за сваляне
MAX_WORDS = None  # Set from the user's answer when the program starts.

FREQ_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/de/de_full.txt"
FREQ_FILE = "de_full.txt"

# This is extracted from the German-language Wiktionary, so its definitions
# (including ``glosses``) are in German.
KAIKKI_GZ_URL = "https://kaikki.org/dewiktionary/raw-wiktextract-data.jsonl.gz"
KAIKKI_GZ_FILE = "dewiktionary-raw-wiktextract-data.jsonl.gz"
KAIKKI_JSONL_FILE = "dewiktionary-raw-wiktextract-data.jsonl"

OUTPUT_CSV = None
DWDS_CACHE_FILE = "dwds_definitions_cache.json"
DWDS_REQUEST_DELAY_SECONDS = 0.25


class DWDSDefinitionParser(HTMLParser):
    """Extracts definition text from a DWDS dictionary article."""

    def __init__(self):
        super().__init__()
        self.definitions = []
        self._definition_depth = 0
        self._current_definition = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get('class', '').split()
        if 'dwdswb-definition' in classes:
            self._definition_depth = 1
            self._current_definition = []
        elif self._definition_depth:
            self._definition_depth += 1

    def handle_data(self, data):
        if self._definition_depth:
            self._current_definition.append(data)

    def handle_endtag(self, tag):
        if not self._definition_depth:
            return
        self._definition_depth -= 1
        if self._definition_depth == 0:
            definition = ' '.join(''.join(self._current_definition).split())
            if definition:
                self.definitions.append(definition)


# ==========================================
# ФУНКЦИИ ЗА СВАЛЯНЕ И ОБРАБОТКА
# ==========================================

def download_file(url, destination):
    """Сваля файл от интернет с индикатор за напредъка."""
    if os.path.exists(destination):
        print(f"Файлът '{destination}' вече съществува. Прескачане на свалянето.")
        return

    print(f"Започва сваляне на '{destination}' от {url}...")

    def report_progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = downloaded * 100 / total_size
            mb_downloaded = downloaded / (1024 * 1024)
            mb_total = total_size / (1024 * 1024)
            print(f"\rНапредък: {percent:.1f}% ({mb_downloaded:.1f} MB / {mb_total:.1f} MB)", end="")
        else:
            mb_downloaded = downloaded / (1024 * 1024)
            print(f"\rСвалени: {mb_downloaded:.1f} MB", end="")

    # Use certifi's current certificate-authority bundle rather than relying on
    # a possibly missing or outdated bundle supplied by the Python installation.
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    block_size = 8192
    with urllib.request.urlopen(url, context=ssl_context) as response:
        total_size = int(response.headers.get('Content-Length', -1))
        with open(destination, 'wb') as output_file:
            block_num = 0
            while chunk := response.read(block_size):
                output_file.write(chunk)
                block_num += 1
                report_progress(block_num, block_size, total_size)
    print("\nСвалянето завърши успешно!")


def extract_gz_file(gz_path, jsonl_path):
    """Разархивира .gz файл до .jsonl."""
    if os.path.exists(jsonl_path):
        print(f"Разархивираният файл '{jsonl_path}' вече съществува. Прескачане.")
        return

    print(f"Разархивиране на '{gz_path}'...")
    with gzip.open(gz_path, 'rb') as f_in:
        with open(jsonl_path, 'wb') as f_out:
            chunk_size = 1024 * 1024  # 1MB чанкове
            while True:
                chunk = f_in.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
    print("Разархивирането завърши!")


def get_cefr_level(rank):
    """Определя приблизително ниво спрямо честотния ранк."""
    if rank <= 2000:
        return 'A1-B1'
    elif rank <= 5000:
        return 'B2'
    else:
        return 'C1-C2'


def clean_definition(text):
    """Изчиства излишно форматиране от дефиницията."""
    if not text:
        return ''
    text = re.sub(r'\(.*?\)', '', text)
    return text.strip()


def load_dwds_cache(cache_path):
    """Loads previously fetched DWDS definitions to avoid repeat requests."""
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, 'r', encoding='utf-8') as cache_file:
            return json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return {}


def save_dwds_cache(cache_path, cache):
    with open(cache_path, 'w', encoding='utf-8') as cache_file:
        json.dump(cache, cache_file, ensure_ascii=False, indent=2)


def get_dwds_definition(word, cache):
    """Returns up to three DWDS definitions for ``word``, or an empty string."""
    if word in cache:
        return cache[word]

    url = f"https://www.dwds.de/wb/{quote(word)}"
    request = urllib.request.Request(url, headers={'User-Agent': 'GermanVocabularyBuilder/1.0'})
    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, context=ssl_context, timeout=20) as response:
            html = response.read().decode('utf-8', errors='replace')
        parser = DWDSDefinitionParser()
        parser.feed(html)
        definition = ' | '.join(parser.definitions[:3])
    except (OSError, ValueError):
        definition = ''

    if definition:
        cache[word] = definition
    time.sleep(DWDS_REQUEST_DELAY_SECONDS)
    return definition


def add_dwds_definitions(entries):
    """Adds a cached DWDS definition to each final vocabulary entry."""
    cache = load_dwds_cache(DWDS_CACHE_FILE)
    print(f"Fetching DWDS definitions for {len(entries)} words...")
    for index, entry in enumerate(entries, start=1):
        entry['DWDS_Erklärung'] = get_dwds_definition(entry['Wort'].split(' ', 1)[-1], cache)
        if index % 100 == 0:
            print(f"Fetched DWDS definitions for {index} words...")
            save_dwds_cache(DWDS_CACHE_FILE, cache)

    save_dwds_cache(DWDS_CACHE_FILE, cache)


def load_frequency_list(freq_path):
    """Зарежда честотния списък и създава бърз речник {дума: rank}."""
    print(f"Зареждане на честотния списък от {freq_path}...")
    freq_dict = {}
    rank = 1

    with open(freq_path, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split()
            if not parts:
                continue
            word = parts[0]

            # Валидация за буквени знаци
            if not word.isalpha():
                continue

            if word not in freq_dict:
                freq_dict[word] = rank
                rank += 1

            # Зареждаме повече потенциални думи от прага, за да компенсираме филтрацията
    print(f"Заредени са {len(freq_dict)} кандидат-думи за филтриране.")
    return freq_dict


def process_kaikki_dictionary(jsonl_path, freq_dict):
    """Обработва големия JSONL файл ред по ред (streaming parsing)."""
    print(f"Обработка на речника {jsonl_path} ред по ред...")
    processed_words = {}
    line_counter = 0

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line_counter += 1
            if line_counter % 100000 == 0:
                print(f"Обработени {line_counter} реда...")

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # The German Wiktionary dump also contains entries in other
            # languages. Keep only German entries and their German glosses.
            if data.get('lang_code') != 'de':
                continue

            word = data.get('word', '')

            # Проверка дали думата фигурира в нашия честотен списък
            if word not in freq_dict and word.capitalize() not in freq_dict:
                continue

            pos = data.get('pos', '')
            # Филтрираме лични имена, съкращения, символи и топоними
            if pos in ['name', 'proper noun', 'symbol', 'character', 'num', 'abbrev']:
                continue

            senses = data.get('senses', [])
            is_inflected = False
            definitions = []

            for sense in senses:
                tags = sense.get('tags', [])
                # Игнорираме спрегнати/склонени форми
                if 'form-of' in tags or 'inflection-of' in tags:
                    is_inflected = True
                    break

                glosses = sense.get('glosses', [])
                if glosses:
                    definitions.extend(glosses)

            if is_inflected or not definitions:
                continue

            formatted_word = word
            word_type = pos.capitalize()

            # Член за съществителните (der/die/das)
            if pos == 'noun':
                word_type = 'Substantiv'
                genders = data.get('genders', [])
                if genders:
                    g = genders[0]
                    if g == 'm':
                        formatted_word = f"der {word}"
                    elif g == 'f':
                        formatted_word = f"die {word}"
                    elif g == 'n':
                        formatted_word = f"das {word}"
            elif pos == 'verb':
                word_type = 'Verb'
            elif pos == 'adj':
                word_type = 'Adjektiv'
            elif pos == 'adv':
                word_type = 'Adverb'

            # Извличане на синоними
            synonyms = set()
            for syn in data.get('synonyms', []):
                if syn.get('word'):
                    synonyms.add(syn.get('word'))

            for sense in senses:
                for syn in sense.get('synonyms', []):
                    if syn.get('word'):
                        synonyms.add(syn.get('word'))

            rank = freq_dict.get(word, freq_dict.get(word.capitalize(), 999999))

            if word not in processed_words or rank < processed_words[word]['Rank']:
                processed_words[word] = {
                    'Wort': formatted_word,
                    'Rank': rank,
                    'Sprachniveau_Est': get_cefr_level(rank),
                    'Wortart': word_type,
                    'Erklärung': clean_definition(definitions[0]),
                    'Synonyme': ', '.join(list(synonyms)[:5])
                }

    return processed_words


# ==========================================
# ГЛАВЕН СЦЕНАРИЙ
# ==========================================

def main():
    global MAX_WORDS, OUTPUT_CSV
    while True:
        try:
            MAX_WORDS = int(input("How many German words should be included? "))
            if MAX_WORDS > 0:
                break
            print("Please enter a positive whole number.")
        except ValueError:
            print("Please enter a positive whole number.")

    OUTPUT_CSV = f"german_vocabulary_top{MAX_WORDS}.csv"
    print(f"=== Автоматично генериране на немски речник за топ {MAX_WORDS} думи ===")

    # 1. Автоматично сваляне на източниците
    download_file(FREQ_URL, FREQ_FILE)
    download_file(KAIKKI_GZ_URL, KAIKKI_GZ_FILE)

    # 2. Разархивиране на речника
    extract_gz_file(KAIKKI_GZ_FILE, KAIKKI_JSONL_FILE)

    # 3. Зареждане на честотите
    freq_dict = load_frequency_list(FREQ_FILE)

    # 4. Обхождане на базата данни
    results_dict = process_kaikki_dictionary(KAIKKI_JSONL_FILE, freq_dict)

    # 5. Сортиране по ранк
    final_list = list(results_dict.values())
    final_list.sort(key=lambda x: x['Rank'])

    # 6. Ограничаване до MAX_WORDS и коригиране на ранга
    final_list = final_list[:MAX_WORDS]
    if len(final_list) < MAX_WORDS:
        print(
            f"Warning: only {len(final_list)} valid German headwords were found "
            f"for the requested {MAX_WORDS} words."
        )
    for idx, item in enumerate(final_list, start=1):
        item['Rank'] = idx
        item['Sprachniveau_Est'] = get_cefr_level(idx)

    # Add a second, independent definition from DWDS.
    add_dwds_definitions(final_list)

    # 7. Запис в CSV
    fieldnames = [
        'Wort', 'Rank', 'Sprachniveau_Est', 'Wortart', 'Erklärung',
        'DWDS_Erklärung', 'Synonyme'
    ]

    print(f"Записване на {len(final_list)} думи във файла '{OUTPUT_CSV}'...")
    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(final_list)

    print(f"\nГотово! Файлът '{OUTPUT_CSV}' е готов за използване.")


if __name__ == '__main__':
    main()
