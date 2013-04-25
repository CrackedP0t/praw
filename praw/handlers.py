"""Provides classes that handle request dispatching."""
import socket
import time
from functools import wraps
from praw.helpers import normalize_url
from requests import Session
from six.moves import cPickle
from threading import Lock


class RateLimitHandler(object):

    """The base handler that provides thread-safe rate limiting enforcement.


    While this handler is threadsafe, PRAW is not thread safe when the same
    `Reddit` instance is being utilized from multiple threads.

    """

    last_call = {}  # Stores a two-item list: [lock, previous_call_time]
    rl_lock = Lock()  # lock used for adding items to last_call

    @staticmethod
    def rate_limit(function):
        """Return a decorator that enforces API request limit guidelines.

        We are allowed to make a API request every api_request_delay seconds as
        specified in praw.ini. This value may differ from reddit to reddit. For
        reddit.com it is 2. Any function decorated with this will be forced to
        delay _rate_delay seconds from the calling of the last function
        decorated with this before executing.

        This decorator must be applied to a RateLimitHandler class method or
        instance method as it assumes `rl_lock` and `last_call` are available.

        """
        @wraps(function)
        def wrapped(cls, _rate_domain, _rate_delay, **kwargs):
            cls.rl_lock.acquire()
            lock_last = cls.last_call.setdefault(_rate_domain, [Lock(), 0])
            with lock_last[0]:  # Obtain the domain specific lock
                cls.rl_lock.release()
                # Sleep if necessary, then perform the request
                now = time.time()
                delay = lock_last[1] + _rate_delay - now
                if delay > 0:
                    now += delay
                    time.sleep(delay)
                lock_last[1] = now
                return function(cls, **kwargs)
        return wrapped

    @classmethod
    def evict(cls, urls):
        """Method utilized to evict entries for the given urls.

        :param urls: An interable containing normalized urls.

        By default this method does nothing as a cache need not be present.

        """

    def __init__(self):
        self.http = Session()  # Each instance should have its own session

    def request(self, request, proxies, timeout, **_):
        """Responsible for dispatching the request and returning the result.

        Network level exceptions should be raised and only
        ``requests.Response`` should be returned.

        :param request: A ``requests.PreparedRequest`` object containing all
            the data necessary to perform the request.
        :param proxies: A dictionary of proxy settings to be utilized for the
            request.
        :param timeout: Specifies the maximum time that the actual HTTP request
            can take.

        ``**_`` should be added to the method call to ignore the extra
        arguments intended for the cache hander.

        """
        return self.http.send(request, proxies=proxies, timeout=timeout,
                              allow_redirects=False)
RateLimitHandler.request = RateLimitHandler.rate_limit(
    RateLimitHandler.request)


class DefaultHandler(RateLimitHandler):

    """Extends the RateLimitHandler to add thread-safe caching support."""

    ca_lock = Lock()
    cache = {}
    cache_hit_callback = None
    timeouts = {}

    @staticmethod
    def with_cache(function):
        """Return a decorator that interacts with a handler's cache.

        This decorator must be applied to a DefaultHandler class method or
        instance method as it assumes `cache`, `ca_lock` and `timeouts` are
        available.

        """
        @wraps(function)
        def wrapped(cls, _cache_key, _cache_ignore, _cache_timeout, **kwargs):
            def clear_timeouts():
                """Clear the cache of timed out results."""
                for key in list(cls.timeouts):
                    if time.time() - cls.timeouts[key] > _cache_timeout:
                        del cls.timeouts[key]
                        del cls.cache[key]

            if _cache_ignore:
                return function(cls, **kwargs)
            with cls.ca_lock:
                clear_timeouts()
                if _cache_key in cls.cache:
                    if cls.cache_hit_callback:
                        cls.cache_hit_callback(_cache_key)
                    return cls.cache[_cache_key]
            # Releasing the lock before actually making the request allows for
            # the possibility of more than one thread making the same request
            # to get through. Without having domain-specific caching (under the
            # assumption only one request to a domain can be made at a
            # time), there isn't a better way to handle this.
            result = function(cls, **kwargs)
            with cls.ca_lock:
                cls.timeouts[_cache_key] = time.time()
                cls.cache[_cache_key] = result
                return result
        return wrapped

    @classmethod
    def evict(cls, urls):
        """Remove cached responses by URL."""
        urls = set(normalize_url(url) for url in urls)
        with cls.ca_lock:
            for key in list(cls.cache):
                if key[0] in urls:
                    del cls.cache[key]
                    del cls.timeouts[key]
DefaultHandler.request = DefaultHandler.with_cache(RateLimitHandler.request)


class MultiprocessHandler(object):

    """A PRAW handler to interact with the PRAW multi-process server."""

    def __init__(self, host='localhost', port=10101):
        self.host = host
        self.port = port

    def _relay(self, **kwargs):
        """Send the request through the Server and return the http response."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_fp = sock.makefile('rwb')  # Used for pickle
        try:
            sock.connect((self.host, self.port))
            cPickle.dump(kwargs, sock_fp, cPickle.HIGHEST_PROTOCOL)
            sock_fp.flush()
            return cPickle.load(sock_fp)
        finally:
            sock_fp.close()
            sock.close()

    def evict(self, urls):
        """Forward the eviction to the server and return its response."""
        return self._relay(method='evict', urls=urls)

    def request(self, **kwargs):
        """Forward the request to the server and return its http response."""
        return self._relay(method='request', **kwargs)
