"""可独立测试的本地音乐库、日推和播放队列；不保存会过期的直链。"""
import copy
import datetime
import hashlib
import json
import os
import random
import tempfile


def song_key(song):
    return '%s:%s' % (song.get('platform') or 'wy', song.get('id', ''))


def clean_song(song):
    if not isinstance(song, dict) or not song.get('id') or not song.get('name'):
        return None
    return {k: copy.deepcopy(song.get(k, default)) for k, default in (
        ('id', ''), ('name', ''), ('singer', ''), ('album', ''),
        ('interval', ''), ('platform', 'wy'), ('extra', {}))}


class MusicLibrary:
    def __init__(self, directory):
        self.path = os.path.join(directory, 'music_library.json')
        self.favorites = []
        self.daily = {}
        self.load_error = ''
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            self.favorites = self._unique(data.get('favorites', []))
            daily = data.get('daily', {})
            if isinstance(daily, dict):
                self.daily = {str(k): self._unique(v) for k, v in daily.items()}
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            self.load_error = str(exc)

    @staticmethod
    def _unique(songs):
        out, seen = [], set()
        for raw in songs if isinstance(songs, list) else []:
            s = clean_song(raw)
            if s and song_key(s) not in seen:
                out.append(s)
                seen.add(song_key(s))
        return out

    def save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.library-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'favorites': self.favorites,
                           'daily': self.daily}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def contains(self, song):
        return any(song_key(s) == song_key(song) for s in self.favorites)

    def toggle(self, song):
        song = clean_song(song)
        if not song:
            raise ValueError('歌曲信息不完整，无法收藏')
        old = list(self.favorites)
        added = not self.contains(song)
        self.favorites = ([song] + self.favorites if added else
                          [s for s in self.favorites if song_key(s) != song_key(song)])
        try:
            self.save()
        except OSError:
            self.favorites = old
            raise
        return added

    def daily_key(self, platform, day=None, offset=0):
        return '%s:%s:%d' % (day or datetime.date.today().isoformat(),
                             platform, int(offset))

    def cached_daily(self, platform, day=None, offset=0):
        return copy.deepcopy(self.daily.get(self.daily_key(platform, day, offset), []))

    def daily_terms(self, platform, day=None, offset=0):
        """固定日期种子；优先从收藏歌手取灵感，再配一个每日主题。

        offset 用来「换一批」：不同的 offset 得到不同的关键词种子，
        从而在同一天生成多批互不重复的推荐。
        """
        key = self.daily_key(platform, day, offset)
        rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest(), 16))
        themes = ['华语经典', '治愈', '流行', '民谣', '轻音乐', '摇滚', '旅行', '晚风']
        singers = sorted({s['singer'] for s in self.favorites if s.get('singer')})
        return ([rng.choice(singers)] if singers else []) + rng.sample(themes, 2)

    def store_daily(self, platform, songs, day=None, offset=0):
        key = self.daily_key(platform, day, offset)
        songs = self._unique(songs)[:30]
        if not songs:
            return
        old = dict(self.daily)
        self.daily[key] = songs
        self.daily = {k: self.daily[k] for k in sorted(self.daily)[-15:]}
        try:
            self.save()
        except OSError:
            self.daily = old
            raise


class PlaybackQueue:
    """队列快照不随搜索页/收藏页切换而变化。"""
    def __init__(self):
        self.songs = []
        self.index = -1

    def set(self, songs, index):
        if not 0 <= index < len(songs):
            raise IndexError(index)
        self.songs = copy.deepcopy(songs)
        self.index = index

    def move(self, step):
        if not self.songs:
            return None
        self.index = (self.index + step) % len(self.songs)
        return copy.deepcopy(self.songs[self.index])
