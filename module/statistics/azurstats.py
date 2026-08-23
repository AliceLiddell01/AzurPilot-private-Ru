"""Сбор снимков наград и локальный разбор данных AzurStats.

Распознанные предметы сохраняются через production application-layer сервис
в PostgreSQL. CSV создаётся только явной командой экспорта WebUI.
"""

import threading
import os
import time
import uuid
from datetime import datetime
from dataclasses import asdict

import numpy as np

from module.application.errors import StorageError
from module.application.runtime_storage import get_runtime_storage
from module.base.utils import save_image
from module.logger import logger
from module.statistics.utils import pack


class DropImage:
    """Собрать снимки наград и зафиксировать их при выходе из контекста."""

    def __init__(self, stat, genre, save, local, info=''):
        """Сохранить параметры обработки набора снимков."""
        self.stat = stat
        self.genre = str(genre)
        self.save = bool(save)
        self.local = bool(local)
        self.info = info
        self.images = []
        self.combat_count = 0

    def add(self, image):
        """Добавить снимок к текущему набору."""
        if self:
            self.images.append(image)
            logger.info(
                f'Данные о наградах добавлены: genre={self.genre}, amount={self.count}')

    def set_combat_count(self, count):
        self.combat_count = count

    def handle_add(self, main, before=None):
        """Дождаться стабильного кадра и добавить новый снимок."""
        if before is None:
            before = main.config.WAIT_BEFORE_SAVING_SCREEN_SHOT

        if self:
            main.handle_info_bar()
            main.device.sleep(before)
            main.device.screenshot()
            self.add(main.device.image)

    def clear(self):
        self.images = []

    @property
    def count(self):
        return len(self.images)

    def __bool__(self):
        return self.save or self.local

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self:
            self.stat.commit(images=self.images, genre=self.genre,
                             save=self.save, local=self.local, info=self.info, combat_count=self.combat_count)


