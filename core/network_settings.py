"""One proxy route for HTTPS clients; never expose proxy URLs in reports."""
import os
from urllib.parse import urlsplit
import requests


def proxy_for_url(url):
    # Same environment/Windows system settings and NO_PROXY as Requests.
    proxies = requests.utils.get_environ_proxies(url)
    proxy = requests.utils.select_proxy(url, proxies)
    return ('http://' + proxy if '://' not in proxy else proxy) if proxy else None


def curl_environment(url):
    proxy = proxy_for_url(url)
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}:
            del env[key]
    # Selection already applied NO_PROXY. Do not let curl choose another route.
    env['no_proxy'] = ''
    if proxy:
        env['https_proxy'] = proxy
    return env


def http_proxy_for_url(url):
    proxy = proxy_for_url(url)
    if proxy and urlsplit(proxy).scheme.lower() not in {'http', 'https'}:
        raise RuntimeError('Shazam: для этого клиента требуется HTTP/HTTPS-прокси или VPN в режиме TUN; '
                           'неподдерживаемый тип прокси не заменяется прямым подключением.')
    return proxy


def route_summary(url):
    try:
        proxy = proxy_for_url(url)
        return {'route': 'proxy' if proxy else 'direct',
                'proxy_scheme': urlsplit(proxy).scheme.lower() if proxy else None}
    except Exception:
        return {'route': 'configuration_error'}
