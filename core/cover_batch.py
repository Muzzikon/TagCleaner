"""Look ahead in catalog queries while applying track results serially."""
import copy
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future,ThreadPoolExecutor


class BatchArtworkDownloads:
    """Share successful image fetches for one run, with a bounded RAM budget."""
    def __init__(self,fetch=None,limit=64*1024*1024,max_entries=256):
        from core.covers import download
        self.fetch=fetch or download;self.limit=limit;self.max_entries=max_entries
        self.cache=OrderedDict();self.flights={};self.lock=threading.RLock();self.size=0
        self.requests=0;self.reuses=0

    def __call__(self,url):
        with self.lock:
            if url in self.cache:
                self.reuses+=1;self.cache.move_to_end(url);return self.cache[url]
            future=self.flights.get(url)
            owner=future is None
            if owner:future=Future();self.flights[url]=future;self.requests+=1
            else:self.reuses+=1
        if not owner:return future.result()
        try:
            blob=self.fetch(url)
            with self.lock:
                if len(blob or b'')<=self.limit:
                    self.cache[url]=blob;self.size+=len(blob or b'')
                    while self.size>self.limit or len(self.cache)>self.max_entries:
                        _,old=self.cache.popitem(last=False);self.size-=len(old or b'')
                future.set_result(blob)
            return blob
        except BaseException as error:
            future.set_exception(error);raise
        finally:
            with self.lock:self.flights.pop(url,None)

    def clear(self):
        with self.lock:self.cache.clear();self.size=0


class CoverSearchBatch:
    """At most two pending catalog searches; no parallel file/state writes."""
    def __init__(self,records,indices,query=None,progress=None,cancelled=None):
        self.records=records;self.indices=list(dict.fromkeys(indices));self.query=query
        self.progress=progress or (lambda index,message:None)
        self.cancelled=cancelled or (lambda:False)
        self.pending={};self.cache=OrderedDict();self.position=0;self.pool=None
        self.started=None;self.elapsed=None
        self.downloads=BatchArtworkDownloads();self.requests=0;self.reuses=0

    def __enter__(self):
        self.started=time.perf_counter()
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='cover-prefetch')
        self._fill();return self

    def _fill(self):
        from core.cover_query import default_query
        from core.cover_search import discover
        while len(self.pending)<2 and self.position<len(self.indices) and not self.cancelled():
            index=self.indices[self.position];self.position+=1
            query=self.query or default_query(self.records[index]);key=tuple(query.get(k,'') for k in ('artist','title','album'))
            future=self.cache.get(key)
            if future is not None:
                self.reuses+=1;self.cache.move_to_end(key)
            else:
                self.requests+=1
                if not key[0] or not key[1]:
                    future=Future();future.set_exception(ValueError('Укажите исполнителя и название в поиске обложки.'))
                else:
                    future=self.pool.submit(discover,*key,progress=lambda message,i=index:self.progress(i,message),cancelled=self.cancelled)
                self.cache[key]=future
                while len(self.cache)>64:self.cache.popitem(last=False)
            self.pending[index]=(key,future)

    def result(self,index):
        key,future=self.pending.pop(index)
        try:
            value=future.result()
            # A failed source must be retried on a later occurrence, rather
            # than becoming cached successful absence for the rest of a run.
            if value[1] and self.cache.get(key) is future:self.cache.pop(key,None)
            return copy.deepcopy(value)
        except BaseException:
            if self.cache.get(key) is future:self.cache.pop(key,None)
            raise
        finally:self._fill()

    def __exit__(self,*exc):
        for _,future in self.pending.values():future.cancel()
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.pending.clear();self.cache.clear()
        self.downloads.clear()
        self.elapsed=time.perf_counter()-self.started

    def stats(self):
        return {'catalog_queries':self.requests,'catalog_reuses':self.reuses,
                'image_requests':self.downloads.requests,'image_reuses':self.downloads.reuses,
                'elapsed_seconds':round(self.elapsed if self.elapsed is not None else time.perf_counter()-self.started if self.started is not None else 0,3)}
