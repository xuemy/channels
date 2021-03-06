"""
channels.views
~~~~~~~~~~~~~~

:copyright: (c) 2012 DISQUS.
:license: Apache License 2.0, see LICENSE for more details.
"""
import simplejson
import time


from channels.app import db, publisher
from channels.contrib.lrucache import LRUCache


class Publisher(object):
    def __init__(self, client, buffer_size=10000):
        self.client = client
        self.lru = LRUCache(1500, 1, buffer_size)

    def publish(self, key, value):
        now = time.time()
        val = self.lru.get(key)
        if val and (val + 3) < now:
            return False
        self.lru.put(key, now)
        self.client.publish(key, value)
        return True


class View(object):
    def __init__(self, redis, publisher, name, version=1):
        self.version = version
        self.ns = 'view:%d:%s' % (self.version, name)
        self.pns = 'channel:%d: %s' % (self.version, name)
        self.redis = redis
        self.publisher = Publisher(publisher)

    def add(self, data, score, _key=None, **kwargs):
        """
        Adds an object.

        This will serialize it into the shared object cache, as well
        as add it to the materialized view designated with ``kwargs``.
        """
        # Immediately update this object in the shared object cache
        self.redis.hmset(self.get_obj_key(data['id']), data)

        # Update the filtered sorted set (based on kwargs)
        self.add_to_set(data['id'], score, data, _key, **kwargs)

    def add_to_set(self, id, score, _data=None, _key=None, **kwargs):
        """
        Adds an object to a materialized view.
        """
        key = self.get_key(_key, **kwargs)
        self.redis.zadd(key, id, float(score))

        if _data is None:
            _data = self.get(id)

        # Send the notice out to our subscribers that this data
        # was added
        json_data = simplejson.dumps({
            'event': 'add',
            'score': score,
            'data': _data,
        })

        self.publisher.publish(self.get_channel_key(key), json_data)

    def incr_in_set(self, id, score, _data=None, _key=None, **kwargs):
        """
        Adds an object to a materialized view.
        """
        key = self.get_key(_key, **kwargs)
        score = self.redis.zincrby(key, id, float(score))

        if _data is None:
            _data = self.get(id)

        json_data = simplejson.dumps({
            'event': 'add',
            'score': score,
            'data': _data,
        })

        # Send the notice out to our subscribers that this data
        # was added
        self.publisher.publish(self.get_channel_key(key), json_data)

    def remove(self, data, _key=None, **kwargs):
        """
        Removes an object.

        This will only remove it from the materialized view as passed with
        ``kwargs``.
        """
        self.redis.zrem(self.get_key(_key, **kwargs), data['id'])
        self.redis.remove(self.get_obj_key(data['id']))

    def remove_from_set(self, id, _key=None, **kwargs):
        """
        Removes an object from its materialized view.
        """
        self.redis.zrem(self.get_key(_key, **kwargs), id)

    def incr_counter(self, id, key, amount=1):
        return self.redis.hincrby(self.get_obj_key(id), key, amount)

    def get(self, id):
        """
        Fetchs an object from the shared object cache.
        """
        result = self.redis.hgetall(self.get_obj_key(id))
        if not result:
            return
        return result

    def get_many(self, id_list):
        """
        Fetchs many objects from the shared object cache.
        """
        result = {}
        with self.redis.map() as conn:
            for id in id_list:
                result[id] = conn.hgetall(self.get_obj_key(id))

        for key, value in result.iteritems():
            if value:
                result[key] = dict(value)
            else:
                result[key] = None

        return result

    def list(self, offset=0, limit=-1, desc=True, _key=None, **kwargs):
        """
        Returns a list of objects from the given materialized view.
        """
        if desc:
            func = self.redis.zrevrange
        else:
            func = self.redis.zrange

        key = self.get_key(_key, **kwargs)
        id_list = func(key, offset, offset + limit)

        if not id_list:
            if not self.redis.exists(key):
                return None
            return []

        obj_cache = {}
        with self.redis.map() as conn:
            for id in id_list:
                key = self.get_obj_key(id)
                obj_cache[id] = conn.hgetall(key)

        results = filter(bool, [dict(obj_cache[t]) for t in id_list if obj_cache])

        return results

    def search(self, query, field, limit=100, desc=True, _key=None, **kwargs):
        """
        Returns a list of objects from the given materialized view that match query.

        This will only iterate a maximum of 1000 rows.
        """
        i, n = 0, 0
        results = []
        words = set(query.lower().split(' '))
        while i < 1000 and n < limit:
            chunk = self.list(offset=i, limit=limit, desc=desc, _key=_key, **kwargs)
            if not chunk:
                break
            for obj in chunk:
                i += 1
                tokens = set(obj[field].lower().split(' '))

                if tokens.intersection(words):
                    results.append(obj)
                    n += 1

            if n > limit:
                break
        return results

    def get_key(self, keybase=None, **kwargs):
        kwarg_str = '&'.join('%s=%s' % (k, v) for k, v in sorted(kwargs.items()))

        if keybase:
            keybase = '%s:%s' % (self.ns, keybase)
        else:
            keybase = self.ns

        return '%s:%s' % (keybase, kwarg_str)

    def get_channel_key(self, key):
        return '%s:%s' % (self.pns, key)

    def get_obj_key(self, id):
        return '%s:objects:%s' % (self.ns, id)

posts = View(db, publisher, 'posts')
threads = View(db, publisher, 'threads')
users = View(db, publisher, 'users')
