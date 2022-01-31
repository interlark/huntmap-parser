#!/usr/bin/env python3

# Author: interlark@gmail.com
# Description: HuntMap.Ru Parser

import json
import logging
import os
import time
from collections import OrderedDict
from glob import glob
from shutil import rmtree
from sys import platform

import geojson
from bs4 import BeautifulSoup
from pyproj import Transformer
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform

logging.getLogger('seleniumwire').setLevel(logging.ERROR)

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from seleniumwire import webdriver

# Directories
OUT_DIR = 'result'  # Директория для geojson файлов

# Browser parameters
BROWSER_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.131 Safari/537.48'
BROWSER_HEADLESS = False
BROWSER_LOAD_IMAGES = True
BROWSER_PAGE_WAIT = 15  # wait (secs) for all async requests on a map page get completed

# Конвертирование систем координат
GEO_CONVERT_COORDINATES = True  # Флаг для конвертации координат (True - сконвертировать, False - оставить как есть)
GEO_SOURSE_SRS = 3857  # EPSG (исходная), 3857: Web Mercator projection (Google Maps, OpenStreetMap, Web related stuff)
GEO_TARGET_SRS = 4326  # EPSG (желаемая), 4326: WGS 84 aka World Geodetic System (GPS, Navigation, etc...)

# URLs
URL_HUNTMAP_INDEX = 'https://huntmap.ru/spisok-gotovyh-kart-ohotugodij-regionov-rossii'

# Selectors
SELECTOR_INDEX_LINKS_ROOT = '.wpb_text_column.wpb_content_element > .wpb_wrapper'
SELECTOR_MAP_IFRAME = '#kosmosnimki > iframe'

# Other
GENERATE_MERGED_FILES = False
GEOJSON_ENCODING = 'utf-8'

def get_index_dict(driver):
    '''
    Получение dict следующего типа:
    {
        Округ_1: {
            Область_1: URL_1,
            Область_2: URL_2,
            ...
        },
        ...
    }

    :param driver: WebDriver
    '''
    driver.get(URL_HUNTMAP_INDEX)

    # get index links
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    root_item = soup.select_one(SELECTOR_INDEX_LINKS_ROOT)
    root_children = [x for x in root_item.children if x.name is not None]
    index_items = {}
    i = 0
    while i < len(root_children)-3:
        elem_h2 = root_children[i]
        elem_p = root_children[i+1]
        elem_ul = root_children[i+2]

        if elem_h2.name == 'h2' and elem_p.name == 'p' and elem_ul.name == 'ul':
            index_items[elem_h2.get_text()] = {a.get_text(): a['href'] for a in elem_ul.find_all('a')}
        
        i += 1

    return index_items

def parse_page(url, driver):
    '''
    Парсинг гео-данных со страницы.

    :param url: URL страницы с картой
    :param driver: WebDriver
    '''

    logging.info(f'Ожидание загрузки запросов: {BROWSER_PAGE_WAIT} секунд')

    driver.get(url)

    # scroll to the map iframe, just in case...
    try:
        driver.execute_script('arguments[0].scrollIntoView({behavior: "smooth"});', driver.find_element(By.CSS_SELECTOR, SELECTOR_MAP_IFRAME))
    except NoSuchElementException:
        pass  # no biggie
    
    time.sleep(BROWSER_PAGE_WAIT)

    data_requests = [req for req in driver.requests if req.host == 'maps.kosmosnimki.ru'\
        and req.path == '/TileSender.ashx' and req.response]

    data_responses = {}
    for req in data_requests:
        try:
            resp = req.response.body.decode('utf8')
            
            # remove jsonp wrapper
            while resp[0] != '(':
                resp = resp[1:]
            resp = resp.strip('()')
            
            data = json.loads(resp)
            data_responses[req.id] = data
        except json.JSONDecodeError:
            logging.warning('Неудачная попытка парсинга геоданных')

    # clear requests
    del driver.requests

    return data_responses

def save_result(data, county, region, output_path):
    '''
    Сохранение промежуточных файлов.

    :param data: Словарь со слоями и geojson features 
    :param county: Округ
    :param region: Регион
    :param output_path: Директория выходных файлов
    '''
    county_dir = os.path.join(output_path, county)
    region_dir = os.path.join(county_dir, region)

    if not os.path.isdir(output_path):
        os.mkdir(output_path)

    for layer_title, features in data.items():
        if not os.path.isdir(county_dir):
            os.mkdir(county_dir)
        if not os.path.isdir(region_dir):
            os.mkdir(region_dir)
        result_path = os.path.join(region_dir, f'{layer_title}.geojson')
        with open(result_path, 'w', encoding=GEOJSON_ENCODING) as f_json:
            features_collection = geojson.FeatureCollection(features)
            geojson.dump(features_collection, f_json, ensure_ascii=False, indent=4)

    if GENERATE_MERGED_FILES:
        # extra effort to combine all features into one file
        all_features_collection = geojson.FeatureCollection(sum(data.values(), []))
        all_merged_path = os.path.join(region_dir, 'merged.geojson')
        with open(all_merged_path, 'w', encoding=GEOJSON_ENCODING) as f_json:
            geojson.dump(all_features_collection, f_json, ensure_ascii=False)

def merge_result(output_path):
    '''
    Компиляция промежуточных файлов в один.

    :param output_path: Директория выходных файлов
    '''
    all_features = []
    for doc_file in glob(os.path.join(output_path, '*', '*', 'merged.geojson')):
        with open(doc_file, 'r') as f_doc:
            all_features.extend(geojson.load(f_doc)['features'])

    all_feature_collection = geojson.FeatureCollection(all_features)
    with open(f'{output_path}/merged.geojson', 'w', encoding=GEOJSON_ENCODING) as f_compiled:
        geojson.dump(all_feature_collection, f_compiled, ensure_ascii=False)