class AzurStats:
    """Сохранять снимки и фиксировать распознанные награды в PostgreSQL."""

    TIMEOUT = 20
    LOCAL_MEOW_CSV = './log/azurstat_meowofficer_farming.csv'
    LOCAL_GENRES = {'opsi_meowfficer_farming'}
    _record_lock = threading.Lock()

    def __init__(self, config):
        """Связать сборщик с конфигурацией экземпляра."""
        self.config = config

    meowofficer_farming_labels = [
        'Уровень коррозии',
        'Время последней записи',
        'Эффективные циклы',
        'Средние жёлтые монеты за цикл',
        'Средние золотые детали за цикл',
        'Средние координаты Бездны за цикл',
        'Средние скрытые координаты за цикл',
    ]
    meowofficer_farming_map = [
        'OperationCoin',
        'Plate',
        'CoordinateAbyssal',
        'CoordinateObscure'
    ]
    unit_combat_count = {
        1: 2,
        2: 2,
        3: 2,
        4: 3,
        5: 3,
        6: 3
    }

    @staticmethod
    def _insert_local_opsi_items(instance, rows):
        if not rows:
            return 0
        return get_runtime_storage().record_opsi_items(instance, tuple(rows))

    @staticmethod
    def _load_local_opsi_items(instance='default', genre='opsi_meowfficer_farming'):
        rows = get_runtime_storage().opsi_items(instance, genre=genre)
        return [
            {
                'created_at': int(row.observed_at.timestamp()),
                'imgid': row.imgid,
                'server': row.server,
                'zone': row.zone,
                'zone_type': row.zone_type,
                'zone_id': row.zone_id,
                'hazard_level': row.hazard_level,
                'item': row.item_code,
                'amount': row.amount,
                'tag': row.tag,
                'genre': row.genre,
                'combat_count': row.combat_count,
            }
            for row in rows
            if row.observed_at is not None
        ]

    @staticmethod
    def _write_meowofficer_farming(data):
        header = ','.join(AzurStats.meowofficer_farming_labels)
        os.makedirs(os.path.dirname(AzurStats.LOCAL_MEOW_CSV), exist_ok=True)
        np.savetxt(
            AzurStats.LOCAL_MEOW_CSV,
            data,
            delimiter=',',
            header=header,
            comments='',
            fmt='%f',
            encoding='utf-8',
        )

    @staticmethod
    def get_meowofficer_farming(instance='default'):
        all_data = AzurStats._load_local_opsi_items(
            instance=instance,
            genre='opsi_meowfficer_farming',
        )
        out_data = np.zeros((6, len(AzurStats.meowofficer_farming_labels)))
        img_combat_counts = {}

        for row in all_data:
            imgid = row.get('imgid')
            h_level = row.get('hazard_level')
            if not h_level or h_level < 1 or h_level > 6:
                continue
                
            combat_count = row.get('combat_count', 0)
            if imgid not in img_combat_counts:
                img_combat_counts[imgid] = combat_count
                out_data[h_level - 1, 2] += combat_count
            
            item_name = row.get('item')
            amount = row.get('amount', 0)
            
            for i, item_prefix in enumerate(AzurStats.meowofficer_farming_map):
                if item_name.startswith(item_prefix):
                    out_data[h_level - 1, 3 + i] += amount
                    break
        current_time = int(datetime.timestamp(datetime.now()))

        for i in range(6):
            h = i + 1
            out_data[i, 0] = h
            out_data[i, 1] = current_time
            out_data[i, 2] /= AzurStats.unit_combat_count[h]

            if out_data[i, 2] > 0:
                for j in range(3, len(AzurStats.meowofficer_farming_labels)):
                    out_data[i, j] /= out_data[i, 2]

        return out_data

    @staticmethod
    def _ensure_local_parser():
        from module.azur_stats.scene.operation_siren import SceneOperationSiren
        return SceneOperationSiren

    @staticmethod
    def _parse_local_opsi_items(image, imgid, genre, combat_count):
        SceneOperationSiren = AzurStats._ensure_local_parser()
        scene = SceneOperationSiren()
        scene.load_file(image)
        scene.__dict__['imgid'] = imgid
        rows = []
        created_at = int(time.time())

        for item in scene.parse_scene():
            row = asdict(item)
            row['imgid'] = imgid
            row['genre'] = genre
            row['combat_count'] = int(combat_count or 0)
            row['created_at'] = created_at
            rows.append(row)

        return rows

    def _record_local(self, image, genre, filename, combat_count):
        if genre not in ['opsi_meowfficer_farming']:
            return False

        imgid = f"{os.path.splitext(os.path.basename(filename))[0][:8]}{uuid.uuid4().hex[:8]}"
        try:
            rows = self._parse_local_opsi_items(image, imgid, genre, combat_count)
            if not rows:
                logger.warning('Локальный разбор AzurStats пропущен: строки предметов Operation Siren не извлечены')
                return False
            instance = getattr(self.config, 'config_name', None) or 'default'
            inserted = self._insert_local_opsi_items(instance, rows)
            logger.info(f'Локальный разбор AzurStats завершён, строк: {inserted}')
            return True
        except StorageError:
            raise
        except Exception as e:
            logger.warning(f'Не удалось выполнить локальный разбор AzurStats: {e}')
            return False

    def _save(self, image, genre, filename):
        """Сохранить изображение в выбранную подпапку."""
        try:
            folder = os.path.join(
                str(self.config.DropRecord_SaveFolder), genre)
            os.makedirs(folder, exist_ok=True)
            file = os.path.join(folder, filename)
            save_image(image, file)
            logger.info(f'Изображение сохранено: {file}')
            return True
        except Exception as e:
            logger.exception(e)

        return False

    def commit(self, images, genre, save=False, local=False, info='', combat_count=0):
        """Объединить снимки, сохранить их и при необходимости распознать."""
        if len(images) == 0:
            return False

        save, local = bool(save), bool(local)
        logger.info(
            f'Фиксация данных о наградах: genre={genre}, amount={len(images)}, save={save}, local={local}')
        image = pack(images)
        now = int(time.time() * 1000)

        if info:
            filename = f'{now}_{info}.png'
        else:
            filename = f'{now}.png'

        if save:
            save_thread = threading.Thread(
                target=self._save, args=(image, genre, filename))
            save_thread.start()

        if local:
            logger.info(f'Запуск локального разбора AzurStats, тип={genre}')
            with self._record_lock:
                self._record_local(image, genre, filename, combat_count)

        return True

    def new(self, genre, method=None, save=False, local=None, info=''):
        """Создать контекст сбора снимков для указанной категории."""
        method_value = None
        if isinstance(method, bool):
            save = save or method
            method = None
        if method is not None:
            method_value = str(method)
            save = save or 'save' in method_value
        if local is None:
            if method_value is None:
                local = genre in self.LOCAL_GENRES
            else:
                local = 'upload' in method_value and genre in self.LOCAL_GENRES
        return DropImage(stat=self, genre=genre, save=save, local=local, info=info)
