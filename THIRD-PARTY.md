# Сторонние компоненты

В исходниках и архиве приложения есть шрифт [Inter](https://rsms.me/inter/) под SIL Open Font License 1.1 ([текст](ui/fonts/OFL.txt)) и модели [FSRCNN](https://github.com/Saafke/FSRCNN_Tensorflow) и [LapSRN](https://github.com/fannymonori/TF-LapSRN) под Apache 2.0 ([текст](data/upscale/LICENSE.txt)). Для восстановления маленьких обложек используется [Real-ESRGAN General x4v3](https://github.com/xinntao/Real-ESRGAN) под BSD 3-Clause ([текст](data/upscale/REAL-ESRGAN-LICENSE.txt)). Точные хеши и ревизии моделей указаны в [манифесте](data/upscale/manifest.json).

В ZIP для Windows входит загрузчик [uv](https://github.com/astral-sh/uv) версии 0.12.23. Тексты Apache 2.0 и MIT находятся рядом с ним в `runtime/licenses`.

При первом запуске uv загружает [Python от Astral](https://github.com/astral-sh/python-build-standalone) и пакеты из PyPI. Требования перечислены в [requirements-windows.txt](requirements-windows.txt). [FFmpeg](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2) и [Chromaprint](https://github.com/acoustid/chromaprint/releases/tag/v1.6.2) загружаются из релизов их сборщиков. Эти инструменты и пакеты распространяются на своих условиях.

В Android-сборку входят Chaquopy 17.2.0, CPython 3.14.8, OpenSSL 3.5.9 и Mutagen 1.48.1. Тексты лицензий входят в APK и доступны в [android/licenses](android/licenses). [Chaquopy](https://github.com/chaquo/chaquopy/tree/17.2.0) · [CPython](https://github.com/python/cpython/tree/v3.14.8) · [OpenSSL](https://github.com/openssl/openssl/tree/openssl-3.5.9) · [Mutagen](https://pypi.org/project/mutagen/1.48.1/)

Исходный архив Mutagen приложен к [Android-релизу](https://github.com/Muzzikon/TagCleaner/releases/tag/android-v1.0.0).