def build_geojson_features(docs):
    '''
    Убираем метаданные и компонуем оставшиеся ответы сервера в geojson features следующим образом:
    {
        ИмяСлоя: [GeoJsonFeature,...],
        ...
    }

    :param docs: Ответы сервера
    '''
    meta_docs, layer_docs = {}, {}
    attr_mapping = {}
    title_mapping = {}

    for k, v in docs.items():
        if 'values' in v:
            layer_docs[k] = v
        else:
            meta_docs[k] = v
    
    # let's get mapping between layers and attributs they contains inside
    def find_attrs(doc):
        if isinstance(doc, dict):
            for v in doc.values():
                if isinstance(v, dict):
                    find_attrs(v)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, (dict, list)):
                            find_attrs(x)
            
            if 'LayerID' in doc and 'attributes' in doc and 'name' in doc and 'title' in doc:
                # TODO: assert name == LayerID
                attr_mapping[doc['name']] = doc['attributes']
                title_mapping[doc['name']] = doc['title']
                
        if isinstance(doc, list):
            for x in docs:
                find_attrs(x)

    for doc in meta_docs.values():
        find_attrs(doc)

    # collected layers and features
    layers = {}

    # now we're gonna convert geojson and assign attributes according to their mappings
    for doc in layer_docs.values():
        layer_name = doc['LayerName']
        if layer_name in attr_mapping:
            layer_attr_names = attr_mapping[layer_name]
        else:
            layer_attr_names = [f'property_{x+1}' for x in range(128)]
        
        geojson_features = []

        for v in doc['values']:
            v_properties = OrderedDict()  # for py < 3.7
            v_geom = None 
            for i, vv in enumerate(v, -1):
                if i == -1:
                    continue  # skip index
                if not isinstance(vv, dict):
                    v_properties[layer_attr_names[i]] = vv
                else:
                    try:
                        geom = shape(vv)
                        if GEO_CONVERT_COORDINATES:
                            project = Transformer.from_crs(f'EPSG:{GEO_SOURSE_SRS}', f'EPSG:{GEO_TARGET_SRS}', always_xy=True)
                            geom = shapely_transform(project.transform, geom)
                        v_geom = shapely_mapping(geom)
                    except ValueError as e:
                        logging.warning('Ошибка в структуре геоданных у объекта с аттрибутами [' +\
                            f', '.join(f'{attr}:{val}' for attr, val in v_properties.items()) + ']:\n' + str(e))
                        continue  # Пропуск объекта

            geo_feature = geojson.Feature(geometry=v_geom, properties=v_properties)
            geojson_features.append(geo_feature)
        
        if layer_name not in layers:
            layers[layer_name] = []

        layers[layer_name].extend(geojson_features)

    result_data = {}
    for layer_name, features in layers.items():
        result_data[title_mapping[layer_name]] = features

    return result_data


def run(output_path):
    '''
    Запуск парсера.

    :param output_path: Директория выходных файлов
    '''

    seleniumwire_options = {
        'disable_encoding': True,
    }

    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument('--start-maximized')
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

    if not BROWSER_LOAD_IMAGES:
        prefs = {"profile.managed_default_content_settings.images": 2}
        chrome_options.add_experimental_option("prefs", prefs)

    if BROWSER_HEADLESS:
        driver_options = Options()
        driver_options.add_argument('user-agent={}'.format(BROWSER_USER_AGENT))
        driver_options.add_argument("--headless")

        driver = webdriver.Chrome(options=driver_options, chrome_options=chrome_options, seleniumwire_options=seleniumwire_options)
    else:
        driver = webdriver.Chrome(chrome_options=chrome_options, seleniumwire_options=seleniumwire_options)

    logging.info(f'Получение индекса карт...')
    index_dict = get_index_dict(driver)

    for county, regions in index_dict.items():
        for region, url in regions.items():
            logging.info(f'Парсинг геоданных [{county}] ({region})')
            data = parse_page(url, driver)
            data = build_geojson_features(data)
            save_result(data, county, region, output_path)

    if GENERATE_MERGED_FILES:
        logging.info(f'Компиляция геоданных в один файл...')
        merge_result(output_path)

    logging.info(f'Работа завершена')


if __name__ == '__main__':
    here = os.path.abspath(os.path.dirname(__file__))

    if platform == "linux" or platform == "linux2":  # Linux
        os.environ['PATH'] += os.pathsep + os.path.join(here, 'drivers/linux64')
    elif platform == "darwin":  # OS X
        os.environ['PATH'] += os.pathsep + os.path.join(here, 'drivers/mac64')
    elif platform == "win32":  # Windows
        os.environ['PATH'] += os.pathsep + os.path.join(here, 'drivers/win32')
    else:
        raise OSError('ОС не определена')

    output_path = os.path.join(here, OUT_DIR)
    logging.basicConfig(level=logging.getLevelName('INFO'), format='[%(name)s] %(levelname)s: %(message)s')
    logging.info(f'Запуск парсера HuntMap... (Based on Chrome 97.0)\nДиректория: {output_path}')

    if os.path.isdir(output_path):
        while True:
            ret = input(f'Папка {OUT_DIR} уже существует. Удалить? [Yy/Nn] ')
            if ret.upper() == 'Y':
                rmtree(output_path)
                break
            elif ret.upper() == 'N':
                exit(1)
    
    run(output_path)

